# Run the harness container. Requires project\.env (copy from project\.env.example).
# Consumes the `deepagent-harness` runtime image built by build.ps1
# (`docker build --target runtime`) - no test code, no pytest.
#
# Usage:
#   .\run-docker.ps1 "your task here"
#   .\run-docker.ps1 test task here
#   .\run-docker.ps1 -WorkspacePath C:\path\to\repo "your task here"
#   .\run-docker.ps1 -NetJail "task"         # deny-all-egress jail + allowlist
#   .\run-docker.ps1 -Ephemeral "task"       # revert all workspace changes on close
#                                            #   (in-container /refresh pulls live host edits)
#   .\run-docker.ps1 -SaveWorkspace "task"   # ephemeral + snapshot to workspace-logs\<ts>\
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$TaskParts = @(),
    [string]$WorkspacePath = "",
    # Resource caps (Milestone 1 §3): a Docker host-boundary control so a runaway
    # agent can't exhaust the host CPU/RAM or fork-bomb it. NOT a sandbox (trust
    # boundary is still the container; docs/milestones/mvp.md §5). Override e.g.
    #   .\run-docker.ps1 -Cpus 4 -Memory 8g -PidsLimit 1024 "task"
    # Milestone 5, C3: defaults live in Resolve-HostSetting, not here, so an
    # unpassed flag can fall through to env / .env / .harness-profile.yaml --
    # a literal "2" here would shadow a saved `cpus: 6` in the profile.
    [string]$Cpus = "",
    [string]$Memory = "",
    [string]$PidsLimit = "",
    # Milestone 5, C3: CLI parity for knobs that previously only had env/.env
    # coverage. All four resolve CLI flag > host env > project\.env >
    # .harness-profile.yaml > default via Resolve-HostSetting (lib\config.ps1).
    [string]$Model = "",
    [string]$MaskMode = "",
    [string]$Jail = "",
    [string]$JailApparmor = "",
    # Write/update .harness-config.yaml's autonomy_level before launch (strict|
    # guided|autonomous). An imperative action, not a resolved Settings field --
    # HITL's presence-of-file-turns-it-on design (M3) means this necessarily
    # turns HITL on if it wasn't already; that's the point, not a side effect.
    [string]$Autonomy = "",
    # NetJail (see netjail\README.md): run the agent on an --internal docker
    # network with no route to host or internet, punching only the holes declared
    # in netjail\host-services.txt (host ports) and netjail\allowed-domains.txt
    # (egress domains). Default off = the agent keeps normal bridge networking.
    [switch]$NetJail,
    # Ephemeral workspace: mount a throwaway COPY of the workspace, so every change
    # the agent makes is reverted on close (the real workspace is never touched).
    # The real workspace is ALSO mounted read-only at /project/workspace-src, so the
    # in-container /refresh command + refresh_workspace tool can pull live host edits
    # into the copy mid-run (still reverted on close). -SaveWorkspace additionally
    # snapshots the post-run copy to workspace-logs\<ts>\ before it is discarded, and
    # implies -Ephemeral. The rebuildable conda env (.conda) is excluded from the copy
    # so a run doesn't clone gigabytes.
    [switch]$Ephemeral,
    [switch]$SaveWorkspace
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvFile = Join-Path $Root "project\.env"
$ProfileFile = Join-Path $Root "project\.harness-profile.yaml"
$NetjailDir = Join-Path $Root "netjail"
. (Join-Path $PSScriptRoot "lib\config.ps1")
if (-not (Test-Path $EnvFile)) {
    throw "Missing $EnvFile - copy project\.env.example to project\.env and set API keys."
}

