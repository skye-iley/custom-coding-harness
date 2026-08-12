# Create the OPTIONAL host dev venv at deepagent-image\.venv.
#
# Why this exists: deepagent-image/CLAUDE.md documents the keyless admin commands
# (`harness past list`, `harness config`, ...) as host-side and tells you to
# activate deepagent-image\.venv -- but nothing created that venv, so every one of
# them, plus the image-tier tests and any langchain-touching probe, was reachable
# only through Docker.
#
# What it is NOT:
#   * Not a third Python stack. It mirrors the IMAGE's harness venv (/opt/venv)
#     from the same project\requirements.txt. The two-stack rule is unchanged:
#     harness deps here, workspace deps in <workspace>\.conda\env, never mixed.
#   * Not required. CI installs pytest and nothing else and runs the host tier
#     that way (.github\workflows\ci.yml). That property -- the suite runs with
#     nothing installed -- is load-bearing; this venv must stay opt-in and no
#     `pytest.importorskip` guard may be dropped because it exists locally.
#   * Not the authority. There is no lockfile, so this venv can drift from the
#     image (platform wheels, resolution date). `smoke` builds clean and stays
#     the check before a PR -- same caveat the bind-mount dev loop carries.
#
# Usage:
#   .\scripts\dev-setup.ps1              # create (or reuse) and install
#   .\scripts\dev-setup.ps1 -Recreate    # delete and rebuild from scratch
#
# Mirror of dev-setup.sh -- keep the pair in sync.
param(
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv"
$Req = Join-Path $Root "project\requirements.txt"
# The image is ubuntu:24.04, whose python3 is 3.12. A host on a different minor
# resolves different wheels, which is the drift this warns about (not fatal --
# the harness supports a range, and smoke is what actually gates a PR).
$ImagePyMinor = "3.12"

if (-not (Test-Path $Req)) {
    Write-Error "dev-setup: requirements not found at $Req"
}

# `python` is the Windows launcher name; `python3` is usually a Store shim that
# opens the Microsoft Store instead of running, so prefer python then py -3.
$PyExe = $null
foreach ($candidate in @("python", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $PyExe = $cmd.Source; break }
}
if (-not $PyExe) {
    Write-Error "dev-setup: no python on PATH (looked for python, py)"
}

$HostPy = & $PyExe -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($HostPy -ne $ImagePyMinor) {
    Write-Host "dev-setup: NOTE host python is $HostPy, image is $ImagePyMinor." -ForegroundColor Yellow
    Write-Host "dev-setup:      Resolved wheels may differ from the image. smoke is the authority." -ForegroundColor Yellow
}

if ($Recreate -and (Test-Path $Venv)) {
    Write-Host "dev-setup: removing $Venv"
    Remove-Item -Recurse -Force $Venv
}

if (Test-Path $Venv) {
    Write-Host "dev-setup: reusing existing venv at $Venv"
} else {
    Write-Host "dev-setup: creating venv at $Venv (python $HostPy)"
    & $PyExe -m venv $Venv
}

$VPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VPy)) {
    # A venv created under WSL/Git Bash in this tree lays out bin/ instead.
    $VPyPosix = Join-Path $Venv "bin\python"
    if (Test-Path $VPyPosix) {
        $VPy = $VPyPosix
    } else {
        Write-Error "dev-setup: venv looks incomplete (no python under $Venv)"
    }
}

Write-Host "dev-setup: installing harness deps + pytest (this pulls langchain; a few minutes cold)"
& $VPy -m pip install --upgrade pip | Out-Null
# pytest is NOT in requirements.txt on purpose -- the image installs it only in
# the `test` stage, so the runtime image ships without it. The host venv is a dev
# tool, so it gets both.
& $VPy -m pip install -r $Req pytest
if ($LASTEXITCODE -ne 0) { Write-Error "dev-setup: pip install failed" }

Write-Host ""
Write-Host "dev-setup: done. Activate with:"
Write-Host "    $Venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "Then, from deepagent-image\project\:"
Write-Host "    python -m pytest tests/          # host + image tiers (importorskip no longer skips)"
Write-Host "    python -m harness past list      # keyless admin commands"
Write-Host ""
Write-Host "Reminder: this venv is a convenience, not the gate. Run .\scripts\smoke.ps1"
Write-Host "before a PR -- it builds clean and catches what a local install papers over"
Write-Host "(a missing COPY, a stale image layer, an image-only dep)."
