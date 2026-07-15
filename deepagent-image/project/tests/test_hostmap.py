"""Unit tests for the host-uid mapping decision (scripts/lib/hostmap.sh).

Pure-shell decision, driven here through `bash -c` so it needs no live Docker
daemon. Asserts the full 3x3 matrix from IMMEDIATE_TODO.md:
    MAP_HOST_USER in {unset, "0", "1"}  x  engine in {native-linux, wsl, docker-desktop}
Self-skips on a host without bash (the lib uses bash [[ ]] / case).
"""
import pathlib
import shutil
import subprocess

import pytest

_BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(_BASH is None, reason="needs bash")

_LIB = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "lib" / "hostmap.sh"


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
@pytest.mark.parametrize("engine", [_NATIVE, _WSL, _DESKTOP, _MACOS])
def test_explicit_on_forces_map(engine):
    # MAP_HOST_USER=1 maps on every engine.
    assert _decide(*engine, "1") == "1"


@needs_bash
@pytest.mark.parametrize("engine", [_NATIVE, _WSL, _DESKTOP, _MACOS])
def test_explicit_off_forces_no_map(engine):
    # MAP_HOST_USER=0 never maps, even on native Linux.
    assert _decide(*engine, "0") == "0"


@needs_bash
def test_auto_maps_only_on_native_linux():
    assert _decide(*_NATIVE, "") == "1"


@needs_bash
@pytest.mark.parametrize("engine", [_WSL, _DESKTOP, _MACOS])
def test_auto_skips_squashed_engines(engine):
    # WSL / Docker Desktop / macOS squash mount ownership → no mapping.
    assert _decide(*engine, "") == "0"


@needs_bash
def test_macos_never_auto_maps_regardless_of_docker_os():
    # The Darwin gate must win before docker_os is consulted: even a colima/lima
    # VM (reports a plain distro, not "Docker Desktop") must not auto-map, because
    # the macOS host uid != the daemon VM's uid.
    assert _decide("Darwin", "0", "Ubuntu 24.04", "") == "0"
    assert _decide("Darwin", "0", "unknown", "") == "0"


@needs_bash
def test_detect_is_wsl_echoes_zero_or_one():
    # Smoke: the WSL probe runs and returns a clean boolean on this host.
    out = subprocess.run(
        [_BASH, "-c", f'source "{_LIB}"\n_detect_is_wsl\n'],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() in ("0", "1")
