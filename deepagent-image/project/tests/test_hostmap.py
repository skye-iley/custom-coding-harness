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
_needs_lib = pytest.mark.skipif(not _LIB.is_file(), reason="hostmap.sh not available (not in test image)")


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


# --- the decision has to reach EVERY container that touches a host-owned mount -

_RUN_DOCKER_SH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "run-docker.sh"
_needs_launcher = pytest.mark.skipif(
    not _RUN_DOCKER_SH.is_file(),
    reason="run-docker.sh not available (scripts/ is not COPYed into the image)",
)


def _joined_commands(text):
    r"""Collapse backslash-continued shell lines into one logical line each, so a
    multi-line `docker run ... \` reads as a single string. Normalizes CRLF
    first: the repo is edited on Windows, where the continuation is `\` + CRLF."""
    text = text.replace("\r\n", "\n")
    return [c.strip() for c in text.replace("\\\n", " ").splitlines()]


@_needs_launcher
def test_mask_scan_container_is_host_uid_mapped():
    """Regression: the mask-scan container mounted $STATE_HOST_DIR read-write but
    ran unmapped, so on a native-Linux engine it hit
    `[Errno 13] Permission denied: '/project/state/mask-snapshot.txt'` — the host
    user had just created that dir via mkdir -p, and the image's uid 10001 cannot
    write it. The launcher then fails CLOSED ("refusing to launch unmasked"), so
    masking being on by default made run-docker unusable on bare Linux. Invisible
    on Docker Desktop/WSL2, which squash mount ownership.

    The property: any container that mounts the host-owned state dir must carry
    the same uid mapping the agent container does."""
    commands = _joined_commands(_RUN_DOCKER_SH.read_text(encoding="utf-8"))
    scans = [c for c in commands if "docker run" in c and "mask-scan" in c]
    assert scans, "no mask-scan `docker run` found in run-docker.sh"
    for cmd in scans:
        assert "STATE_HOST_DIR" in cmd, "mask-scan no longer mounts the state dir — retarget this test"
        assert "USER_FLAGS" in cmd, (
            "mask-scan container is not host-uid mapped; it will EACCES on "
            "$STATE_HOST_DIR on a native-Linux engine"
        )


@_needs_launcher
def test_user_flags_is_applied_not_merely_computed():
    """USER_FLAGS is built once near the top; a container that never expands it
    silently ignores the whole hostmap decision."""
    text = _RUN_DOCKER_SH.read_text(encoding="utf-8")
    expansions = text.count('${USER_FLAGS[@]+"${USER_FLAGS[@]}"}')
    assert expansions >= 2, (
        f"USER_FLAGS expanded {expansions} time(s); expected the agent container "
        "AND the mask-scan container"
    )
