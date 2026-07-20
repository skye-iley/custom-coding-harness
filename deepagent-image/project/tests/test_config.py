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
