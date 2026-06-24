# Run the harness container. Requires project\.env (copy from project\.env.example).
#
# Usage:
#   .\run-docker.ps1 "your task here"
#   .\run-docker.ps1 test task here
#   .\run-docker.ps1 -WorkspacePath C:\path\to\repo "your task here"
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$TaskParts = @(),
    [string]$WorkspacePath = "",
    # Resource caps (Milestone 1 §3): a Docker host-boundary control so a runaway
    # agent can't exhaust the host CPU/RAM or fork-bomb it. NOT a sandbox (trust
    # boundary is still the container; design_doc_mvp.md §5). Override e.g.
    #   .\run-docker.ps1 -Cpus 4 -Memory 8g -PidsLimit 1024 "task"
    [string]$Cpus = "2",
    [string]$Memory = "4g",
    [string]$PidsLimit = "512"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvFile = Join-Path $Root "project\.env"
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
Seed-Workspace -Target $WorkspacePath -SeedSource $SeedSource

# -it gives the REPL prompt loop a TTY. If stdin is redirected (CI, piped
# smoke tests) only -i is requested; Docker can't allocate a pty for -t
# without one, and the harness already handles the non-TTY case itself.
$TtyFlags = @("-i")
if (-not [Console]::IsInputRedirected) {
    $TtyFlags = @("-i", "-t")
}

$dockerArgs = @(
    "run", "--rm"
) + $TtyFlags + @(
    "--cpus", $Cpus,
    "--memory", $Memory,
    "--pids-limit", $PidsLimit,
    "--env-file", $EnvFile,
    "-e", "AGENT_WORKSPACE=/project/workspace",
    "-v", "${WorkspacePath}:/project/workspace"
)

# Git identity: mount host .gitconfig read-only into the agent user's home (uid 10001 -> /home/agent),
# not /root (container runs USER agent). Never mount ~/.ssh into an autonomous-agent container -
# use a scoped, per-session deploy key or a short-lived token for pushes instead.
$GitConfig = Join-Path $env:USERPROFILE ".gitconfig"
if (Test-Path $GitConfig) {
    $dockerArgs += "-v", "${GitConfig}:/home/agent/.gitconfig:ro"
}

$dockerArgs += "deepagent-harness"

if ($TaskParts.Count -gt 0) {
    $dockerArgs += "python3", "main.py"
    $dockerArgs += $TaskParts
}

Write-Host "Workspace: $WorkspacePath"
if ($TaskParts.Count -gt 0) {
    Write-Host "Task: $($TaskParts -join ' ')"
}

& docker @dockerArgs
