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
