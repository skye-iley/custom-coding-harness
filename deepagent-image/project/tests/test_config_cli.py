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


@pytest.fixture(autouse=True)
def _project_shaped_tmp_path(tmp_path):
    """Give every `tmp_path` a `providers/` dir.

    The write paths refuse to run from a cwd that isn't the harness project dir
    (detected by `providers/`), because a `.harness-profile.yaml` written
    anywhere else is one `run-docker` never mounts. Tests that `chdir(tmp_path)`
    are standing in for that directory, so they have to look like it."""
    (tmp_path / "providers").mkdir(exist_ok=True)


def _no_provider_detected(monkeypatch):
    """Force `_wizard_model_step` down its "nothing detected" branch.

    Clearing the API-key env vars is no longer sufficient: ollama is keyless and
    carries a `default_model`, so the shipped registry always detects it (that is
    the point -- it is the auto-select default). Drop keyless providers from the
    list as well to reach the no-detection path.
    """
    keyed = [p for p in cc.PROVIDERS if p.requires_key]
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    monkeypatch.setattr(cc, "PROVIDERS", keyed)


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
    _no_provider_detected(monkeypatch)
    assert cc._wizard_model_step() is None
    assert "no provider API key detected" in capsys.readouterr().out


def test_wizard_model_step_offers_keyless_provider_without_keys(monkeypatch):
    """A keyless provider with a default_model is offered with no keys set.

    Regression guard: the wizard gated on `os.getenv(api_key_env)` like
    choose_model did, so it would report "no provider API key detected" on a
    host whose only configured model is a local ollama one -- and then advise
    auto-select, which would have picked ollama anyway. The two now agree.
    """
    for p in cc.PROVIDERS:
        monkeypatch.delenv(p.api_key_env, raising=False)
    _input_sequence(monkeypatch, ["1"])  # pick the first (only) detected option
    assert cc._wizard_model_step() == "ollama:gemma4"


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


def test_wizard_security_step_default_posture_writes_values_explicitly(monkeypatch):
    # Regression: an empty diff here left a previously-saved `jail: true` in the
    # profile untouched, so the wizard reported "jail off" and the next run came
    # up jailed. A picked posture must write the posture it names.
    _input_sequence(monkeypatch, [""])  # default posture
    assert cc._wizard_security_step() == {"mask_mode": "deny", "jail": False}


def test_wizard_security_step_hardened_sets_jail(monkeypatch):
    _input_sequence(monkeypatch, ["2"])  # hardened
    assert cc._wizard_security_step() == {"mask_mode": "deny", "jail": True}


def test_wizard_default_posture_overwrites_saved_jail(tmp_path, monkeypatch):
    """The end-to-end shape of the regression above: a profile with jail on,
    then a 'default' posture run, must leave jail off on disk."""
    monkeypatch.chdir(tmp_path)
    cfg.save_profile(tmp_path / cfg.PROFILE_NAME, {"jail": True})
    assert cfg.load_profile(tmp_path / cfg.PROFILE_NAME)["jail"] is True

    monkeypatch.setattr(cc.sys.stdin, "isatty", lambda: True, raising=False)
    _input_sequence(monkeypatch, [
        "",   # model: keep current / auto-select (or skipped if no keys)
        "",   # security posture: default
        "",   # HITL preset: off
    ])
    cc._run_wizard(security_only=True, auto_save=True)
    assert cfg.load_profile(tmp_path / cfg.PROFILE_NAME)["jail"] is False


def test_wizard_security_step_custom_collects_all_fields(monkeypatch):
    _input_sequence(monkeypatch, [
        "3",       # custom posture
        "2",       # mask mode: allow
        "2",       # jail: on
        "",        # apparmor: blank => auto (not written)
        "",        # systempaths: blank => the starred default (unconfined)
        "4",       # cpus
        "8g",      # memory
        "1024",    # pids_limit
        "y",       # net_jail
    ])
    values = cc._wizard_security_step()
    assert values == {
        "mask_mode": "allow",
        "jail": True,
        # An enum menu always answers (unlike the free-text apparmor prompt), so the
        # blank picks the starred default and it IS written -- which matches what the
        # launchers do under the jail anyway (M4.1 fork J5).
        "jail_systempaths": "unconfined",
        "cpus": "4",
        "memory": "8g",
        "pids_limit": "1024",
        "net_jail": True,
    }
    assert "jail_apparmor" not in values  # blank input => not set


