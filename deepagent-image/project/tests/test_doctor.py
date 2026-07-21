"""Tests for harness.doctor — pre-flight config validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from _bootstrap import _load

doctor = _load("harness.doctor")


def test_doctor_clean_workspace_no_errors(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 0


def test_doctor_workspace_with_env_file(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".env").write_text("SECRET=1")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 0


def test_doctor_no_errors_on_empty_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 0


def test_doctor_detect_malformed_mcp_json(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cwd = Path.cwd()
    mcp = cwd / ".mcp.json"
    mcp.write_text("{bad json", encoding="utf-8")
    try:
        rc = doctor.doctor_main([str(ws), str(state)])
        assert rc == 1
    finally:
        mcp.unlink(missing_ok=True)


def test_doctor_empty_workspace_no_keys_no_errors(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 0


def test_doctor_no_snapshot_mutation(tmp_path):
    """Regression: doctor must not write mask-snapshot.txt (snapshot=False)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".env").write_text("SECRET=1")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 0
    snap = state / "mask-snapshot.txt"
    assert not snap.is_file(), "doctor should not write snapshot"


def test_doctor_detects_floor_negation(tmp_path):
    """Regression: floor negation should produce error in doctor."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".env").write_text("SECRET=1")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agentignore").write_text("#!floor:\n.env\n", encoding="utf-8")
    ignore = ws / ".agentignore"
    ignore.write_text("!.env\n", encoding="utf-8")
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 1, "doctor should detect floor negation and return non-zero"
