# Post-build smoke. Builds both targets (self-contained — no ordering dependency
# on build.ps1), runs a bare-runtime import check against the shippable image,
# then runs the whole suite via pytest discovery on the test image.
#
# Usage:
#   .\smoke.ps1                 # normal (bridge networking)
#   .\smoke.ps1 -NetJail        # run the import check + pytest INSIDE the NetJail
#                               # (--internal net + allowlisted egress proxy). Proves
#                               # the harness boots and the suite passes with no direct
#                               # egress, and that the jail plumbing stands up fail-closed.
#   .\smoke.ps1 -KeepArtifacts  # ship files that tests write via the `artifact_dir`
#                               # fixture out to test-artifacts\<timestamp>\ on the host
#                               # (default: they go to the container's tmp and vanish).
#   .\smoke.ps1 -JailCheck      # REQUIRE the M4 slice H jail gate to pass. By default
#                               # the gate runs but self-skips on a host that cannot
#                               # nest user namespaces; -JailCheck turns that skip into
#                               # a failure (use in CI to pin the boundary).
#   .\smoke.ps1 -LiveModel      # also run the live-model tier: real prompts to a real
#                               # model, real replies asserted (tests\test_live_model.py).
#                               # Default off, so the suite stays hermetic. Needs a
#                               # reachable model — with the shipped default that is a
#                               # host `ollama serve`. Individual cases SKIP rather than
#                               # fail when it is unreachable, so read the -ra recap
#                               # instead of trusting a green exit.
[CmdletBinding()]
param([switch]$NetJail, [switch]$KeepArtifacts, [switch]$JailCheck, [switch]$LiveModel)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$NetjailDir = Join-Path $Root "netjail"

# ---------------------------------------------------------------------------
# NetJail plumbing — mirror of run-docker.ps1's NET_JAIL path. Kept in sync with
# that script (and with run-docker.sh / smoke.sh). Only used when -NetJail is set;
# the sidecars are torn down in the finally block below.
$JailNet    = if ($env:NETJAIL_JAIL_NET)    { $env:NETJAIL_JAIL_NET }    else { "deepagent-jail" }
$EgressNet  = if ($env:NETJAIL_EGRESS_NET)  { $env:NETJAIL_EGRESS_NET }  else { "deepagent-egress" }
$SocatImage = if ($env:NETJAIL_SOCAT_IMAGE) { $env:NETJAIL_SOCAT_IMAGE } else { "alpine/socat:latest" }
$ProxyImage = if ($env:NETJAIL_PROXY_IMAGE) { $env:NETJAIL_PROXY_IMAGE } else { "kalaksi/tinyproxy:latest" }
$script:Sidecars = @()
$script:FilterTmp = $null

function Remove-ContainerIfExists {
    param([string]$Name)
    # Guard by existence instead of `docker rm 2>$null`: under ErrorActionPreference
    # Stop, a native command's stderr is wrapped as a terminating error, so removing
    # an absent container would abort the script.
    if (docker ps -aq -f "name=^$Name$") { docker rm -f $Name | Out-Null }
}

function Netjail-Down {
    try {
        foreach ($c in $script:Sidecars) { Remove-ContainerIfExists $c }
        if ($script:FilterTmp -and (Test-Path $script:FilterTmp)) { Remove-Item -Force $script:FilterTmp }
    } catch { Write-Warning "NetJail teardown: $_" }
}

