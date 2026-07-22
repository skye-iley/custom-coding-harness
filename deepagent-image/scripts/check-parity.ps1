# Check .ps1 <-> .sh script pairs for drift.
#
# Reports line-count deltas and fails on a MISSING pair member. Deep content
# parity is NOT auto-checked: the two scripts express the same docker invocation
# in different shell syntax (${MountWorkspace} vs $MOUNT_WORKSPACE, /home/agent vs
# $HOME_DIR, Linux-only `-e HOME=/tmp`), so any text-level cross-shell diff yields
# false positives. Keep the pairs in sync by review. Mirror of check-parity.sh.
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
    $ps1 = Join-Path (Join-Path $Root "scripts") $pair[0]
    $sh = Join-Path (Join-Path $Root "scripts") $pair[1]
    if (-not (Test-Path $ps1)) { Write-Host "MISSING: $ps1"; $Failed = $true; continue }
    if (-not (Test-Path $sh)) { Write-Host "MISSING: $sh"; $Failed = $true; continue }
    $ps1Lines = (Get-Content $ps1).Count
    $shLines = (Get-Content $sh).Count
    $diff = $ps1Lines - $shLines
    Write-Host "$($pair[0]) ($ps1Lines lines) vs $($pair[1]) ($shLines lines) - diff $diff lines"
}

# Semantic parity (M4 trust boundary): markers that MUST appear in BOTH
# run-docker.{ps1,sh}. Line-count diff can't catch a fail-closed guard or a
# mask pre-flight dropped from one script only - this does. Mirror of check-parity.sh.
$markers = @("harness mask-scan", "refusing to launch unmasked", "DEEPAGENTS_MASK")
$rdPs1 = Join-Path (Join-Path $Root "scripts") "run-docker.ps1"
$rdSh  = Join-Path (Join-Path $Root "scripts") "run-docker.sh"
foreach ($m in $markers) {
    $inPs1 = Select-String -Path $rdPs1 -Pattern ([regex]::Escape($m)) -Quiet
    $inSh  = Select-String -Path $rdSh  -Pattern ([regex]::Escape($m)) -Quiet
    if (-not $inPs1 -or -not $inSh) {
        Write-Host "PARITY: marker missing from one of run-docker.{ps1,sh}: '$m'"
        $Failed = $true
    }
}

if ($Failed) {
    Write-Host "PARITY CHECK FAILED"
    exit 1
}
Write-Host "PARITY CHECK OK"
