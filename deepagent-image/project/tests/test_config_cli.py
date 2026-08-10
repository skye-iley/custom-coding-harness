"""Tests for harness/config_cli.py (Milestone 5, C6/C7: `harness config` /
`harness config security` wizard). Host-runnable -- the module is stdlib +
harness.config + harness.providers only, no deepagents/langgraph/langchain
(see its module docstring), so unlike test_cli.py this needs no importorskip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _bootstrap import _load

cc = _load("harness.config_cli")
cfg = _load("harness.config")


def _input_sequence(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


# --- format_settings_lines (pure) ---------------------------------------------


def test_format_settings_lines_defaults():
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=Path("/nonexistent"), hitl_path=Path("/nonexistent")
    )
    lines = cc.format_settings_lines(settings, sources)
    joined = "\n".join(lines)
    assert "model" in joined and "(unset)" in joined
    assert "hitl" in joined and "off" in joined
    assert "--- pre-spinup" in joined
    assert "mask_mode" in joined and "deny" in joined


def test_format_settings_lines_with_hitl():
    hitl = cfg.HitlSection(autonomy_level="strict", on_deny="continue")
    settings = cfg.Settings(model="openai:gpt-5.5", hitl=hitl)
    sources = cfg.SettingsSources(model="cli", hitl="profile")
    lines = cc.format_settings_lines(settings, sources)
    joined = "\n".join(lines)
    assert "hitl.autonomy_level" in joined and "strict" in joined
    assert "hitl.on_deny" in joined and "continue" in joined


# --- numbered-choice / confirm prompt primitives ------------------------------


def test_numbered_choice_blank_picks_default(monkeypatch):
    _input_sequence(monkeypatch, [""])
    assert cc._numbered_choice("pick:", ["a", "b", "c"], default_index=1) == "b"


def test_numbered_choice_picks_by_number(monkeypatch):
    _input_sequence(monkeypatch, ["3"])
    assert cc._numbered_choice("pick:", ["a", "b", "c"]) == "c"


def test_numbered_choice_reprompts_on_invalid(monkeypatch, capsys):
    _input_sequence(monkeypatch, ["bogus", "9", "2"])
    assert cc._numbered_choice("pick:", ["a", "b", "c"]) == "b"
    assert "enter a number" in capsys.readouterr().out


def test_confirm_default_yes_on_blank(monkeypatch):
    _input_sequence(monkeypatch, [""])
    assert cc._confirm("ok?") is True


def test_confirm_explicit_no(monkeypatch):
    _input_sequence(monkeypatch, ["n"])
    assert cc._confirm("ok?", default=True) is False


# --- .agentignore quick-edit ---------------------------------------------------


def test_agentignore_add_pattern_creates_file(tmp_path):
    ws = tmp_path / "workspace"
    path = cc.agentignore_add_pattern(ws, "secrets/*.pem")
    assert path.read_text(encoding="utf-8").strip() == "secrets/*.pem"


def test_agentignore_add_pattern_appends(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".agentignore").write_text("existing.txt\n", encoding="utf-8")
    path = cc.agentignore_add_pattern(ws, "new.txt")
    text = path.read_text(encoding="utf-8")
    assert "existing.txt" in text and "new.txt" in text


def test_agentignore_add_floor_creates_block(tmp_path):
    ws = tmp_path / "workspace"
    path = cc.agentignore_add_floor(ws, "id_rsa")
    text = path.read_text(encoding="utf-8")
    assert "#!floor:" in text and "id_rsa" in text and "#!floor-end" in text
    # Round-trips through mask.py's own floor parser convention: id_rsa between
    # the markers, not before/after.
    lines = [l.strip() for l in text.splitlines()]
    start, end = lines.index("#!floor:"), lines.index("#!floor-end")
    assert start < lines.index("id_rsa") < end


def test_agentignore_add_floor_inserts_into_existing_block(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".agentignore").write_text(
        "src/\n#!floor:\nid_rsa\n#!floor-end\ntests/\n", encoding="utf-8"
    )
    path = cc.agentignore_add_floor(ws, ".aws/credentials")
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    start, end = lines.index("#!floor:"), lines.index("#!floor-end")
    assert start < lines.index("id_rsa") < end
    assert start < lines.index(".aws/credentials") < end
    # Non-floor lines untouched, in place.
    assert lines[0] == "src/"
    assert lines[-1] == "tests/"


def test_agentignore_add_floor_malformed_unclosed_block(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
    path = cc.agentignore_add_floor(ws, "new-secret")
    text = path.read_text(encoding="utf-8")
    assert "new-secret" in text  # not silently dropped


# --- HITL preset writer --------------------------------------------------------


def test_write_hitl_preset_creates_file(tmp_path):
    p = tmp_path / ".harness-config.yaml"
    cc.write_hitl_preset(p, "strict")
    conf = cfg.parse_config(p.read_text(encoding="utf-8"))
    assert conf.autonomy_level == "strict"


def test_write_hitl_preset_replaces_existing_line_preserves_rest(tmp_path):
    p = tmp_path / ".harness-config.yaml"
    p.write_text(
        "autonomy_level: guided\nreview_triggers:\n  - { on: path, pattern: \"*.env\" }\n",
        encoding="utf-8",
    )
    cc.write_hitl_preset(p, "strict")
    conf = cfg.parse_config(p.read_text(encoding="utf-8"))
    assert conf.autonomy_level == "strict"
    assert len(conf.review_triggers) == 1  # untouched


def test_write_hitl_preset_prepends_when_missing(tmp_path):
    p = tmp_path / ".harness-config.yaml"
    p.write_text("interruption_policy: blocking\n", encoding="utf-8")
    cc.write_hitl_preset(p, "guided")
    conf = cfg.parse_config(p.read_text(encoding="utf-8"))
    assert conf.autonomy_level == "guided"
    assert conf.interruption_policy == "blocking"


# --- show / set one-shots -------------------------------------------------------


def test_cmd_show_prints_resolved_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cc._cmd_show()
    assert rc == 0
    assert "model" in capsys.readouterr().out


def test_cmd_set_writes_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cc._cmd_set("jail", "true")
    assert rc == 0
    values = cfg.load_profile(tmp_path / cfg.PROFILE_NAME)
    assert values["jail"] is True
    assert "wrote" in capsys.readouterr().out


def test_cmd_set_unknown_field_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cc._cmd_set("thread_id", "x")  # valid Settings field, NOT a profile field
    assert rc == 1
    assert not (tmp_path / cfg.PROFILE_NAME).exists()
    assert "unknown/unsettable" in capsys.readouterr().out


def test_cmd_set_invalid_value_rolls_back(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / cfg.PROFILE_NAME
    cc._cmd_set("model", "openai:gpt-5.5")  # establish a baseline
    before = profile.read_text(encoding="utf-8")

    rc = cc._cmd_set("jail", "bogus-not-a-bool")
    assert rc == 1
    assert profile.read_text(encoding="utf-8") == before  # rolled back, not corrupted
    assert "expected a boolean" in capsys.readouterr().out


def test_cmd_set_invalid_value_no_prior_file_removed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cc._cmd_set("jail", "bogus")
    assert rc == 1
    assert not (tmp_path / cfg.PROFILE_NAME).exists()  # not left behind half-written


# --- wizard steps ----------------------------------------------------------------


def test_wizard_model_step_no_keys_returns_none(tmp_path, monkeypatch, capsys):
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    assert cc._wizard_model_step() is None
    assert "no provider API key detected" in capsys.readouterr().out


def test_wizard_model_step_picks_detected_provider(monkeypatch):
    detected = next(p for p in cc.PROVIDERS if p.default_model)
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    monkeypatch.setenv(detected.api_key_env, "sk-fake")
    # Only one provider detected => options = [detected.default_model, "(keep
    # current / auto-select)"]; blank picks the LAST option (auto-select is the
    # default), so "1" is needed to actually pick the detected provider.
    _input_sequence(monkeypatch, ["1"])
    assert cc._wizard_model_step() == detected.default_model


def test_wizard_model_step_blank_keeps_auto_select(monkeypatch):
    detected = next(p for p in cc.PROVIDERS if p.default_model)
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    monkeypatch.setenv(detected.api_key_env, "sk-fake")
    _input_sequence(monkeypatch, [""])
    assert cc._wizard_model_step() is None


def test_wizard_security_step_default_posture_is_noop(monkeypatch):
    _input_sequence(monkeypatch, [""])  # default posture
    assert cc._wizard_security_step() == {}


def test_wizard_security_step_hardened_sets_jail(monkeypatch):
    _input_sequence(monkeypatch, ["2"])  # hardened
    assert cc._wizard_security_step() == {"jail": True}


def test_wizard_security_step_custom_collects_all_fields(monkeypatch):
    _input_sequence(monkeypatch, [
        "3",       # custom posture
        "2",       # mask mode: allow
        "2",       # jail: on
        "",        # apparmor: blank => auto (not written)
        "4",       # cpus
        "8g",      # memory
        "1024",    # pids_limit
        "y",       # net_jail
    ])
    values = cc._wizard_security_step()
    assert values == {
        "mask_mode": "allow",
        "jail": True,
        "cpus": "4",
        "memory": "8g",
        "pids_limit": "1024",
        "net_jail": True,
    }
    assert "jail_apparmor" not in values  # blank input => not set


def test_wizard_hitl_step_off_returns_none(monkeypatch):
    _input_sequence(monkeypatch, [""])
    assert cc._wizard_hitl_step() is None


def test_wizard_hitl_step_strict(monkeypatch):
    _input_sequence(monkeypatch, ["3"])
    assert cc._wizard_hitl_step() == "strict"


# --- full wizard + dispatch -------------------------------------------------------


def test_run_wizard_refuses_without_tty(monkeypatch, capsys):
    monkeypatch.setattr(cc.sys.stdin, "isatty", lambda: False)
    rc = cc._run_wizard(security_only=False, auto_save=False)
    assert rc == 1
    assert "interactive terminal" in capsys.readouterr().out


def test_run_wizard_end_to_end_writes_profile_and_hitl(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc.sys.stdin, "isatty", lambda: True)
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    # No keys detected => _wizard_model_step prints a message and returns
    # WITHOUT calling input() at all, so the sequence below starts at the
    # security-posture prompt, not the model prompt.
    _input_sequence(monkeypatch, [
        "",   # security posture: default (blank => index 0)
        "2",  # hitl preset: guided
        "",   # save? [Y/n] blank => yes
    ])
    rc = cc._run_wizard(security_only=False, auto_save=False)
    assert rc == 0
    hitl = cfg.load_config(tmp_path / cfg.CONFIG_NAME)
    assert hitl is not None and hitl.autonomy_level == "guided"
    assert "saved" in capsys.readouterr().out


def test_run_wizard_auto_save_skips_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc.sys.stdin, "isatty", lambda: True)
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    _input_sequence(monkeypatch, [
        "1",  # security posture: default (no model/hitl prompts -- security_only)
        "",   # .agentignore quick-edit: skip
    ])
    rc = cc._run_wizard(security_only=True, auto_save=True)
    assert rc == 0
    # No "Save to ...? [Y/n]" prompt consumed an input -- auto_save=True skipped
    # it, proven by the sequence above being exactly exhausted with no
    # StopIteration (a 3rd queued input would go unused and be silently fine,
    # but a missing one would raise -- absence of that error is the assertion).


def test_config_main_dispatches_show(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cc.config_main(["show"])
    assert rc == 0
    assert "model" in capsys.readouterr().out


def test_config_main_dispatches_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cc.config_main(["set", "jail", "true"])
    assert rc == 0
    assert cfg.load_profile(tmp_path / cfg.PROFILE_NAME)["jail"] is True


def test_config_main_set_wrong_arity(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cc.config_main(["set", "jail"])
    assert rc == 1
    assert "usage" in capsys.readouterr().out
