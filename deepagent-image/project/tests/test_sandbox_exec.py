"""sandbox-exec.sh's AGENT_BIND_SCOPE parsing (M9, milestone9_invariants.md 3/9/10).

First direct unit coverage of sandbox-exec.sh -- previously exercised only live
(smoke -JailCheck) and referenced only as a string pattern in test_nsguard.py's
denylist. A stub `bwrap` placed first on PATH dumps its received argv to a file
instead of exec'ing, so the script's bind-scope loop can be asserted the same
argv-equality way jail.bwrap_args is -- never a substring check.

Host-runnable but needs a real `bash` on PATH (the script uses bash arrays and
`${var@Q}`, not POSIX sh) -- skips otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "sandbox-exec.sh"

_HAS_BASH = shutil.which("bash") is not None
needs_bash = pytest.mark.skipif(not _HAS_BASH, reason="needs a real bash")


@pytest.fixture
def bwrap_stub(tmp_path):
    """A fake `bwrap` first on PATH: logs argv (one per line) to BWRAP_STUB_LOG, exits 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "bwrap-argv.log"
    stub = bin_dir / "bwrap"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'for a in "$@"; do printf \'%s\\n\' "$a" >> "$BWRAP_STUB_LOG"; done\n'
        'exit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, log_path


def _run(bin_dir, log_path, ws, bind_scope=None, phase="exec"):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["AGENT_WORKSPACE"] = str(ws)
    env["BWRAP_STUB_LOG"] = str(log_path)
    if bind_scope is None:
        env.pop("AGENT_BIND_SCOPE", None)
    else:
        env["AGENT_BIND_SCOPE"] = bind_scope
    result = subprocess.run(
        ["bash", str(_SCRIPT), phase, "--", "true"],
        env=env,
        capture_output=True,
        text=True,
    )
    argv = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return result, argv


def _pairs(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag and i + 2 < len(argv)]


@needs_bash
def test_unset_bind_scope_emits_the_unmodified_single_workspace_bind(bwrap_stub, tmp_path):
    """Invariant 3: unset must not even evaluate the loop body."""
    bin_dir, log_path = bwrap_stub
    ws = tmp_path / "workspace"
    result, argv = _run(bin_dir, log_path, ws, bind_scope=None)

    assert result.returncode == 0, result.stderr
    assert (str(ws), str(ws)) in _pairs(argv, "--bind")


@needs_bash
def test_empty_bind_scope_is_treated_as_unset(bwrap_stub, tmp_path):
    bin_dir, log_path = bwrap_stub
    ws = tmp_path / "workspace"
    result, argv = _run(bin_dir, log_path, ws, bind_scope="")

    assert result.returncode == 0, result.stderr
    assert (str(ws), str(ws)) in _pairs(argv, "--bind")


@needs_bash
def test_bind_scope_emits_one_pair_per_entry_in_order(bwrap_stub, tmp_path):
    """Invariant 9: mirrors invariant 5 (per-entry mode) in shell."""
    bin_dir, log_path = bwrap_stub
    ws = tmp_path / "workspace"
    result, argv = _run(bin_dir, log_path, ws, bind_scope="src:rw,docs:ro")

    assert result.returncode == 0, result.stderr
    src, dst = f"{ws}/src", f"{ws}/docs"
    assert (src, src) in _pairs(argv, "--bind")
    assert (dst, dst) in _pairs(argv, "--ro-bind")
    # order preserved: the rw entry's flag precedes the ro entry's flag
    assert argv.index("--bind") < argv.index("--ro-bind", argv.index("--bind"))


@needs_bash
def test_malformed_entry_missing_colon_is_a_hard_failure(bwrap_stub, tmp_path):
    """Invariant 10: never a silently-dropped entry / fallback to the whole workspace."""
    bin_dir, log_path = bwrap_stub
    ws = tmp_path / "workspace"
    result, argv = _run(bin_dir, log_path, ws, bind_scope="src")

    assert result.returncode != 0
    assert argv == []  # bwrap (the stub) was never invoked


@needs_bash
def test_malformed_entry_empty_relpath_is_a_hard_failure(bwrap_stub, tmp_path):
    bin_dir, log_path = bwrap_stub
    ws = tmp_path / "workspace"
    result, argv = _run(bin_dir, log_path, ws, bind_scope=":rw")

    assert result.returncode != 0
    assert argv == []


@needs_bash
def test_malformed_entry_unknown_mode_is_a_hard_failure(bwrap_stub, tmp_path):
    bin_dir, log_path = bwrap_stub
    ws = tmp_path / "workspace"
    result, argv = _run(bin_dir, log_path, ws, bind_scope="src:rwx")

    assert result.returncode != 0
    assert argv == []
