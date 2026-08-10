"""Tests for harness/config.py (Milestone 3 slice S2, HITL config + gate matching).

Pure stdlib; config files written to pytest tmp_path.
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

cfg = _load("harness.config")


_SAMPLE = """\
autonomy_level: guided            # strict | guided | autonomous
review_triggers:
  - { on: path,    pattern: "*.env" }
  - { on: command, pattern: "rm -rf*" }
interruption_policy: blocking
system_interrupts:
  missing_price: true
  provider_error: true
  permission_denied: false
"""


# --- parsing -----------------------------------------------------------------

def test_parse_sample():
    c = cfg.parse_config(_SAMPLE)
    assert c.autonomy_level == "guided"
    assert c.interruption_policy == "blocking"
    assert len(c.review_triggers) == 2
    assert c.review_triggers[0].on == "path"
    assert c.review_triggers[0].pattern == "*.env"
    assert c.review_triggers[1].on == "command"
    assert c.review_triggers[1].pattern == "rm -rf*"
    assert c.system_interrupt_enabled("missing_price") is True
    assert c.system_interrupt_enabled("permission_denied") is False


def test_defaults_when_keys_omitted():
    c = cfg.parse_config("autonomy_level: strict\n")
    assert c.autonomy_level == "strict"
    assert c.interruption_policy == "blocking"  # default
    assert c.review_triggers == ()
    # system interrupts default on
    assert c.system_interrupt_enabled("provider_error") is True


def test_load_config_absent_returns_none(tmp_path):
    assert cfg.load_config(tmp_path / ".harness-config.yaml") is None
    assert cfg.find_config(tmp_path) is None


def test_load_config_present(tmp_path):
    p = tmp_path / ".harness-config.yaml"
    p.write_text(_SAMPLE, encoding="utf-8")
    c = cfg.find_config(tmp_path)
    assert c is not None and c.autonomy_level == "guided"


# --- validation (loud failures) ----------------------------------------------

def test_unknown_top_level_key_fails():
    with pytest.raises(SystemExit):
        cfg.parse_config("autonomy_levels: guided\n")  # typo


def test_bad_autonomy_level_fails():
    with pytest.raises(SystemExit):
        cfg.parse_config("autonomy_level: reckless\n")


def test_bad_policy_fails():
    with pytest.raises(SystemExit):
        cfg.parse_config("interruption_policy: sometimes\n")


def test_bad_trigger_target_fails():
    with pytest.raises(SystemExit):
        cfg.parse_config("review_triggers:\n  - { on: filename, pattern: x }\n")


def test_trigger_missing_pattern_fails():
    with pytest.raises(SystemExit):
        cfg.parse_config("review_triggers:\n  - { on: path }\n")


def test_unknown_system_interrupt_key_fails():
    with pytest.raises(SystemExit):
        cfg.parse_config("system_interrupts:\n  frobnicate: true\n")


# --- autonomy presets --------------------------------------------------------

def test_autonomy_presets():
    assert cfg.parse_config("autonomy_level: strict\n").gated_hooks() == frozenset(
        {"tool.start", "session.end"}
    )
    assert cfg.parse_config("autonomy_level: guided\n").gated_hooks() == frozenset(
        {"session.end"}
    )
    assert cfg.parse_config("autonomy_level: autonomous\n").gated_hooks() == frozenset()


# --- trigger matching (§6) ---------------------------------------------------

def test_match_path_trigger():
    c = cfg.parse_config(_SAMPLE)
    hit = cfg.match_triggers(c.review_triggers, paths=["src/app.py", "config/prod.env"])
    assert hit is not None and hit.on == "path"


def test_match_command_trigger():
    c = cfg.parse_config(_SAMPLE)
    hit = cfg.match_triggers(c.review_triggers, command="rm -rf /tmp/x")
    assert hit is not None and hit.on == "command"


def test_no_match_returns_none():
    c = cfg.parse_config(_SAMPLE)
    assert cfg.match_triggers(c.review_triggers, paths=["src/app.py"], command="ls -la") is None


def test_regex_pattern():
    c = cfg.parse_config('review_triggers:\n  - { on: command, pattern: "re:^sudo " }\n')
    assert cfg.match_triggers(c.review_triggers, command="sudo rm x") is not None
    assert cfg.match_triggers(c.review_triggers, command="pseudo cmd") is None


def test_tool_name_and_arg_targets():
    c = cfg.parse_config(
        "review_triggers:\n"
        "  - { on: tool_name, pattern: write_file }\n"
        "  - { on: arg, pattern: '*password*' }\n"
    )
    assert cfg.match_triggers(c.review_triggers, tool_name="write_file") is not None
    assert cfg.match_triggers(c.review_triggers, args=["set the password now"]) is not None
    assert cfg.match_triggers(c.review_triggers, tool_name="read_file") is None


# --- Milestone 5: Settings resolver ------------------------------------------


def test_resolve_settings_all_defaults(tmp_path):
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "missing.yaml", hitl_path=tmp_path / "missing-hitl.yaml"
    )
    assert settings.model is None and sources.model == "default"
    assert settings.thread_id.startswith("session-") and sources.thread_id == "default"
    assert settings.max_cost is None and sources.max_cost == "default"
    assert settings.mask_enabled is True and sources.mask_enabled == "default"
    assert settings.mask_mode == "deny" and sources.mask_mode == "default"
    assert settings.jail is False and sources.jail == "default"
    assert settings.cpus == "2" and sources.cpus == "default"
    assert settings.hitl is None and sources.hitl == "default"


def test_resolve_settings_precedence_cli_beats_env_beats_profile(tmp_path):
    profile = tmp_path / ".harness-profile.yaml"
    profile.write_text("model: profile-model\n", encoding="utf-8")

    # profile alone
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=profile, hitl_path=tmp_path / "no-hitl.yaml"
    )
    assert settings.model == "profile-model" and sources.model == "profile"

    # env beats profile
    settings, sources = cfg.resolve_settings(
        env={"DEEPAGENTS_MODEL": "env-model"}, profile_path=profile, hitl_path=tmp_path / "no-hitl.yaml"
    )
    assert settings.model == "env-model" and sources.model == "env"

    # cli beats env and profile
    settings, sources = cfg.resolve_settings(
        cli={"model": "cli-model"},
        env={"DEEPAGENTS_MODEL": "env-model"},
        profile_path=profile,
        hitl_path=tmp_path / "no-hitl.yaml",
    )
    assert settings.model == "cli-model" and sources.model == "cli"


def test_resolve_settings_precedence_every_field(tmp_path):
    """Every scalar field independently obeys cli > env > profile > default."""
    profile = tmp_path / ".harness-profile.yaml"
    profile.write_text(
        "topic: profile-topic\n"
        "max_cost: 1.5\n"
        "max_tokens: 100\n"
        "mask_mode: allow\n"
        "jail: true\n"
        "jail_apparmor: unconfined\n"
        "cpus: \"4\"\n"
        "memory: 8g\n"
        "pids_limit: \"1024\"\n"
        "net_jail: true\n",
        encoding="utf-8",
    )
    env = {
        "DEEPAGENTS_TOPIC": "env-topic",
        "DEEPAGENTS_MAX_COST": "2.5",
        "DEEPAGENTS_MAX_TOKENS": "200",
        "DEEPAGENTS_MASK_MODE": "deny",
        "DEEPAGENTS_JAIL": "0",
        "DEEPAGENTS_JAIL_APPARMOR": "deepagent-userns",
        "CPUS": "3",
        "MEMORY": "6g",
        "PIDS_LIMIT": "768",
        "NET_JAIL": "0",
    }
    cli = {"topic": "cli-topic", "max_cost": 9.9}

    settings, sources = cfg.resolve_settings(
        cli=cli, env=env, profile_path=profile, hitl_path=tmp_path / "no-hitl.yaml"
    )
    assert (settings.topic, sources.topic) == ("cli-topic", "cli")
    assert (settings.max_cost, sources.max_cost) == (9.9, "cli")
    assert (settings.max_tokens, sources.max_tokens) == (200, "env")
    assert (settings.mask_mode, sources.mask_mode) == ("deny", "env")
    assert (settings.jail, sources.jail) == (False, "env")
    assert (settings.jail_apparmor, sources.jail_apparmor) == ("deepagent-userns", "env")
    assert (settings.cpus, sources.cpus) == ("3", "env")
    assert (settings.memory, sources.memory) == ("6g", "env")
    assert (settings.pids_limit, sources.pids_limit) == ("768", "env")
    assert (settings.net_jail, sources.net_jail) == (False, "env")

    # Drop env entirely: profile tier surfaces.
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=profile, hitl_path=tmp_path / "no-hitl.yaml"
    )
    assert (settings.mask_mode, sources.mask_mode) == ("allow", "profile")
    assert (settings.jail, sources.jail) == (True, "profile")
    assert (settings.jail_apparmor, sources.jail_apparmor) == ("unconfined", "profile")
    assert (settings.cpus, sources.cpus) == ("4", "profile")
    assert (settings.net_jail, sources.net_jail) == (True, "profile")


def test_resolve_settings_hitl_whole_object(tmp_path):
    hitl_path = tmp_path / ".harness-config.yaml"
    hitl_path.write_text("autonomy_level: strict\n", encoding="utf-8")

    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "no-profile.yaml", hitl_path=hitl_path
    )
    assert settings.hitl is not None
    assert settings.hitl.autonomy_level == "strict"
    assert sources.hitl == "profile"


def test_resolve_settings_removable_contract_matches_pre_m5_defaults(tmp_path):
    """No profile, no CLI => same defaults _env_defaults()/load_config() produced pre-M5."""
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "missing.yaml", hitl_path=tmp_path / "missing-hitl.yaml"
    )
    assert settings.topic is None
    assert settings.headless is False
    assert settings.max_cost is None
    assert settings.max_tokens is None
    assert settings.hitl is None
    for name in ("model", "topic", "max_cost", "max_tokens", "headless", "hitl"):
        assert getattr(sources, name) in ("env", "default")


def test_live_fields_matches_milestone5_table():
    assert cfg.LIVE_FIELDS == frozenset(
        {"model", "thread_id", "topic", "max_cost", "max_tokens", "hitl"}
    )


# --- Milestone 5: profile file parsing/writing --------------------------------


def test_load_profile_absent_returns_empty_dict(tmp_path):
    assert cfg.load_profile(tmp_path / "missing.yaml") == {}


def test_load_profile_unknown_key_fails(tmp_path):
    p = tmp_path / ".harness-profile.yaml"
    p.write_text("modle: typo\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cfg.load_profile(p)


def test_load_profile_typed_fields(tmp_path):
    p = tmp_path / ".harness-profile.yaml"
    p.write_text("model: openai:gpt-5.5\njail: true\nmax_cost: 3.5\nmax_tokens: 1000\n", encoding="utf-8")
    values = cfg.load_profile(p)
    assert values == {"model": "openai:gpt-5.5", "jail": True, "max_cost": 3.5, "max_tokens": 1000}


def test_save_profile_merges_not_overwrites(tmp_path):
    p = tmp_path / ".harness-profile.yaml"
    cfg.save_profile(p, {"jail": True})
    cfg.save_profile(p, {"model": "openai:gpt-5.5"})
    values = cfg.load_profile(p)
    assert values["jail"] is True
    assert values["model"] == "openai:gpt-5.5"


def test_save_profile_unknown_key_fails(tmp_path):
    p = tmp_path / ".harness-profile.yaml"
    with pytest.raises(SystemExit):
        cfg.save_profile(p, {"thread_id": "nope"})  # not a profile field