def test_wizard_hitl_step_off_returns_off_not_none(monkeypatch):
    # Regression: "off" used to return None, indistinguishable from "screen
    # skipped", so _run_wizard printed "hitl: off" and left an existing
    # .harness-config.yaml in place -- HITL stayed on.
    _input_sequence(monkeypatch, [""])
    assert cc._wizard_hitl_step() == "off"


def test_disable_hitl_moves_file_aside(tmp_path):
    path = tmp_path / cfg.CONFIG_NAME
    path.write_text("autonomy_level: strict\nreview_triggers:\n  - { on: path, pattern: \"*.env\" }\n")
    moved = cc.disable_hitl(path)
    assert moved is not None and moved.name.endswith(".disabled")
    assert not path.exists()          # presence-of-file IS the switch: HITL now off
    assert "review_triggers" in moved.read_text()  # hand-edited block preserved, not deleted
    assert cfg.load_config(path) is None


def test_disable_hitl_absent_file_is_noop(tmp_path):
    assert cc.disable_hitl(tmp_path / cfg.CONFIG_NAME) is None


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
    _no_provider_detected(monkeypatch)
    # No provider detected => _wizard_model_step prints a message and returns
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
    # Isolate from the real repo's netjail/ dir -- point at an empty tmp dir so
    # the netjail step's "not found" branch fires with no prompt consumed.
    monkeypatch.setattr(cc, "netjail_dir", lambda: tmp_path / "no-netjail-here")
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


# --- NetJail list editor (harness config security) -----------------------------


def test_netjail_dir_resolves_relative_to_module():
    # Portable across host-checkout and in-container layouts: netjail_dir() is
    # always three parents up from this file (harness/ -> project/ -> its
    # parent), whatever that parent is named. The host checkout names it
    # "deepagent-image"; a container's flattened /project has no such sibling
    # at all (netjail/ isn't COPYed in) -- that asymmetry is exactly why the
    # wizard step checks .is_dir() rather than assuming the path is valid.
    expected = cc.Path(cc.__file__).resolve().parent.parent.parent / "netjail"
    assert cc.netjail_dir() == expected
    assert cc.netjail_dir().name == "netjail"


def test_netjail_list_entries_skips_comments_and_blanks(tmp_path):
    p = tmp_path / "host-services.txt"
    p.write_text("# comment\n\nollama 11434\n  \n# another\nredis 6379\n", encoding="utf-8")
    assert cc.netjail_list_entries(p) == ["ollama 11434", "redis 6379"]


def test_netjail_list_entries_absent_file_is_empty(tmp_path):
    assert cc.netjail_list_entries(tmp_path / "missing.txt") == []


def test_netjail_add_entry_appends(tmp_path):
    p = tmp_path / "allowed-domains.txt"
    p.write_text("# header\nexample.com\n", encoding="utf-8")
    cc.netjail_add_entry(p, "api.github.com")
    assert cc.netjail_list_entries(p) == ["example.com", "api.github.com"]


def test_netjail_add_entry_creates_file(tmp_path):
    p = tmp_path / "sub" / "allowed-domains.txt"
    cc.netjail_add_entry(p, "example.com")
    assert cc.netjail_list_entries(p) == ["example.com"]


def test_netjail_remove_entry_by_index_preserves_comments(tmp_path):
    p = tmp_path / "host-services.txt"
    p.write_text("# header\nollama 11434\n# note\nredis 6379\n", encoding="utf-8")
    removed = cc.netjail_remove_entry(p, 0)
    assert removed == "ollama 11434"
    text = p.read_text(encoding="utf-8")
    assert "# header" in text and "# note" in text
    assert cc.netjail_list_entries(p) == ["redis 6379"]


def test_netjail_remove_entry_out_of_range_returns_none(tmp_path):
    p = tmp_path / "host-services.txt"
    p.write_text("ollama 11434\n", encoding="utf-8")
    assert cc.netjail_remove_entry(p, 5) is None
    assert cc.netjail_list_entries(p) == ["ollama 11434"]  # untouched


def test_netjail_remove_entry_absent_file_returns_none(tmp_path):
    assert cc.netjail_remove_entry(tmp_path / "missing.txt", 0) is None


def test_wizard_netjail_step_missing_dir_prints_and_returns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cc, "netjail_dir", lambda: tmp_path / "nope")
    cc._wizard_netjail_step()  # no input() call should happen
    assert "not found" in capsys.readouterr().out


