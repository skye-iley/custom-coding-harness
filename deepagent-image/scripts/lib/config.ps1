# Shared host-side pre-spinup config resolution (Milestone 5, C3/§7c):
# CLI flag > host env > project\.env > .harness-profile.yaml > default.
#
# One function instead of a scrape block duplicated per variable in
# run-docker.ps1. Mirror of lib/config.sh's env-var-driven equivalent -- bash has
# no separate flag/env distinction, so its "CLI" tier collapses into its env
# tier; PowerShell has a real named -Flag, so it gets its own tier here.

function Get-EnvFileValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path $Path)) { return "" }
    $line = Select-String -Path $Path -Pattern "^\s*$Name\s*=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if (-not $line) { return "" }
    return ($line.Line -replace "^\s*$Name\s*=", '').Trim().Trim('"').Trim("'")
}

# Last matching `key: value` line from the flat-scalar .harness-profile.yaml. A
# comment-only value ("key:   # note", used throughout the checked-in example
# for every unset key) is unset, not the literal string "# note" -- matches the
# fix in harness/config.py's load_profile (a value that IS a comment isn't one).
function Get-ProfileFileValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return "" }
    $line = Select-String -Path $Path -Pattern "^\s*$Key\s*:" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if (-not $line) { return "" }
    $raw = ($line.Line -replace "^\s*${Key}\s*:", '').Trim()
    $hashIdx = $raw.IndexOf('#')
    if ($hashIdx -ge 0) { $raw = $raw.Substring(0, $hashIdx).Trim() }
    return $raw.Trim('"').Trim("'")
}

function Resolve-HostSetting {
    param(
        [string]$Value,       # explicit -Flag; wins outright
        [string]$EnvVarName,  # e.g. DEEPAGENTS_JAIL
        [string]$ProfileKey,  # e.g. jail (the .harness-profile.yaml key)
        [string]$Default,
        [string]$EnvFile,
        [string]$ProfileFile
    )
    if ($Value) { return $Value }
    $hostEnv = [Environment]::GetEnvironmentVariable($EnvVarName)
    if ($hostEnv) { return $hostEnv }
    $fromEnvFile = Get-EnvFileValue -Path $EnvFile -Name $EnvVarName
    if ($fromEnvFile) { return $fromEnvFile }
    $fromProfile = Get-ProfileFileValue -Path $ProfileFile -Key $ProfileKey
    if ($fromProfile) { return $fromProfile }
    return $Default
}