# Milestone 5, C3: resource caps resolve through the same four-tier chain as every
# other pre-spinup knob, so `harness config security`'s saved caps actually take
# effect instead of being written and ignored. Mirror of run-docker.sh.
$Cpus = Resolve-HostSetting -Value $Cpus -EnvVarName "CPUS" -ProfileKey "cpus" `
    -Default "2" -EnvFile $EnvFile -ProfileFile $ProfileFile
$Memory = Resolve-HostSetting -Value $Memory -EnvVarName "MEMORY" -ProfileKey "memory" `
    -Default "4g" -EnvFile $EnvFile -ProfileFile $ProfileFile
$PidsLimit = Resolve-HostSetting -Value $PidsLimit -EnvVarName "PIDS_LIMIT" -ProfileKey "pids_limit" `
    -Default "512" -EnvFile $EnvFile -ProfileFile $ProfileFile

# Mask mode resolves ONCE here, not inside the mask-scan block, because it has two
# consumers: the scan container (which computes the overlay set) and the agent
# container (whose in-container `harness doctor` / mask.resolve re-read the env).
# One resolution, two consumers -- the point of lib\config. Mirror of run-docker.sh.
$ScanMode = Resolve-HostSetting -Value $MaskMode -EnvVarName "DEEPAGENTS_MASK_MODE" `
    -ProfileKey "mask_mode" -Default "" -EnvFile $EnvFile -ProfileFile $ProfileFile

# NetJail is a [switch], so "not passed" is indistinguishable from "-NetJail:$false"
# by value alone -- only consult the lower tiers when the flag is genuinely absent.
if (-not $PSBoundParameters.ContainsKey('NetJail')) {
    $NetJailResolved = Resolve-HostSetting -Value "" -EnvVarName "NET_JAIL" -ProfileKey "net_jail" `
        -Default "0" -EnvFile $EnvFile -ProfileFile $ProfileFile
    if ($NetJailResolved -in @("1", "true", "yes", "on")) { $NetJail = [switch]$true }
}

$DefaultWorkspace = Join-Path $Root "project\workspace"
if (-not $WorkspacePath) {
    $WorkspacePath = $DefaultWorkspace
} elseif (-not [System.IO.Path]::IsPathRooted($WorkspacePath) -and $WorkspacePath -match '\s') {
    throw "WorkspacePath '$WorkspacePath' looks like a task string. Use: .\run-docker.ps1 `"your task`""
}

if (-not (Test-Path $WorkspacePath)) {
    New-Item -ItemType Directory -Force -Path $WorkspacePath | Out-Null
}
$WorkspacePath = (Resolve-Path $WorkspacePath).Path

# Harness state (checkpoints.sqlite + past.sqlite + session.env) lives OUTSIDE the
# workspace mount, at /project/state, so the agent's file/shell tools (rooted at
# /project/workspace) can't read the past archive or corrupt the live DBs. Backed
# by a host dir under the harness repo, keyed per-workspace so distinct repos keep
# separate archives (mirrors the old per-workspace <workspace>/.deepagents split).
# The Python side reads DEEPAGENTS_STATE_DIR via archive.state_dir. Mirror in run-docker.sh.
$sha256 = [System.Security.Cryptography.SHA256]::Create()
$WsKey = [System.BitConverter]::ToString(
    $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($WorkspacePath))
).Replace("-", "").Substring(0, 12).ToLower()
$StateHostDir = Join-Path $Root "project\state\$WsKey"
if (-not (Test-Path $StateHostDir)) {
    New-Item -ItemType Directory -Force -Path $StateHostDir | Out-Null
}

function Seed-Workspace {
    param([string]$Target, [string]$SeedSource)
    if (-not (Test-Path $SeedSource)) { return }
    foreach ($file in @("environment.yml", ".gitignore")) {
        $dest = Join-Path $Target $file
        if (-not (Test-Path $dest)) {
            Copy-Item (Join-Path $SeedSource $file) $dest
        }
    }
    $scriptSrc = Join-Path $SeedSource "scripts\run-in-env.sh"
    $scriptDest = Join-Path $Target "scripts\run-in-env.sh"
    if ((Test-Path $scriptSrc) -and -not (Test-Path $scriptDest)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $scriptDest) | Out-Null
        Copy-Item $scriptSrc $scriptDest
        # NB: run-docker.sh does `chmod +x` here; intentionally omitted on the
        # PowerShell side. NTFS has no unix exec bit, and Docker Desktop's
        # Windows bind mounts present files to the Linux container as executable
        # (0755) regardless, so the agent can still run scripts/run-in-env.sh.
    }
}

