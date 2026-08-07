"""Tests for harness.doctor — pre-flight config validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _bootstrap import _load

doctor = _load("harness.doctor")
# doctor imports seccomp lazily inside doctor_main; grab the same module object so
# monkeypatching profile_path() reaches the one doctor will actually call.
from harness import seccomp as doctor_seccomp  # noqa: E402


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


# --- state-dir isolation (M4 invariants 20 / 17a) --------------------------


def test_state_dir_inside_workspace_predicate(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert doctor.state_dir_inside_workspace(ws / ".deepagents", ws) is True
    assert doctor.state_dir_inside_workspace(ws, ws) is True
    assert doctor.state_dir_inside_workspace(tmp_path / "state", ws) is False


def test_state_dir_sibling_is_not_inside(tmp_path):
    # commonpath, not startswith: `<ws>-state` shares a string prefix with `<ws>`
    # but is a sibling, and must not be judged as inside it.
    ws = tmp_path / "workspace"
    ws.mkdir()
    sibling = tmp_path / "workspace-state"
    sibling.mkdir()
    assert doctor.state_dir_inside_workspace(sibling, ws) is False


def test_in_container_state_dir_inside_workspace_is_an_error(tmp_path, monkeypatch):
    # The DEEPAGENTS_STATE_DIR fallback (<workspace>/.deepagents) puts the M2
    # stores and the M4 denial log back in-bounds for the agent's file tools.
    # Both launchers set the var; nothing else asserted that they had to.
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    ws = tmp_path / "workspace"
    ws.mkdir()
    rc = doctor.doctor_main([str(ws), str(ws / ".deepagents")])
    assert rc == 1


def test_in_container_state_dir_outside_workspace_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    assert doctor.doctor_main([str(ws), str(state)]) == 0


def test_bare_host_state_dir_inside_workspace_is_not_an_error(tmp_path, monkeypatch):
    # Off-container `<workspace>/.deepagents` is the documented default layout
    # and there is no container boundary to protect — doctor must not fail every
    # legitimate bare-host run.
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert doctor.doctor_main([str(ws), str(ws / ".deepagents")]) == 0


# --- M4 slice H: jail / seccomp checks --------------------------------------


def test_jail_checks_are_skipped_when_the_jail_is_off(tmp_path, monkeypatch, capsys):
    """Off by default (invariant 35) -- doctor must not fail runs that never opted in."""
    monkeypatch.delenv("DEEPAGENTS_JAIL", raising=False)
    monkeypatch.chdir(tmp_path)

    rc = doctor.doctor_main([str(tmp_path / "ws"), str(tmp_path / "state")])

    assert "slice H checks skipped" in capsys.readouterr().err
    assert rc == 0


def test_jail_on_with_missing_seccomp_profile_is_an_error(tmp_path, monkeypatch, capsys):
    """Fail closed: the jail cannot be built without the profile, so say so loudly."""
    monkeypatch.setenv("DEEPAGENTS_JAIL", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_seccomp, "profile_path", lambda: tmp_path / "nope.json")

    rc = doctor.doctor_main([str(tmp_path / "ws"), str(tmp_path / "state")])

    assert "seccomp profile is missing" in capsys.readouterr().err
    assert rc == 1


def test_jail_on_with_widened_seccomp_profile_is_an_error(tmp_path, monkeypatch, capsys):
    """Invariant 31: a profile that drifted wider must fail doctor, hence CI."""
    monkeypatch.setenv("DEEPAGENTS_JAIL", "1")
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []}), encoding="utf-8"
    )
    monkeypatch.setattr(doctor_seccomp, "profile_path", lambda: bad)

    rc = doctor.doctor_main([str(tmp_path / "ws"), str(tmp_path / "state")])

    assert "seccomp profile:" in capsys.readouterr().err
    assert rc == 1


def test_jail_on_with_the_committed_profile_passes(tmp_path, monkeypatch, capsys):
    """The SECCOMP profile is clean, so doctor passes -- pinned against the host LSM.

    This asserts a property of the vendored seccomp profile, so it must not inherit
    whether the machine running the tests happens to be AppArmor-confined. Without
    the pin it passes on Docker Desktop and fails on any Ubuntu/Debian runner, where
    the (correct) AppArmor finding makes doctor non-zero -- which is the exact
    host-dependence that let slice H ship believing the jail was universally
    verified (milestone4.md §11.6). The AppArmor path has its own tests below.
    """
    from harness import jail as doctor_jail

    monkeypatch.setenv("DEEPAGENTS_JAIL", "1")
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)
    monkeypatch.delenv("DEEPAGENTS_JAIL_APPARMOR", raising=False)
    monkeypatch.setattr(doctor_jail, "apparmor_confinement", lambda: None)
    monkeypatch.chdir(tmp_path)

    rc = doctor.doctor_main([str(tmp_path / "ws"), str(tmp_path / "state")])

    err = capsys.readouterr().err
    assert "Docker's default plus exactly" in err
    assert rc == 0


# --- AppArmor / LSM pre-flight (M4 slice J, invariants 37/38) -----------------


def _jail_on(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_JAIL", "1")
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)


def _records(capsys):
    return capsys.readouterr().err


def test_doctor_errors_when_jail_is_on_under_apparmor(tmp_path, monkeypatch, capsys):
    """The failure CI hit: jail on, seccomp fine, AppArmor silently blocking mount."""
    from harness import jail as doctor_jail

    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _jail_on(monkeypatch)
    monkeypatch.delenv("DEEPAGENTS_JAIL_APPARMOR", raising=False)
    monkeypatch.setattr(doctor_jail, "apparmor_confinement", lambda: "docker-default")

    rc = doctor.doctor_main([str(ws), str(state)])
    err = _records(capsys)
    assert rc != 0
    assert "docker-default" in err
    assert "DEEPAGENTS_JAIL_APPARMOR=unconfined" in err
    # Must name AppArmor as the cause rather than implying the seccomp profile is wrong.
    assert "seccomp is not the problem" in err.lower()


def test_doctor_warns_but_passes_when_apparmor_deliberately_unconfined(
    tmp_path, monkeypatch, capsys
):
    from harness import jail as doctor_jail

    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _jail_on(monkeypatch)
    monkeypatch.setenv("DEEPAGENTS_JAIL_APPARMOR", "unconfined")
    monkeypatch.setattr(doctor_jail, "apparmor_confinement", lambda: None)

    rc = doctor.doctor_main([str(ws), str(state)])
    err = _records(capsys)
    # The operator asked for it, so it must not fail -- but it must never be silent:
    # this is a wider trade than the five relaxed syscalls the jail alone costs.
    assert rc == 0
    assert "AppArmor is disabled" in err
    assert "docker-default" in err


def test_doctor_quiet_when_no_lsm_in_force(tmp_path, monkeypatch, capsys):
    from harness import jail as doctor_jail

    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _jail_on(monkeypatch)
    monkeypatch.delenv("DEEPAGENTS_JAIL_APPARMOR", raising=False)
    monkeypatch.setattr(doctor_jail, "apparmor_confinement", lambda: None)

    rc = doctor.doctor_main([str(ws), str(state)])
    err = _records(capsys)
    assert rc == 0
    assert "no AppArmor confinement in force" in err


def test_doctor_skips_the_apparmor_check_entirely_when_jail_is_off(
    tmp_path, monkeypatch, capsys
):
    """Invariant 35: jail off must stay byte-for-byte A-G. No LSM finding at all."""
    from harness import jail as doctor_jail

    ws = tmp_path / "workspace"
    ws.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.delenv("DEEPAGENTS_JAIL", raising=False)
    monkeypatch.setattr(doctor_jail, "apparmor_confinement", lambda: "docker-default")

    rc = doctor.doctor_main([str(ws), str(state)])
    err = _records(capsys)
    assert rc == 0
    assert "AppArmor" not in err
