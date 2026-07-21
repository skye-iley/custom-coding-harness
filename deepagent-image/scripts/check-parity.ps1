# Check .ps1 ↔ .sh script pairs for drift.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Failed = $false

$pairs = @(
    @("build.ps1", "build.sh"),
    @("run-docker.ps1", "run-docker.sh"),
    @("smoke.ps1", "smoke.sh"),
    @("verify.ps1", "verify.sh"),
    @("sync-models.ps1", "sync-models.sh")
)

foreach ($pair in $pairs) {
    $ps1 = Join-Path $Root "scripts" $pair[0]
    $sh = Join-Path $Root "scripts" $pair[1]
    if (-not (Test-Path $ps1)) { Write-Host "MISSING: $ps1"; $Failed = $true; continue }
    if (-not (Test-Path $sh)) { Write-Host "MISSING: $sh"; $Failed = $true; continue }
    $ps1Lines = (Get-Content $ps1).Count
    $shLines = (Get-Content $sh).Count
    $diff = $ps1Lines - $shLines
    Write-Host "$($pair[0]) ($ps1Lines lines) vs $($pair[1]) ($shLines lines) — diff $diff lines"
}

if ($Failed) {
    Write-Host "PARITY CHECK FAILED" *>> "$null"
    exit 1
}
Write-Host "PARITY CHECK OK"
