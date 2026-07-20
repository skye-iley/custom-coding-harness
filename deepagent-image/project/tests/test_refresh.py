"""Tests for harness/refresh.py — the ephemeral-workspace live-refresh helper.

Pure stdlib (shutil/os/pathlib), so this runs on a bare interpreter via the
_bootstrap loader. All writes go to pytest's tmp_path (auto-removed); nothing
reaches the repo or a real workspace. Covers the mirror semantics (source wins,
agent-only files preserved), the .conda exclusion, subpath scoping, and the
escape / missing-source guards.
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

refresh = _load("harness.refresh")


# --- workspace_src ---------------------------------------------------------

def test_workspace_src_unset_is_none(monkeypatch):
    monkeypatch.delenv(refresh.WORKSPACE_SRC_ENV, raising=False)
    assert refresh.workspace_src() is None


def test_workspace_src_missing_dir_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv(refresh.WORKSPACE_SRC_ENV, str(tmp_path / "nope"))
    assert refresh.workspace_src() is None


def test_workspace_src_existing_dir_returns_path(monkeypatch, tmp_path):
    monkeypatch.setenv(refresh.WORKSPACE_SRC_ENV, str(tmp_path))
    assert refresh.workspace_src() == tmp_path


def test_workspace_src_file_is_none(monkeypatch, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setenv(refresh.WORKSPACE_SRC_ENV, str(f))
    assert refresh.workspace_src() is None  # must be a directory


# --- refresh_into: mirror semantics ----------------------------------------

def _mk(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_source_wins_on_conflict(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, "a.py", "from host")
    _mk(dst, "a.py", "agent edit")  # agent's in-flight edit to the same file

    written = refresh.refresh_into(dst, src)

    assert (dst / "a.py").read_text(encoding="utf-8") == "from host"
    assert "a.py" in written


def test_new_source_file_is_pulled_in(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, "sub/new.txt", "brand new")
    dst.mkdir()

    refresh.refresh_into(dst, src)

    assert (dst / "sub" / "new.txt").read_text(encoding="utf-8") == "brand new"


def test_agent_only_file_is_preserved(tmp_path):
    # A file the agent created this run (absent from source) must survive a
    # refresh — refresh pulls the source IN, it does not delete divergent work.
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, "shared.txt", "host")
    _mk(dst, "agent_only.txt", "keep me")

    refresh.refresh_into(dst, src)

    assert (dst / "agent_only.txt").read_text(encoding="utf-8") == "keep me"
    assert (dst / "shared.txt").read_text(encoding="utf-8") == "host"


def test_conda_dir_is_excluded(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, ".conda/env/pyvenv.cfg", "huge")
    _mk(src, "code.py", "real")
    dst.mkdir()

    written = refresh.refresh_into(dst, src)

    assert not (dst / ".conda").exists()
    assert (dst / "code.py").exists()
    assert all(".conda" not in w for w in written)


def test_returns_relative_paths(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, "a.txt", "1")
    _mk(src, "d/b.txt", "2")
    dst.mkdir()

    written = set(refresh.refresh_into(dst, src))

    # Normalize separators so the assertion holds on Windows and POSIX alike.
    assert {w.replace("\\", "/") for w in written} == {"a.txt", "d/b.txt"}


# --- refresh_into: subpath scoping -----------------------------------------

def test_subpath_single_file(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, "a.txt", "host-a")
    _mk(src, "b.txt", "host-b")
    _mk(dst, "b.txt", "agent-b")
    dst.mkdir(exist_ok=True)

    written = refresh.refresh_into(dst, src, "a.txt")

    assert (dst / "a.txt").read_text(encoding="utf-8") == "host-a"
    # b.txt was out of scope, so the agent's version is untouched.
    assert (dst / "b.txt").read_text(encoding="utf-8") == "agent-b"
    assert [w.replace("\\", "/") for w in written] == ["a.txt"]


def test_subpath_subdir(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src, "keep/x.txt", "host-x")
    _mk(src, "other/y.txt", "host-y")
    dst.mkdir()

    refresh.refresh_into(dst, src, "keep")

    assert (dst / "keep" / "x.txt").read_text(encoding="utf-8") == "host-x"
    assert not (dst / "other").exists()  # out of scope


def test_subpath_escape_raises(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    with pytest.raises(ValueError):
        refresh.refresh_into(dst, src, "../outside")


def test_subpath_missing_in_source_raises(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    with pytest.raises(FileNotFoundError):
        refresh.refresh_into(dst, src, "ghost.txt")
