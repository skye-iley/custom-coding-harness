"""Tests for harness.pathguard — traversal, sibling-escape, symlink-out."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from _bootstrap import _load

pg = _load("harness.pathguard")


def test_in_bounds_pass(tmp_path):
    base = tmp_path / "workspace"
    base.mkdir()
    target = base / "file.txt"
    target.write_text("content")
    result = pg.validate_path(str(target), str(base))
    assert result == os.path.realpath(str(target))


def test_traversal_refused(tmp_path):
    base = tmp_path / "workspace"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("leaked")
    target = base / "sub" / ".." / ".." / "secret.txt"
    target = target.resolve()
    with pytest.raises(pg.PathGuardDenied):
        pg.validate_path(str(target), str(base))


def test_absolute_path_workspace_outside_refused(tmp_path):
    base = tmp_path / "workspace"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("leaked")
    with pytest.raises(pg.PathGuardDenied):
        pg.validate_path(str(outside), str(base))


def test_sibling_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace-evil"
    sibling.mkdir()
    target = sibling / "file.txt"
    target.write_text("content")
    with pytest.raises(pg.PathGuardDenied):
        pg.validate_path(str(target), str(workspace))


@pytest.mark.skipif(sys.platform == "win32", reason="symlink privilege not available on Windows")
def test_symlink_out_refused(tmp_path):
    base = tmp_path / "workspace"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("leaked")
    link = base / "evil_link"
    link.symlink_to(os.path.relpath(str(outside), str(base)))
    with pytest.raises(pg.PathGuardDenied):
        pg.validate_path(str(link), str(base))


def test_path_guard_denied_carries_relpath(tmp_path):
    base = tmp_path / "workspace"
    base.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("leaked")
    try:
        pg.validate_path(str(outside), str(base))
    except pg.PathGuardDenied as e:
        assert e.relpath is not None
        assert e.target is not None


def test_validate_path_or_none_returns_none(tmp_path):
    assert pg.validate_path_or_none(None, "/tmp") is None


def test_validate_path_or_none_validates(tmp_path):
    base = tmp_path / "workspace"
    base.mkdir()
    target = base / "file.txt"
    target.write_text("content")
    result = pg.validate_path_or_none(str(target), str(base))
    assert result == os.path.realpath(str(target))


# --- relative_to (audit-trail helper, M4 slice D) --------------------------


def test_relative_to_computes_a_relpath():
    assert pg.relative_to("/workspace/sub/f.txt", "/workspace") == os.path.join("sub", "f.txt")


def test_relative_to_never_raises(monkeypatch):
    # Callers run inside an `except PathGuardDenied` block, where a second
    # exception would replace the PermissionError the tool layer expects. So the
    # inputs relpath genuinely rejects (another Windows drive, empty path) must
    # degrade to the absolute target rather than propagate.
    def _boom(*a, **k):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(os.path, "relpath", _boom)
    assert pg.relative_to("/outside/x", "/workspace") == "/outside/x"


def test_denied_relpath_survives_a_relpath_failure(monkeypatch):
    def _boom(*a, **k):
        raise ValueError("no common prefix")

    monkeypatch.setattr(os.path, "relpath", _boom)
    exc = pg.PathGuardDenied("/outside/x", "/workspace")
    assert exc.relpath == "/outside/x"