function Netjail-Up {
    # Returns the docker flags (network + proxy/OLLAMA env) for a jailed container.
    $netArgs = @("--network", $JailNet)
    $proxyEnv = @()
    $noProxy = "localhost,127.0.0.1"

    # Networks (idempotent). jail = --internal (no gateway -> no host/internet).
    $nets = docker network ls --format '{{.Name}}'
    if ($nets -notcontains $JailNet)   { docker network create --internal $JailNet | Out-Null }
    if ($nets -notcontains $EgressNet) { docker network create $EgressNet | Out-Null }

    # Host-service forwarders: one socat relay per host-services.txt line.
    $servicesFile = Join-Path $NetjailDir "host-services.txt"
    if (Test-Path $servicesFile) {
        foreach ($line in Get-Content $servicesFile) {
            $t = $line.Trim()
            if ($t -eq "" -or $t.StartsWith("#")) { continue }
            $parts = $t -split '\s+'
            $name = $parts[0]; $port = $parts[1]
            $cname = "deepagent-fwd-$name"
            Remove-ContainerIfExists $cname
            docker run -d --rm --name $cname `
                --network $EgressNet `
                --add-host=host.docker.internal:host-gateway `
                --cap-drop=ALL --security-opt=no-new-privileges `
                $SocatImage `
                "TCP-LISTEN:$port,fork,reuseaddr" "TCP:host.docker.internal:$port" | Out-Null
            docker network connect $JailNet $cname | Out-Null
            $script:Sidecars += $cname
            $noProxy = "$noProxy,$cname"
            if ($name -eq "ollama") { $proxyEnv += @("-e", "OLLAMA_HOST=http://${cname}:$port") }
        }
    }

    # Egress proxy: domain-allowlisted HTTP(S) forward proxy for git/pip/npm.
    $domainsFile = Join-Path $NetjailDir "allowed-domains.txt"
    $allowed = @()
    if (Test-Path $domainsFile) {
        $allowed = Get-Content $domainsFile | ForEach-Object { $_.Trim() } |
                   Where-Object { $_ -ne "" -and -not $_.StartsWith("#") } |
                   ForEach-Object { ($_ -split '\s+')[0] }
    }
    if ($allowed.Count -gt 0) {
        # Generate the tinyproxy Filter: anchor each plain domain to itself and
        # its subdomains (and only those), not arbitrary substrings.
        $script:FilterTmp = (New-TemporaryFile).FullName
        foreach ($d in $allowed) {
            "(^|\.)$([regex]::Escape($d))$" | Add-Content -Path $script:FilterTmp -Encoding ascii
        }
        $pname = "deepagent-proxy"
        Remove-ContainerIfExists $pname
        docker run -d --rm --name $pname `
            --network $EgressNet `
            --cap-drop=ALL --security-opt=no-new-privileges `
            -v "$(Join-Path $NetjailDir 'tinyproxy.conf'):/etc/tinyproxy/tinyproxy.conf:ro" `
            -v "${script:FilterTmp}:/etc/tinyproxy/filter:ro" `
            $ProxyImage | Out-Null
        docker network connect $JailNet $pname | Out-Null
        $script:Sidecars += $pname
        # Fail CLOSED: assert the proxy loaded our allowlist config. If the conf
        # mount silently failed to land, a stock proxy image falls back to its
        # default (filtering disabled) and would allow ALL egress — refuse to run
        # the smoke in that state rather than hand it an open proxy.
        Start-Sleep 2
        docker exec $pname grep -q '^FilterDefaultDeny Yes' /etc/tinyproxy/tinyproxy.conf 2>$null
        if ($LASTEXITCODE -ne 0) {
            docker logs $pname 2>&1 | Select-Object -Last 20 | Out-Host
            Netjail-Down
            throw "NET_JAIL: egress proxy did not load the allowlist config (would fail open) - aborting."
        }
        $purl = "http://${pname}:8888"
        $proxyEnv += @(
            "-e", "HTTP_PROXY=$purl",  "-e", "HTTPS_PROXY=$purl",
            "-e", "http_proxy=$purl",  "-e", "https_proxy=$purl",
            "-e", "NO_PROXY=$noProxy", "-e", "no_proxy=$noProxy")
    }

    return @{ NetArgs = $netArgs; ProxyEnv = $proxyEnv }
}

# Build the runtime (shippable) image and the test image (FROM runtime + pytest +
# tests/). The second build reuses the cached runtime layers. Builds run BEFORE
# any jail is stood up: image builds need pip/PyPI egress, which the jail denies.
docker build --target runtime -t deepagent-harness $Root
if ($LASTEXITCODE -ne 0) { throw "runtime image build failed" }
docker build --target test -t deepagent-harness-test $Root
if ($LASTEXITCODE -ne 0) { throw "test image build failed" }

# Under -NetJail the two smoke runs below execute on the --internal jail network
# with the allowlisted egress proxy in front. Neither run makes outbound calls, so
# this proves the harness boots + the suite passes with zero direct egress, and
# that the jail plumbing (networks, forwarders, fail-closed proxy) stands up.
$NetArgs = @()
$ProxyEnv = @()
if ($NetJail) {
    $jail = Netjail-Up
    $NetArgs = $jail.NetArgs
    $ProxyEnv = $jail.ProxyEnv
    Write-Host "NetJail: on (deny-all egress + allowlist)"
}

# Test-artifact capture: when -KeepArtifacts is set, bind-mount a fresh host
# folder to /artifacts (OUTSIDE /project, so the conftest artifact-guard leaves it
# alone) and point DEEPAGENTS_TEST_ARTIFACTS_DIR at it, so files tests write via
# the `artifact_dir` fixture survive the disposable container. Off = the fixture
# falls back to the container's tmp_path and everything is deleted with the container.
$ArtifactArgs = @()
$ArtifactHostDir = $null
if ($KeepArtifacts) {
    $ArtifactHostDir = Join-Path $Root "test-artifacts\$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    New-Item -ItemType Directory -Force -Path $ArtifactHostDir | Out-Null
    $ArtifactArgs = @("-v", "${ArtifactHostDir}:/artifacts", "-e", "DEEPAGENTS_TEST_ARTIFACTS_DIR=/artifacts")
    Write-Host "KeepArtifacts: on -> $ArtifactHostDir"
}

try {
    # Bare-runtime import smoke: third-party deps + the harness package (incl. the
    # cost tracker, so a providers<->cost import cycle fails here). Runs against the
    # plain runtime image - NO test layer - so a runtime import the test layer would
    # mask still fails here.
    $importCode = "import deepagents, langgraph, langchain_openai; from harness.cli import main; from harness.cost import CostTrackerMiddleware; print('runtime import ok')"
    $importArgs = @("run", "--rm") + $NetArgs + $ProxyEnv + @("deepagent-harness", "python3", "-c", $importCode)
    & docker @importArgs
    if ($LASTEXITCODE -ne 0) { throw "runtime import check failed" }

    # M4 smoke: verify mask resolution works end-to-end by running mask-scan
    $maskCode = "from harness.mask import resolve; r = resolve('/tmp', '/tmp'); print(f'mask OK: {len(r.masked)} entries')"
    $maskArgs = @("run", "--rm") + $NetArgs + $ProxyEnv + @("deepagent-harness", "python3", "-c", $maskCode)
    & docker @maskArgs
    if ($LASTEXITCODE -ne 0) { throw "mask resolution check failed" }

    # M4 fail-closed: a mask-scan failure MUST abort the launch, never run unmasked.
    # (1) mask-scan signals failure via a nonzero exit on a poisoned config; (2) both
    # launchers key their abort on that. Regression guard for the run-docker.{ps1,sh}
    # fail-closed contract (a scan error must not degrade to a maskless launch).
    $scanFailDir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Force -Path $scanFailDir | Out-Null
    "secret`n#!mode: allow" | Set-Content -Path (Join-Path $scanFailDir ".agentignore") -Encoding ascii
    $failArgs = @("run", "--rm") + $NetArgs + $ProxyEnv + @(
        "-v", "${scanFailDir}:/project/workspace:ro",
        "-e", "AGENT_WORKSPACE=/project/workspace", "-e", "DEEPAGENTS_STATE_DIR=/tmp/mask-state",
        "deepagent-harness", "python3", "-m", "harness", "mask-scan")
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & docker @failArgs 2>$null | Out-Null
    $scanRc = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    Remove-Item -Recurse -Force $scanFailDir -ErrorAction SilentlyContinue
    if ($scanRc -eq 0) { throw "M4 fail-closed: mask-scan exited 0 on a poisoned .agentignore (expected nonzero)" }
    foreach ($launcher in @("run-docker.ps1", "run-docker.sh")) {
        if (-not (Select-String -Path (Join-Path $Root "scripts\$launcher") -Pattern 'refusing to launch unmasked' -Quiet)) {
            throw "M4 fail-closed: $launcher lost its fail-closed guard"
        }
    }
    Write-Host "M4 fail-closed: mask-scan aborts on failure + launchers guarded - ok"

    # M4 slice H: the bwrap fs jail actually holds in the built image. This is the
    # §3 hard gate ("bwrap --unshare-all must really run here"), plus the boundary
    # properties the jail is supposed to buy - asserted against the harness's own
    # jail.bwrap_args, with NO docker mask applied, so the jail is the only enforcer.
    # Needs the vendored narrow seccomp profile: Docker's default blocks unprivileged
    # userns creation by design, which is exactly why DEEPAGENTS_JAIL is opt-in.
    $SeccompProfile = Join-Path $Root "seccomp\userns.json"
    if (-not (Test-Path $SeccompProfile)) {
        throw "M4 jail: $SeccompProfile missing - run 'python3 -m harness seccomp-sync'"
    }
    # Script piped in on stdin (`python3 -`) rather than bind-mounted, to stay in
    # lockstep with smoke.sh — there a bind target is rewritten by MSYS under Git
    # Bash, so stdin is the only form that is portable across both launchers.
    # M4 slice J (§11.6): on an AppArmor-confined host, seccomp is only half the gate -
    # Docker's `docker-default` profile denies `mount` outright, so bwrap fails after
    # `unshare` succeeds. Unset (the default) means we pass nothing and the check skips
    # on such a host. `unconfined` makes it run everywhere at the cost of dropping the
    # WHOLE profile, not just its `deny mount,` - a wider trade than the five relaxed
    # syscalls, so it is opt-in and announced, never a silent default.
    #
    # Slice J adds the narrowed profile: when the operator sets nothing, prefer
    # `deepagent-userns` **if the daemon already has it loaded**. Unlike run-docker this
    # does NOT abort when it is missing - smoke's job is to report, and jail-check.py
    # already self-skips (rc 77) on an LSM denial rather than reddening the run.
    $ApparmorArgs = @()
    $ApparmorChoice = $env:DEEPAGENTS_JAIL_APPARMOR
    if (-not $ApparmorChoice) {
        # LSM-in-force first, then the profile probe - a daemon with no AppArmor support
        # accepts `--security-opt apparmor=<anything>` and ignores it, so probing first
        # would claim a profile that is loaded nowhere. Mirror of run-docker.
        $aaInForce = (docker run --rm deepagent-harness sh -c 'cat /proc/self/attr/apparmor/current 2>/dev/null || cat /proc/self/attr/current 2>/dev/null || true' 2>$null)
        if ($aaInForce) { $aaInForce = ($aaInForce -replace ' \(.*', '').Trim() }
        if ($aaInForce -and $aaInForce -notin @("unconfined", "kernel")) {
            docker run --rm --security-opt "apparmor=deepagent-userns" deepagent-harness true 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ApparmorChoice = "deepagent-userns" }
        }
    }
    if ($ApparmorChoice) {
        $ApparmorArgs = @("--security-opt", "apparmor=$ApparmorChoice")
        if ($ApparmorChoice -eq "unconfined") {
            Write-Host "M4 jail: AppArmor DISABLED for this container (apparmor=unconfined) - drops docker-default entirely, not just its deny-mount rule."
        } else {
            Write-Host "M4 jail: using AppArmor profile '$ApparmorChoice' (loaded on the Docker daemon's host)."
        }
    }
    $jailArgs = @("run", "--rm", "-i") + $NetArgs + $ProxyEnv + @(
        "--security-opt", "seccomp=$SeccompProfile") + $ApparmorArgs + @(
        "-e", "DEEPAGENTS_JAIL=1",
        "deepagent-harness", "python3", "-")
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Get-Content -Raw (Join-Path $Root "scripts\jail-check.py") | & docker @jailArgs
    $jailRc = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    if ($jailRc -eq 77) {
        # Environmental, not a regression, for either reason the gate can report: the
        # kernel/runtime refuses nested userns, or the host LSM denies bwrap's mounts.
        # Only a hard failure when the caller pinned it (CI).
        if ($JailCheck) {
            throw "M4 jail: -JailCheck was set but this host cannot build the jail (see the SKIPPED reason above) - failing."
        }
        Write-Host "M4 jail: SKIPPED (host cannot build the jail - see reason above). Use -JailCheck to require it."
    } elseif ($jailRc -ne 0) {
        throw "M4 jail: boundary check FAILED (rc=$jailRc)"
    } else {
        Write-Host "M4 jail: bwrap gate + masked/unmasked/write/ro boundary checks - ok"
    }

    # Live-model tier: off by default so the suite needs no model, no keys, no
    # network. -LiveModel turns it on and reaches a host-run daemon at
    # host.docker.internal (built in on Docker Desktop; --add-host is the Linux
    # path and is harmless here). Under -NetJail the forwarder already set
    # OLLAMA_HOST for us - don't overwrite it.
    $LiveArgs = @()
    if ($LiveModel) {
        $LiveArgs = @("-e", "DEEPAGENTS_LIVE_MODEL=1", "--add-host", "host.docker.internal:host-gateway")
        if (-not $NetJail) {
            $ollamaHost = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST } else { "http://host.docker.internal:11434" }
            $LiveArgs += @("-e", "OLLAMA_HOST=$ollamaHost")
        }
        if ($env:DEEPAGENTS_MODEL) { $LiveArgs += @("-e", "DEEPAGENTS_MODEL=$($env:DEEPAGENTS_MODEL)") }
        Write-Host "LiveModel: on -> live-model tier will run (cases SKIP if the model is unreachable)"
    }

    # Full suite via pytest discovery on the test image. -v names every test case
    # (file::test PASSED/FAILED); -ra recaps non-passing tests at the end. Failures
    # print the failing test id, file:line, and asserted values by default.
    $pytestArgs = @("run", "--rm") + $NetArgs + $ProxyEnv + $ArtifactArgs + $LiveArgs + @("deepagent-harness-test", "python3", "-m", "pytest", "tests/", "-v", "-ra")
    & docker @pytestArgs
    if ($LASTEXITCODE -ne 0) { throw "pytest suite failed" }
} finally {
    if ($NetJail) { Netjail-Down }
    if ($ArtifactHostDir) { Write-Host "Test artifacts saved under $ArtifactHostDir" }
}