$SeedSource = $DefaultWorkspace

# Copy $Src -> $Dst, excluding the heavy, rebuildable workspace conda env (.conda).
# robocopy /E mirrors the tree; exit codes 0-7 are success, >=8 is a real failure.
function Copy-Workspace {
    param([string]$Src, [string]$Dst)
    New-Item -ItemType Directory -Force -Path $Dst | Out-Null
    & robocopy $Src $Dst /E /XD (Join-Path $Src ".conda") /NFL /NDL /NJH /NJS /NP | Out-Null
    $rc = $LASTEXITCODE
    $global:LASTEXITCODE = 0   # robocopy's success codes (1-7) aren't errors
    if ($rc -ge 8) { throw "workspace copy failed (robocopy exit $rc): $Src -> $Dst" }
}

# ---------------------------------------------------------------------------
# Ephemeral workspace (-Ephemeral / -SaveWorkspace): mount a throwaway COPY instead
# of the real workspace, so all changes revert on close. The state dir (keyed to
# the REAL workspace path) is left persistent. -SaveWorkspace implies -Ephemeral and
# snapshots the post-run copy before it is discarded.
if ($SaveWorkspace) { $Ephemeral = $true }
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$MountWorkspace = $WorkspacePath
$EphemeralDir = $null
# In ephemeral mode we ALSO bind-mount the real workspace read-only at
# /project/workspace-src, so the in-container /refresh command + refresh_workspace
# tool can pull live host edits into the throwaway copy mid-run (still reverted on
# close). Empty on a normal run, so nothing extra is mounted.
$SrcMountArgs = @()
if ($Ephemeral) {
    $EphemeralDir = Join-Path $Root ".ephemeral\$Stamp"
    Copy-Workspace -Src $WorkspacePath -Dst $EphemeralDir
    $MountWorkspace = $EphemeralDir
    $SrcMountArgs = @(
        "-v", "${WorkspacePath}:/project/workspace-src:ro",
        "-e", "DEEPAGENTS_WORKSPACE_SRC=/project/workspace-src"
    )
    Write-Host "Ephemeral: on - changes revert on close."
    Write-Host "  Live copy (run tests here): $EphemeralDir"
    Write-Host "  /refresh pulls live host edits from $WorkspacePath into the copy."
}

Seed-Workspace -Target $MountWorkspace -SeedSource $SeedSource

# ---------------------------------------------------------------------------
# NetJail: deny-all-egress network jail with an explicit, config-driven allowlist.
# Mirror of run-docker.sh's NET_JAIL path. Sidecars are torn down in `finally`.
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
    # Returns the docker flags (network + proxy/OLLAMA env) for the agent container.
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
        # the agent in that state rather than hand it an open proxy.
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

# -it gives the REPL prompt loop a TTY. If stdin is redirected (CI, piped
# smoke tests) only -i is requested; Docker can't allocate a pty for -t
# without one, and the harness already handles the non-TTY case itself.
$TtyFlags = @("-i")
if (-not [Console]::IsInputRedirected) {
    $TtyFlags = @("-i", "-t")
}

