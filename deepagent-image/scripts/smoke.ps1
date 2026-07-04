# Post-build smoke. Builds both targets (self-contained — no ordering dependency
# on build.ps1), runs a bare-runtime import check against the shippable image,
# then runs the whole suite via pytest discovery on the test image.
#
# Usage:
#   .\smoke.ps1            # normal (bridge networking)
#   .\smoke.ps1 -NetJail   # run the import check + pytest INSIDE the NetJail
#                          # (--internal net + allowlisted egress proxy). Proves
#                          # the harness boots and the suite passes with no direct
#                          # egress, and that the jail plumbing stands up fail-closed.
[CmdletBinding()]
param([switch]$NetJail)

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

try {
    # Bare-runtime import smoke: third-party deps + the harness package (incl. the
    # cost tracker, so a providers<->cost import cycle fails here). Runs against the
    # plain runtime image - NO test layer - so a runtime import the test layer would
    # mask still fails here.
    $importCode = "import deepagents, langgraph, langchain_openai; from harness.cli import main; from harness.cost import CostTrackerMiddleware; print('runtime import ok')"
    $importArgs = @("run", "--rm") + $NetArgs + $ProxyEnv + @("deepagent-harness", "python3", "-c", $importCode)
    & docker @importArgs
    if ($LASTEXITCODE -ne 0) { throw "runtime import check failed" }

    # Full suite via pytest discovery on the test image. -v names every test case
    # (file::test PASSED/FAILED); -ra recaps non-passing tests at the end. Failures
    # print the failing test id, file:line, and asserted values by default.
    $pytestArgs = @("run", "--rm") + $NetArgs + $ProxyEnv + @("deepagent-harness-test", "python3", "-m", "pytest", "tests/", "-v", "-ra")
    & docker @pytestArgs
    if ($LASTEXITCODE -ne 0) { throw "pytest suite failed" }
} finally {
    if ($NetJail) { Netjail-Down }
}
