"""Unit tests for the host-uid mapping decision (scripts/lib/hostmap.sh).

Pure-shell decision, driven here through `bash -c` so it needs no live Docker
daemon. Asserts the full 3x3 matrix from IMMEDIATE_TODO.md:
    MAP_HOST_USER in {unset, "0", "1"}  x  engine in {native-linux, wsl, docker-desktop}
Self-skips when no bash on this host can read the lib.

**Not every `bash` on PATH can run these cases.** On a Windows dev box with WSL
installed, `shutil.which("bash")` finds `C:\\Windows\\system32\\bash.EXE` -- the WSL
launcher -- which cannot resolve a Windows drive path (`C:\\Users\\...` lives at
`/mnt/c/...` inside WSL). Sourcing `_LIB` there fails, so the function under test
is never defined and the call exits 127. Taking the *first* bash on PATH therefore
turned the whole host tier red for a reason that has nothing to do with the code.
Git for Windows' bash reads the same path fine, so candidates are probed and the
first working one wins.
"""
import pathlib
import shutil
import subprocess

import pytest

_LIB = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "lib" / "hostmap.sh"
# scripts/ is host-side and deliberately not COPYed into the image (only
# conda-init-workspace.sh and sandbox-exec.sh are), so in the container _LIB
# resolves to /scripts/lib/hostmap.sh and is absent -- the same reason
# test_config.py skips its launcher cases.
_needs_lib = pytest.mark.skipif(not _LIB.is_file(), reason="hostmap.sh not available (not in test image)")


def _bash_candidates():
    """Every bash worth trying, in preference order, de-duplicated.

    `which("bash")` first -- the right answer everywhere except the Windows+WSL
    case. Then Git for Windows' bash, *derived* from `git` rather than guessed at
    a hardcoded install path: the layout is `<root>/cmd/git.exe` alongside
    `<root>/bin/bash.exe`."""
    found = [p for p in (shutil.which("bash"),) if p]
    git = shutil.which("git")
    if git:
        candidate = pathlib.Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if candidate.is_file():
            found.append(str(candidate))
    seen = []
    for path in found:
        if path not in seen:
            seen.append(path)
    return seen


def _usable_bash():
    """The first candidate that can actually source `_LIB` and run its function.

    Probed, not assumed: "a bash exists" and "this bash can read that path" are
    different claims, and only the second is what these cases need. Returns None
    when none can, so the tests skip rather than fail with a 127 that reads like
    a bug in the lib.

    When `_LIB` is missing (the image) there is nothing to probe against, so
    return the plain `which` result and let `_needs_lib` own that skip -- keeping
    the reported reason accurate."""
    if not _LIB.is_file():
        return shutil.which("bash")
    probe = f'source "{_LIB}"\n_should_map_host_user "Linux" "0" "Ubuntu 24.04" "1"\n'
    for exe in _bash_candidates():
        try:
            done = subprocess.run(
                [exe, "-c", probe], capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip() in ("0", "1"):
            return exe
    return None


_BASH = _usable_bash()
needs_bash = pytest.mark.skipif(
    _BASH is None,
    reason="no bash here can source hostmap.sh (a WSL-only bash cannot read Windows paths)",
)


def _decide(uname_s, is_wsl, docker_os, env):
    """Source the lib and echo _should_map_host_user's verdict ("1"/"0")."""
    script = (
        f'source "{_LIB}"\n'
        f'_should_map_host_user "{uname_s}" "{is_wsl}" "{docker_os}" "{env}"\n'
    )
    out = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


# (uname_s, is_wsl, docker_os) triples for each engine kind.
_NATIVE = ("Linux", "0", "Ubuntu 24.04")
_WSL = ("Linux", "1", "Docker Desktop")
_DESKTOP = ("Linux", "0", "Docker Desktop")
_MACOS = ("Darwin", "0", "Docker Desktop")


@needs_bash
@_needs_lib
@pytest.mark.parametrize("engine", [_NATIVE, _WSL, _DESKTOP, _MACOS])
def test_explicit_on_forces_map(engine):
    # MAP_HOST_USER=1 maps on every engine.
    assert _decide(*engine, "1") == "1"


@needs_bash
@_needs_lib
@pytest.mark.parametrize("engine", [_NATIVE, _WSL, _DESKTOP, _MACOS])
def test_explicit_off_forces_no_map(engine):
    # MAP_HOST_USER=0 never maps, even on native Linux.
    assert _decide(*engine, "0") == "0"


@needs_bash
@_needs_lib
def test_auto_maps_only_on_native_linux():
    assert _decide(*_NATIVE, "") == "1"


@needs_bash
@_needs_lib
@pytest.mark.parametrize("engine", [_WSL, _DESKTOP, _MACOS])
def test_auto_skips_squashed_engines(engine):
    # WSL / Docker Desktop / macOS squash mount ownership → no mapping.
    assert _decide(*engine, "") == "0"


@needs_bash
@_needs_lib
def test_macos_never_auto_maps_regardless_of_docker_os():
    # The Darwin gate must win before docker_os is consulted: even a colima/lima
    # VM (reports a plain distro, not "Docker Desktop") must not auto-map, because
    # the macOS host uid != the daemon VM's uid.
    assert _decide("Darwin", "0", "Ubuntu 24.04", "") == "0"
    assert _decide("Darwin", "0", "unknown", "") == "0"


@needs_bash
@_needs_lib
def test_detect_is_wsl_echoes_zero_or_one():
    # Smoke: the WSL probe runs and returns a clean boolean on this host.
    out = subprocess.run(
        [_BASH, "-c", f'source "{_LIB}"\n_detect_is_wsl\n'],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() in ("0", "1")
