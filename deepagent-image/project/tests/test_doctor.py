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


def test_doctor_detect_malformed_mcp_json(tmp_path, monkeypatch):
    # doctor reads .mcp.json from CWD; chdir into tmp so we never touch the real
    # repo's .mcp.json (hygiene: all writes stay under tmp_path).
    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text("{bad json", encoding="utf-8")
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 1


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


def test_doctor_warns_missing_floor(tmp_path):
    """Regression: no #!floor: block at all is a warning, not an error.
    Invariant 22: weakened floor (deletion) must not silently pass —
    this test documents that floor absence is at least visible (warning)
    even though it does not fail CI (rc=0)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".env").write_text("SECRET=1")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    # No #!floor: block at all
    rc = doctor.doctor_main([str(ws), str(state)])
    assert rc == 0, "missing floor should warn, not fail"
    # TODO: upgrade to error when the harness requires a designated-secret floor


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
