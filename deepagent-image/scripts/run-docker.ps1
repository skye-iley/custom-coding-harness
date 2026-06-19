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
    [string]$WorkspacePath = ""
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
    }
}

$SeedSource = $DefaultWorkspace
Seed-Workspace -Target $WorkspacePath -SeedSource $SeedSource

$dockerArgs = @(
    "run", "--rm",
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