# Network + proxy flags: NetJail path, or the default host-gateway bridge.
if ($NetJail) {
    $jail = Netjail-Up
    $NetArgs = $jail.NetArgs
    $ProxyEnv = $jail.ProxyEnv
} else {
    # Host reachability: make `host.docker.internal` resolve to the host. Docker
    # Desktop/WSL2 provide it already; re-declaring host-gateway is a harmless
    # no-op. Set OLLAMA_HOST=http://host.docker.internal:11434 in project\.env
    # (inside the container `localhost` is the container, not the host).
    #
    # NB: run-docker.sh carries a host-uid remap (MAP_HOST_USER / HOST_UID /
    # HOST_GID, decided by scripts/lib/hostmap.sh) to fix bind-mount permissions
    # on native Linux, where mounts keep host ownership and the image runs as uid
    # 10001. There it *auto-enables* on a native-Linux engine (precedence:
    # MAP_HOST_USER=1 force on, =0 force off, unset → auto). Intentionally a no-op
    # here: this script only ever runs under Docker Desktop on Windows, whose
    # Windows/WSL2 bind mounts squash ownership (agent uid 10001 can already write
    # the mounts), and `id -u` has no Windows equivalent. Kept in sync with the
    # .sh so the pair doesn't drift; the mapping simply never applies on Windows.
    $NetArgs = @("--add-host=host.docker.internal:host-gateway")
    $ProxyEnv = @()
}

# ---------------------------------------------------------------------------
# M4 mask pre-flight: when DEEPAGENTS_MASK != 0, run a throwaway scan container
# to resolve the mask set, then emit empty overlay mounts for each masked path.
# Scan output lines: <mode> <type> <tier> <relpath>
$MaskArgs = @()
# Enable/disable (DEEPAGENTS_MASK) deliberately gets NO -Flag or profile-file
# tier -- it's a debugging escape hatch (config.py's Settings.mask_enabled is
# excluded from the profile on purpose), not something to casually flip via a
# saved default. Host env > project\.env only, unchanged from pre-M5.
$MaskEnabled = $env:DEEPAGENTS_MASK
if ([string]::IsNullOrEmpty($MaskEnabled)) {
    # Launcher env unset — fall back to project\.env so the host-side scan/overlay
    # gate honours the SAME DEEPAGENTS_MASK the container sees (§13). Host env wins.
    $envLine = Select-String -Path $EnvFile -Pattern '^\s*DEEPAGENTS_MASK\s*=' -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($envLine) {
        $MaskEnabled = ($envLine.Line -replace '^\s*DEEPAGENTS_MASK\s*=', '').Trim().Trim('"').Trim("'")
    }
}
if ($MaskEnabled -eq "" -or $MaskEnabled -eq "1") {
    # Surface mask-scan diagnostics (protection-reduction / symlink warnings) instead
    # of dropping stderr; stdout stays the parseable grammar.
    $scanErr = New-TemporaryFile
    # Forward DEEPAGENTS_MASK_MODE (deny/allow, §13) into the scan container so the
    # resolver honours it — the scan gets no --env-file, so without this the env
    # knob is silently ignored and `allow` degrades to `deny` (under-masking).
    # Milestone 5, C3: -MaskMode / .harness-profile.yaml's mask_mode now layer on
    # top of the same host-env / .env fallback this always had. $ScanMode is
    # resolved once at the top of the script (shared with the agent container).
    $ScanModeArgs = @()
    if (-not [string]::IsNullOrEmpty($ScanMode)) {
        $ScanModeArgs = @("-e", "DEEPAGENTS_MASK_MODE=$ScanMode")
    }
    $scanRunArgs = @(
        "run", "--rm",
        "-v", "${MountWorkspace}:/project/workspace:ro",
        "-v", "${StateHostDir}:/project/state",
        "-e", "DEEPAGENTS_STATE_DIR=/project/state"
    ) + $ScanModeArgs + @(
        "deepagent-harness", "python3", "-m", "harness", "mask-scan"
    )
    # Native command stderr must not become a terminating error under
    # ErrorActionPreference=Stop (see Remove-ContainerIfExists above) — mask-scan
    # writes EXPECTED warnings to stderr (protection-reduction, floor-negation),
    # and without this override the launcher would crash on any of them instead
    # of printing and continuing per invariant 24 ("protection reduction is loud",
    # not fatal).
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $scanOutput = & docker @scanRunArgs 2>$scanErr.FullName
    $scanRc = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ((Get-Item $scanErr.FullName).Length -gt 0) {
        Get-Content $scanErr.FullName | ForEach-Object { Write-Host $_ }
    }
    Remove-Item -Force $scanErr.FullName -ErrorAction SilentlyContinue
    if ($scanRc -ne 0) {
        # Fail closed: never launch the agent unmasked when masking is enabled.
        Write-Error "[mask] FATAL: mask-scan failed (exit $scanRc) - refusing to launch unmasked. Fix the scan or set DEEPAGENTS_MASK=0 to disable masking."
        exit 1
    }
    if ($scanOutput) {
        $emptyFile = New-TemporaryFile
        $emptyDir = New-Item -ItemType Directory -Path ([System.IO.Path]::GetTempPath()) -Name ([System.IO.Path]::GetRandomFileName())
        foreach ($line in $scanOutput) {
            $parts = $line -split '\s+', 4
            if ($parts.Length -lt 4) { continue }
            $mode = $parts[0]; $type = $parts[1]; $tier = $parts[2]; $relpath = $parts[3]
            $relpath = $relpath -replace '%20', ' '
            $source = if ($type -eq "dir") { $emptyDir.FullName } else { $emptyFile.FullName }
            $MaskArgs += "-v", "${source}:/project/workspace/${relpath}:ro"
        }
        Write-Host "Mask: $($MaskArgs.Count/2) path(s) masked"  # each -v is 2 args
    }
}