def test_wizard_netjail_step_add_then_delete(tmp_path, monkeypatch, capsys):
    net_dir = tmp_path / "netjail"
    net_dir.mkdir()
    monkeypatch.setattr(cc, "netjail_dir", lambda: net_dir)
    _input_sequence(monkeypatch, [
        "2",             # host-services.txt
        "2",             # action: add
        "ollama 11434",  # new entry
        "2",             # host-services.txt again
        "3",             # action: delete
        "1",             # delete entry #1
        "2",             # host-services.txt again
        "1",             # action: back
        "1",             # done
    ])
    cc._wizard_netjail_step()
    assert cc.netjail_list_entries(net_dir / "host-services.txt") == []
    out = capsys.readouterr().out
    assert "added 'ollama 11434'" in out
    assert "removed 'ollama 11434'" in out


# --- write paths refuse a cwd run-docker never reads (F10) ---------------------


def test_cmd_set_refuses_outside_the_project_dir(tmp_path, monkeypatch, capsys):
    """A profile written from the repo root is one run-docker never mounts, so
    `harness config set` there would report success and change nothing."""
    elsewhere = tmp_path / "repo-root"
    elsewhere.mkdir()  # deliberately no providers/
    monkeypatch.chdir(elsewhere)
    rc = cc._cmd_set("jail", "true")
    assert rc == 1
    assert "refusing to write" in capsys.readouterr().out
    assert not (elsewhere / cfg.PROFILE_NAME).exists()


# --- .agentignore quick-edit is mode-aware (F3) --------------------------------


def _agentignore_step_in(monkeypatch, workspace, answers):
    monkeypatch.setenv("AGENT_WORKSPACE", str(workspace))
    monkeypatch.delenv("DEEPAGENTS_MASK_MODE", raising=False)
    _input_sequence(monkeypatch, answers)


