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
    [string]$Cpus = "2",
    [string]$Memory = "4g",
    [string]$PidsLimit = "512",
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
$NetjailDir = Join-Path $Root "netjail"
if (-not (Test-Path $EnvFile)) {
    throw "Missing $EnvFile - copy project\.env.example to project\.env and set API keys."
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
    # NB: run-docker.sh also carries an opt-in host-uid remap (MAP_HOST_USER /
    # HOST_UID / HOST_GID) to fix bind-mount permissions on native Linux, where
    # mounts keep host ownership and the image runs as uid 10001. Intentionally
    # omitted here: Docker Desktop's Windows/WSL2 bind mounts squash ownership,
    # so the agent (uid 10001) can already write the mounted workspace, and
    # `id -u` has no Windows equivalent.
    $NetArgs = @("--add-host=host.docker.internal:host-gateway")
    $ProxyEnv = @()
}

$dockerArgs = @(
    "run", "--rm"
) + $TtyFlags + @(
    "--cpus", $Cpus,
    "--memory", $Memory,
    "--pids-limit", $PidsLimit
) + $NetArgs + $ProxyEnv + @(
    "--env-file", $EnvFile,
    "-e", "AGENT_WORKSPACE=/project/workspace",
    "-e", "DEEPAGENTS_STATE_DIR=/project/state",
    "-v", "${MountWorkspace}:/project/workspace",
    "-v", "${StateHostDir}:/project/state"
) + $SrcMountArgs

# Git identity: mount host .gitconfig read-only into the agent user's home (uid 10001 -> /home/agent),
# not /root (container runs USER agent). Never mount ~/.ssh into an autonomous-agent container -
# use a scoped, per-session deploy key or a short-lived token for pushes instead.
$GitConfig = Join-Path $env:USERPROFILE ".gitconfig"
if (Test-Path $GitConfig) {
    $dockerArgs += "-v", "${GitConfig}:/home/agent/.gitconfig:ro"
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
}