# M4 slice H: the bwrap fs jail needs the narrow seccomp profile, because Docker's
# default profile blocks unprivileged user-namespace creation (see seccomp/README.md).
# Off by default (§13) - enabling it trades a little outer-boundary attack surface
# for a real inner boundary, so it is the operator's explicit call. Fail closed: if
# the jail is asked for and the profile is missing, refuse to launch rather than run
# unjailed while the operator believes otherwise.
$JailArgs = @()
# Milestone 5, C3: -Jail / .harness-profile.yaml's jail now layer on top of the
# same host-env / .env fallback this always had.
$JailMode = Resolve-HostSetting -Value $Jail -EnvVarName "DEEPAGENTS_JAIL" `
    -ProfileKey "jail" -Default "" -EnvFile $EnvFile -ProfileFile $ProfileFile
if ($JailMode -and $JailMode -notin @("0", "false", "no", "off")) {
    $SeccompProfile = Join-Path $PSScriptRoot "..\seccomp\userns.json"
    if (-not (Test-Path $SeccompProfile)) {
        Write-Error "[jail] FATAL: DEEPAGENTS_JAIL is on but $SeccompProfile is missing - refusing to launch unjailed. Run 'python3 -m harness seccomp-sync' or set DEEPAGENTS_JAIL=0."
        exit 1
    }
    $JailArgs = @("--security-opt", "seccomp=$((Resolve-Path $SeccompProfile).Path)")
    # The seccomp relaxation and the in-container jail must be turned on by the SAME
    # decision. jail.jail_enabled() reads DEEPAGENTS_JAIL from the environment and does
    # not consult Settings, so a value resolved from the -Jail flag / host env / profile
    # tier would apply the relaxation here and never start the jail inside - relaxed
    # syscalls, no containment, and nsguard (which defaults to tracking DEEPAGENTS_JAIL)
    # off too. Normalized to "1": the container only tests truthiness.
    $JailArgs += "-e", "DEEPAGENTS_JAIL=1"
    Write-Host "Jail: bwrap fs jail ON (narrow seccomp profile)"

    # M4 slice J (11.6): seccomp is only ONE of the two gates. On an AppArmor host
    # (Ubuntu/Debian Docker) the generated `docker-default` profile carries a literal
    # `deny mount,`, so bwrap gets past `unshare` and then fails at its first mount -
    # and entering a user namespace does not shed AppArmor confinement, so nothing the
    # jail does from inside can work around it. Slice J vendors a narrowed profile
    # (apparmor/deepagent-userns) that keeps every other docker-default rule.
    #
    # Unset (default): select the narrowed profile and PROBE that the daemon will
    # accept it, before launching anything real. Mirror of run-docker.sh.
    # Milestone 5, C3: -JailApparmor / .harness-profile.yaml's jail_apparmor now
    # layer on top of the same host-env / .env fallback this always had.
    $Apparmor = Resolve-HostSetting -Value $JailApparmor -EnvVarName "DEEPAGENTS_JAIL_APPARMOR" `
        -ProfileKey "jail_apparmor" -Default "" -EnvFile $EnvFile -ProfileFile $ProfileFile
    if (-not $Apparmor) {
        # Ask the DAEMON, not this machine: the profile must be loaded on the host
        # running dockerd, which for a remote daemon / Colima-Lima VM / WSL distro is
        # not this one. A local /sys read would need root AND would lie there.
        #
        # Order matters, and not for performance. A daemon with no AppArmor support
        # ACCEPTS `--security-opt apparmor=<anything>` and ignores it (measured on
        # Docker Desktop/WSL2), so probing first would "succeed" against a profile that
        # is not loaded anywhere and make the launcher announce a boundary that does not
        # exist. Ask what actually confines a container here before asking for a profile.
        $probe = "deepagent-userns"
        $inForce = (docker run --rm deepagent-harness sh -c 'cat /proc/self/attr/apparmor/current 2>/dev/null || cat /proc/self/attr/current 2>/dev/null || true' 2>$null)
        if ($inForce) { $inForce = ($inForce -replace ' \(.*', '').Trim() }
        if ($inForce -and $inForce -notin @("unconfined", "kernel")) {
            docker run --rm --security-opt "apparmor=$probe" deepagent-harness true 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $Apparmor = $probe
            } else {
                # Fail CLOSED. Never silently fall back to apparmor=unconfined: that is
                # a categorically wider trade (a whole LSM off, vs. five relaxed
                # syscalls) and only an operator may make it.
                Write-Error "[jail] FATAL: DEEPAGENTS_JAIL is on and this daemon confines containers with AppArmor profile '$inForce', whose 'deny mount,' blocks bwrap at its first mount (seccomp is NOT the problem - see apparmor/README.md). The narrowed profile '$probe' is not loaded on the Docker daemon's host. Load it: sudo deepagent-image/scripts/install-apparmor-profile.sh  |  Wider trade: DEEPAGENTS_JAIL_APPARMOR=unconfined (drops ALL of $inForce)  |  Or: DEEPAGENTS_JAIL=0"
                exit 1
            }
        }
    }
    if ($Apparmor) {
        $JailArgs += @("--security-opt", "apparmor=$Apparmor")
        if ($Apparmor -eq "unconfined") {
            Write-Host "Jail: AppArmor DISABLED for this container (apparmor=unconfined)."
            Write-Host "      This drops ALL of docker-default - the /proc and /sys write denials and the"
            Write-Host "      ptrace peer restriction - not just its deny-mount rule. Wider than the five"
            Write-Host "      relaxed syscalls DEEPAGENTS_JAIL alone costs. See apparmor/README.md."
        } else {
            Write-Host "Jail: AppArmor profile '$Apparmor' (loaded on the Docker daemon's host)."
        }
    }
}