def test_agentignore_step_warns_and_relabels_in_allow_mode(tmp_path, monkeypatch, capsys):
    """In allow mode a plain pattern is the ALLOW-list entry, not a mask -- so
    "add a path to mask" would make a secret visible. The step has to name that
    before the operator can type a path into it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".agentignore").write_text("#!mode:allow\nsrc/\n", encoding="utf-8")
    _agentignore_step_in(monkeypatch, workspace, [
        "2",                    # the plain-pattern option
        "config/secrets.yaml",  # pattern
        "n",                    # add another? no
    ])
    applied = cc._wizard_agentignore_step()
    out = capsys.readouterr().out
    assert "allow mode" in out
    assert "VISIBLE" in out
    assert "add a path to ALLOW" in out
    assert applied and "config/secrets.yaml" in applied[0]


def test_agentignore_step_unchanged_in_deny_mode(tmp_path, monkeypatch, capsys):
    """The allow-mode warning must not leak into the deny path, and the appended
    line must land exactly as it did before the fix."""
    monkeypatch.chdir(tmp_path)  # no .harness-profile.yaml here => mask_mode default
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _agentignore_step_in(monkeypatch, workspace, [
        "2",
        "config/secrets.yaml",
        "n",
    ])
    cc._wizard_agentignore_step()
    out = capsys.readouterr().out
    assert "allow mode" not in out
    assert "VISIBLE" not in out
    assert "add a path to mask" in out
    assert (workspace / ".agentignore").read_text(encoding="utf-8") == "config/secrets.yaml\n"


def test_agentignore_effective_mode_reads_header_over_setting(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("DEEPAGENTS_MASK_MODE", "deny")
    (workspace / ".agentignore").write_text("#!mode:allow\n", encoding="utf-8")
    assert cc.agentignore_effective_mode(workspace) == "allow"


def test_agentignore_effective_mode_falls_back_to_setting(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPAGENTS_MASK_MODE", "allow")
    (workspace / ".agentignore").write_text("src/\n", encoding="utf-8")  # no header
    assert cc.agentignore_effective_mode(workspace) == "allow"


# --- side-effect edits are named in the summary (F9) ---------------------------


def test_wizard_decline_still_names_applied_netjail_edits(tmp_path, monkeypatch, capsys):
    """`.agentignore`/NetJail edits are written the moment they are answered, so
    declining the profile save must not leave them silent and unmentioned."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    net_dir = tmp_path / "netjail"
    net_dir.mkdir()
    monkeypatch.setattr(cc, "netjail_dir", lambda: net_dir)
    _input_sequence(monkeypatch, [
        "1",             # security posture: default
        "1",             # .agentignore quick-edit: skip
        "2",             # netjail: host-services.txt
        "2",             # action: add
        "ollama 11434",  # new entry
        "1",             # netjail: done
        "n",             # save to profile? NO
    ])
    rc = cc._run_wizard(security_only=True, auto_save=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Already applied (not part of the profile save)" in out
    assert "ollama 11434" in out
    assert "profile not saved" in out
    # ...and the edit really is on disk, which is the whole point of saying so.
    assert cc.netjail_list_entries(net_dir / "host-services.txt") == ["ollama 11434"]
    assert not (tmp_path / cfg.PROFILE_NAME).exists()


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


# =============================================================================
# Milestone 5.1: wizard screens + `harness config set` derive from the registry
# =============================================================================


def test_ask_field_renders_an_enum_as_a_numbered_menu(monkeypatch, capsys):
    _input_sequence(monkeypatch, ["2"])
    assert cc._ask_field(cfg.SPECS_BY_NAME["mask_mode"]) == "allow"
    out = capsys.readouterr().out
    assert "Mask mode:" in out and "1) deny" in out and "2) allow" in out


def test_ask_field_renders_a_bool_as_an_off_on_menu(monkeypatch):
    _input_sequence(monkeypatch, ["2"])
    # The answer goes back through the field's own cast, so "on" becomes a real
    # bool by the same _TRUTHY rule every other tier uses.
    assert cc._ask_field(cfg.SPECS_BY_NAME["jail"]) is True


def test_ask_field_text_prompt_carries_its_default(monkeypatch, capsys):
    _input_sequence(monkeypatch, [""])
    assert cc._ask_field(cfg.SPECS_BY_NAME["cpus"]) == "2"
    _input_sequence(monkeypatch, ["8"])
    assert cc._ask_field(cfg.SPECS_BY_NAME["cpus"]) == "8"


def test_ask_field_unset_default_renders_blank_is_auto(monkeypatch):
    prompts = []

    def record(prompt=""):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", record)
    # default=None => blank means "not written", and the caller drops it.
    assert cc._ask_field(cfg.SPECS_BY_NAME["jail_apparmor"]) is None
    assert prompts == ["AppArmor profile (blank = auto): "]


def test_ask_field_confirm_shape(monkeypatch):
    _input_sequence(monkeypatch, ["y"])
    assert cc._ask_field(cfg.SPECS_BY_NAME["net_jail"]) is True


def test_custom_posture_asks_every_persisted_prespinup_knob(monkeypatch):
    """The DoD's "a new choice-typed knob appears in the wizard for free": add a
    spec to the registry and the custom screen asks about it, with no edit to
    `_wizard_security_step`."""
    extra = cfg.FieldSpec(
        name="mask_mode", tier="prespinup", env_var="X", profile_key="mask_mode",
        default="deny", choices=("deny", "allow"), label="Mask mode",
    )
    new = cfg.FieldSpec(
        name="cpus", tier="prespinup", env_var="CPUS", profile_key="cpus",
        default="2", label="CPU limit",
    )
    monkeypatch.setattr(cc, "WIZARD_PRESPINUP_SPECS", (extra, new))
    _input_sequence(monkeypatch, ["3", "2", "16"])  # custom, mask_mode=allow, cpus=16
    assert cc._wizard_security_step() == {"mask_mode": "allow", "cpus": "16"}


def test_cmd_set_rejects_an_invalid_enum_value(tmp_path, monkeypatch, capsys):
    """The §3.1 gap, closed. Before M5.1 this returned 0 and persisted
    `mask_mode=definitely-not-a-mode`, which then silently resolved to deny."""
    monkeypatch.chdir(tmp_path)
    rc = cc._cmd_set("mask_mode", "definitely-not-a-mode")
    assert rc == 1
    assert not (tmp_path / cfg.PROFILE_NAME).exists()  # not left half-written
    assert "must be one of" in capsys.readouterr().out


def test_cmd_set_invalid_enum_rolls_back_a_prior_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cc._cmd_set("mask_mode", "allow")
    before = (tmp_path / cfg.PROFILE_NAME).read_text(encoding="utf-8")
    assert cc._cmd_set("mask_mode", "alow") == 1
    assert (tmp_path / cfg.PROFILE_NAME).read_text(encoding="utf-8") == before


def test_cmd_set_accepts_every_declared_choice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for value in cfg.SPECS_BY_NAME["mask_mode"].choices:
        assert cc._cmd_set("mask_mode", value) == 0
        assert cfg.load_profile(tmp_path / cfg.PROFILE_NAME)["mask_mode"] == value


def test_format_settings_lines_is_the_shared_renderer(tmp_path):
    """R3: one renderer, not two. `harness config show`'s output is
    `format_config_lines` at its default prefix/width."""
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml"
    )
    assert cc.format_settings_lines(settings, sources) == cfg.format_config_lines(settings, sources)