# Milestone 5, C3: -Model / .harness-profile.yaml's model, forwarded as an
# explicit -e so it reaches the container even when it's not in project\.env
# (docker prefers an explicit -e over the same var in --env-file, so this wins
# regardless of what .env also says once cli/env/profile resolved a value).
$ResolvedModel = Resolve-HostSetting -Value $Model -EnvVarName "DEEPAGENTS_MODEL" `
    -ProfileKey "model" -Default "" -EnvFile $EnvFile -ProfileFile $ProfileFile
$ModelArgs = @()
if (-not [string]::IsNullOrEmpty($ResolvedModel)) {
    $ModelArgs = @("-e", "DEEPAGENTS_MODEL=$ResolvedModel")
}

# Same for the resolved mask mode: the scan container already gets it (it computes
# the overlay set), but the AGENT container never did, so an in-container
# `harness doctor` re-ran mask.resolve against an unset env and reported `deny` on
# an `allow` launch. Enforcement is unaffected either way -- the jail's overmounts
# read the frozen mask-snapshot.txt, not a fresh resolve -- this is about the two
# halves reporting the same mode.
$MaskModeArgs = @()
if (-not [string]::IsNullOrEmpty($ScanMode)) {
    $MaskModeArgs = @("-e", "DEEPAGENTS_MASK_MODE=$ScanMode")
}

# Host-only knobs the container cannot otherwise observe: --cpus/--memory/
# --pids-limit/NetJail are `docker run` flags, never env vars, so without these
# the in-session `/config` read-only view and `harness doctor` would report the
# built-in defaults no matter what this launch actually applied. Informational
# only -- nothing in the container acts on them.
$NetJailValue = if ($NetJail) { "1" } else { "0" }
$CapEnvArgs = @(
    "-e", "CPUS=$Cpus",
    "-e", "MEMORY=$Memory",
    "-e", "PIDS_LIMIT=$PidsLimit",
    "-e", "NET_JAIL=$NetJailValue"
)

$dockerArgs = @(
    "run", "--rm"
) + $TtyFlags + $JailArgs + @(
    "--cpus", $Cpus,
    "--memory", $Memory,
    "--pids-limit", $PidsLimit
) + $NetArgs + $ProxyEnv + @(
    "--env-file", $EnvFile,
    "-e", "AGENT_WORKSPACE=/project/workspace",
    "-e", "DEEPAGENTS_STATE_DIR=/project/state",
    "-v", "${MountWorkspace}:/project/workspace",
    "-v", "${StateHostDir}:/project/state"
) + $SrcMountArgs + $MaskArgs + $ModelArgs + $MaskModeArgs + $CapEnvArgs

# Git identity: mount host .gitconfig read-only into the agent user's home (uid 10001 -> /home/agent),
# not /root (container runs USER agent). Never mount ~/.ssh into an autonomous-agent container -
# use a scoped, per-session deploy key or a short-lived token for pushes instead.
$GitConfig = Join-Path $env:USERPROFILE ".gitconfig"
if (Test-Path $GitConfig) {
    $dockerArgs += "-v", "${GitConfig}:/home/agent/.gitconfig:ro"
}

# -Autonomy: write/update autonomy_level in .harness-config.yaml before the
# mount check below sees it. Plain text edit (no YAML parser, matching every
# other host-side scrape/write in this script) -- replace the existing
# `autonomy_level:` line if present, else prepend one (creating the file if it
# doesn't exist yet).
if ($Autonomy) {
    if ($Autonomy -notin @("strict", "guided", "autonomous")) {
        Write-Error "[harness] FATAL: -Autonomy must be one of strict|guided|autonomous, got '$Autonomy'"
        exit 1
    }
    $HitlConfigPath = Join-Path $Root "project\.harness-config.yaml"
    # NOT Set-Content -Encoding utf8: on Windows PowerShell 5.1 (this repo's primary
    # shell) that writes a BOM. config.parse_config used to read with plain "utf-8",
    # so the first key parsed as "<BOM>autonomy_level" and the harness SystemExit'd on
    # the unknown-key branch -- i.e. -Autonomy bricked the run it was asked to
    # configure. The readers are BOM-tolerant now (utf-8-sig); this side simply must
    # not emit one. `n line endings for parity with run-docker.sh and config.py's
    # own writers.
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    if (Test-Path $HitlConfigPath) {
        $existing = Get-Content $HitlConfigPath
        if ($existing -match '^\s*autonomy_level\s*:') {
            $updated = $existing -replace '^\s*autonomy_level\s*:.*', "autonomy_level: $Autonomy"
        } else {
            $updated = @("autonomy_level: $Autonomy") + $existing
        }
        [System.IO.File]::WriteAllText($HitlConfigPath, (($updated -join "`n") + "`n"), $Utf8NoBom)
    } else {
        [System.IO.File]::WriteAllText($HitlConfigPath, "autonomy_level: $Autonomy`n", $Utf8NoBom)
    }
    Write-Host "HITL: autonomy_level set to '$Autonomy' in .harness-config.yaml (turns HITL on for this run if it wasn't already)"
}

# HITL config: .harness-config.yaml is host-local + gitignored (like .env), so it
# is NOT baked into the image. Mount it into /project (the harness CWD) when
# present so its mere presence turns HITL on (cli reads Path.cwd()/.harness-config.yaml).
# Absent => not mounted => HITL stays off (byte-for-byte Milestone 2).
$HitlConfig = Join-Path $Root "project\.harness-config.yaml"
if (Test-Path $HitlConfig) {
    $dockerArgs += "-v", "${HitlConfig}:/project/.harness-config.yaml:ro"
    Write-Host "HITL config: mounted (.harness-config.yaml present)"
}

# Unified config profile (Milestone 5, C4): same story as .harness-config.yaml --
# gitignored, so NOT baked into the image; mount it into /project (the harness
# CWD) so the container's resolve_settings() sees the same profile tier the host
# side just resolved against. Without this the profile's in-session fields
# (topic/max_cost/max_tokens) are silently ignored on every containerized run and
# `/config save` writes into the throwaway container layer.
#
# Read-WRITE, unlike the HITL mount: `/config save` is a documented in-session
# action that must land on the host. The agent can't reach it -- its file tools
# are rooted at /project/workspace and the bwrap jail (slice H) binds /project
# read-only.
if (Test-Path $ProfileFile) {
    $dockerArgs += "-v", "${ProfileFile}:/project/.harness-profile.yaml"
    Write-Host "Config profile: mounted (.harness-profile.yaml present)"
}

$dockerArgs += "deepagent-harness"

if ($TaskParts.Count -gt 0) {
    $dockerArgs += "python3", "main.py"
    $dockerArgs += $TaskParts
}

Write-Host "Workspace: $WorkspacePath"
if ($NetJail) { Write-Host "NetJail: on (deny-all egress + allowlist)" }
if ($TaskParts.Count -gt 0) {
    Write-Host "Task: $($TaskParts -join ' ')"
}

try {
    & docker @dockerArgs
} finally {
    if ($NetJail) { Netjail-Down }
    if ($Ephemeral) {
        if ($SaveWorkspace) {
            $LogDir = Join-Path $Root "workspace-logs\$Stamp"
            Copy-Workspace -Src $MountWorkspace -Dst $LogDir
            Write-Host "Workspace snapshot saved under $LogDir"
        }
        if ($EphemeralDir -and (Test-Path $EphemeralDir)) {
            Remove-Item -Recurse -Force $EphemeralDir
        }
        Write-Host "Ephemeral: workspace changes discarded."
    }
    # Clean up mask temp files
    if ($emptyFile -and (Test-Path $emptyFile.FullName)) {
        Remove-Item -Force $emptyFile.FullName -ErrorAction SilentlyContinue
    }
    if ($emptyDir -and (Test-Path $emptyDir.FullName)) {
        Remove-Item -Recurse -Force $emptyDir.FullName -ErrorAction SilentlyContinue
    }
}
