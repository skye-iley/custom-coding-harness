"""Tests for harness/config.py (Milestone 3 slice S2, HITL config + gate matching).

Pure stdlib; config files written to pytest tmp_path.
"""

from __future__ import annotations

from pathlib import Path

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


def test_resolve_settings_jail_from_profile_with_no_env(tmp_path):
    """The container-side half of the F1 contract.

    `harness config` -> "hardened" writes `jail: true` to the profile and no
    DEEPAGENTS_JAIL anywhere. Settings must still resolve jail True from the
    profile tier -- the launcher then has to forward it as `-e DEEPAGENTS_JAIL=1`,
    because `jail.jail_enabled()` reads the environment and never consults
    Settings. Without that forward the seccomp relaxation lands and the jail
    doesn't, which is strictly worse than jail-off.
    """
    profile = tmp_path / ".harness-profile.yaml"
    profile.write_text("jail: true\n", encoding="utf-8")

    settings, sources = cfg.resolve_settings(
        env={}, profile_path=profile, hitl_path=tmp_path / "no-hitl.yaml"
    )
    assert settings.jail is True
    assert sources.jail == "profile"


# --- BOM tolerance (F2) -------------------------------------------------------


def test_load_config_tolerates_utf8_bom(tmp_path):
    """A BOM-prefixed .harness-config.yaml parses instead of dying on key 1.

    Windows PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM, as does
    Notepad; read with plain "utf-8" the first key becomes "﻿autonomy_level"
    and parse_config SystemExits on the unknown-key branch, so the harness
    refuses to start.
    """
    path = tmp_path / ".harness-config.yaml"
    path.write_text("﻿autonomy_level: strict\n", encoding="utf-8")

    section = cfg.load_config(path)
    assert section is not None
    assert section.autonomy_level == "strict"


def test_load_profile_tolerates_utf8_bom(tmp_path):
    path = tmp_path / ".harness-profile.yaml"
    path.write_text("﻿model: x\n", encoding="utf-8")

    assert cfg.load_profile(path) == {"model": "x"}


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
    # milestone5.md §3's table, plus `raw_trace` (M7 §10.1 -- live because the
    # operator case is flipping tracing on and re-running the same prompt in the
    # SAME session, against the same thread and accumulated context) and M8 B1's
    # three hard stops (read by the harness process itself, not by `docker run`,
    # so they are live by the same test §3's table applies).
    assert cfg.LIVE_FIELDS == frozenset(
        {
            "model", "thread_id", "topic", "max_cost", "max_tokens", "hitl",
            "raw_trace", "max_steps", "max_seconds", "max_turns",
        }
    )


# --- Milestone 5: profile file parsing/writing --------------------------------


def test_load_profile_absent_returns_empty_dict(tmp_path):
    assert cfg.load_profile(tmp_path / "missing.yaml") == {}


def test_load_profile_unknown_key_fails(tmp_path):
    p = tmp_path / ".harness-profile.yaml"
    p.write_text("modle: typo\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cfg.load_profile(p)


def test_load_profile_comment_only_value_is_unset(tmp_path):
    """A `key:   # note` line (used throughout .harness-profile.yaml.example for
    every unset key) must resolve as absent, not as a literal string starting
    with '#' -- regression for a bug where _strip_comment only trims a
    *trailing* comment off a real value, not a value that IS a comment."""
    p = tmp_path / ".harness-profile.yaml"
    p.write_text("model:   # e.g. openai:gpt-5.5\njail: true\n", encoding="utf-8")
    values = cfg.load_profile(p)
    assert "model" not in values
    assert values == {"jail": True}


def test_example_profile_file_parses_cleanly():
    """The checked-in .harness-profile.yaml.example must itself be valid --
    every unset key blank, no stray comment text leaking into a value."""
    example = Path(__file__).resolve().parent.parent / ".harness-profile.yaml.example"
    values = cfg.load_profile(example)
    assert set(values) <= cfg.PROFILE_FIELDS
    for v in values.values():
        assert not (isinstance(v, str) and v.startswith("#"))


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


def test_save_profile_falls_back_to_in_place_write_when_rename_fails(tmp_path, monkeypatch):
    """Regression: run-docker bind-mounts .harness-profile.yaml into the
    container as a single-file mount, and renaming over a mount point fails with
    EBUSY -- so the atomic tmp+replace write made `/config save` crash in the
    exact deployment the mount exists for. Fall back to writing in place."""
    path = tmp_path / cfg.PROFILE_NAME
    cfg.save_profile(path, {"topic": "before"})

    real_replace = Path.replace

    def refuse_replace(self, target):
        if str(target).endswith(cfg.PROFILE_NAME):
            raise OSError(16, "Device or resource busy")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", refuse_replace)
    cfg.save_profile(path, {"topic": "after"})

    assert cfg.load_profile(path)["topic"] == "after"
    assert not (tmp_path / (cfg.PROFILE_NAME + ".tmp")).exists()  # scratch cleaned up


# =============================================================================
# Milestone 5.1: the field registry is the single declaration
# =============================================================================
#
# These are the guards that make every later derivation safe: if a structure
# that USED to be hand-written gets hand-written again, one of these fails.


def _scalar_specs():
    """Registry specs that correspond 1:1 to a `Settings` attribute (i.e. not
    the dotted `hitl.*` sub-fields, which live in the other config file)."""
    return tuple(s for s in cfg.FIELD_SPECS if "." not in s.name)


def test_settings_dataclass_exactly_matches_the_registry():
    """R1's load-bearing guard. `Settings` stays an explicit frozen dataclass
    (fork 1: static types + `dataclasses.fields()` introspection cli.py depends
    on), so a test -- not codegen -- has to hold the two in step. Order matters:
    the display renderers iterate the registry, so a reorder there silently
    reorders `/config`'s output."""
    import dataclasses

    registry = tuple(s.name for s in _scalar_specs())
    assert tuple(f.name for f in dataclasses.fields(cfg.Settings)) == registry
    assert tuple(f.name for f in dataclasses.fields(cfg.SettingsSources)) == registry


def test_profile_field_set_and_write_order_are_derived():
    assert cfg.PROFILE_FIELDS == frozenset(
        s.profile_key for s in cfg.FIELD_SPECS if s.profile_key
    )
    assert cfg._PROFILE_WRITE_ORDER == tuple(
        s.profile_key for s in cfg.FIELD_SPECS if s.profile_key
    )
    # The M5 exclusions, still excluded -- each for the reason recorded in its
    # own spec comment rather than a module-level note three hundred lines away.
    for name in ("thread_id", "headless", "mask_enabled", "hitl"):
        assert cfg.SPECS_BY_NAME[name].profile_key is None
        assert name not in cfg.PROFILE_FIELDS


def test_live_fields_is_derived_from_tier():
    assert cfg.LIVE_FIELDS == frozenset(
        s.name for s in cfg.FIELD_SPECS if s.tier == "live" and "." not in s.name
    )


def test_every_scalar_spec_is_walked_by_the_resolver():
    """The resolver loop keys on `env_var is not None`; the only specs without
    one must be the whole-object HITL tier."""
    unwalked = [s.name for s in cfg.FIELD_SPECS if s.env_var is None]
    assert unwalked == ["hitl", "hitl.autonomy_level", "hitl.on_deny", "hitl.interruption_policy"]


def test_registry_entries_are_internally_coherent():
    for spec in cfg.FIELD_SPECS:
        assert spec.tier in ("live", "prespinup"), spec.name
        assert spec.label, f"{spec.name} needs a label for the wizard/picker"
        if spec.profile_key is not None:
            # The two names are kept equal on purpose: `resolve_settings` looks
            # the profile value up by key and assigns it by name.
            assert spec.profile_key == spec.name
        if spec.choices is not None:
            # `choices` means "exactly these strings are legal" -- it drives
            # validation, so it must never sit on a lenient cast like _to_bool
            # (which accepts 1/true/on and would reject the launchers' spelling).
            assert spec.cast is str, spec.name
            assert spec.default is None or spec.default in spec.choices, spec.name
        if "." in spec.name:
            assert spec.env_var is None and spec.profile_key is None, spec.name


# --- enum validation, the one sanctioned behaviour change (§3.1) --------------


def test_profile_rejects_an_invalid_enum_value(tmp_path):
    """M5 shipped this as a known bug: `mask_mode: alow` parsed (str cast),
    persisted, and resolved -- and `mask.resolve` then took the `else` branch,
    silently yielding **deny**. Fail-safe but silent; now loud."""
    p = tmp_path / cfg.PROFILE_NAME
    p.write_text("mask_mode: alow\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="must be one of"):
        cfg.load_profile(p)


def test_save_profile_rejects_an_invalid_enum_before_writing(tmp_path):
    """The shared writer refuses too, so no writer -- wizard, `/config save`, or
    `harness config set` -- can produce a file `load_profile` would then refuse."""
    p = tmp_path / cfg.PROFILE_NAME
    with pytest.raises(SystemExit, match="must be one of"):
        cfg.save_profile(p, {"mask_mode": "alow"})
    assert not p.exists()  # nothing written, not written-then-rolled-back


def test_profile_accepts_every_declared_choice(tmp_path):
    for value in cfg.SPECS_BY_NAME["mask_mode"].choices:
        p = tmp_path / cfg.PROFILE_NAME
        p.write_text(f"mask_mode: {value}\n", encoding="utf-8")
        assert cfg.load_profile(p)["mask_mode"] == value


def test_env_rejects_an_invalid_enum_value(tmp_path):
    with pytest.raises(SystemExit, match="DEEPAGENTS_MASK_MODE: mask_mode must be one of"):
        cfg.resolve_settings(
            env={"DEEPAGENTS_MASK_MODE": "alow"},
            profile_path=tmp_path / "none.yaml",
            hitl_path=tmp_path / "none-hitl.yaml",
        )


def test_cli_rejects_an_invalid_enum_value(tmp_path):
    with pytest.raises(SystemExit, match="must be one of"):
        cfg.resolve_settings(
            cli={"mask_mode": "alow"},
            env={},
            profile_path=tmp_path / "none.yaml",
            hitl_path=tmp_path / "none-hitl.yaml",
        )


# --- Milestone 7: the raw_trace knob ------------------------------------------


def test_raw_trace_defaults_to_off(tmp_path):
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml"
    )
    assert (settings.raw_trace, sources.raw_trace) == ("off", "default")


def test_raw_trace_resolves_through_all_four_tiers(tmp_path):
    p = tmp_path / cfg.PROFILE_NAME
    p.write_text("raw_trace: file\n", encoding="utf-8")
    hitl = tmp_path / "none-hitl.yaml"

    settings, sources = cfg.resolve_settings(env={}, profile_path=p, hitl_path=hitl)
    assert (settings.raw_trace, sources.raw_trace) == ("file", "profile")

    settings, sources = cfg.resolve_settings(
        env={"DEEPAGENTS_RAW_TRACE": "console"}, profile_path=p, hitl_path=hitl
    )
    assert (settings.raw_trace, sources.raw_trace) == ("console", "env")

    settings, sources = cfg.resolve_settings(
        cli={"raw_trace": "both"},
        env={"DEEPAGENTS_RAW_TRACE": "console"}, profile_path=p, hitl_path=hitl,
    )
    assert (settings.raw_trace, sources.raw_trace) == ("both", "cli")


def test_raw_trace_rejects_an_invalid_mode_at_every_point_of_entry(tmp_path):
    p = tmp_path / cfg.PROFILE_NAME
    p.write_text("raw_trace: fille\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="must be one of"):
        cfg.load_profile(p)

    with pytest.raises(SystemExit, match="must be one of"):
        cfg.save_profile(tmp_path / "out.yaml", {"raw_trace": "fille"})

    with pytest.raises(SystemExit, match="DEEPAGENTS_RAW_TRACE: raw_trace must be one of"):
        cfg.resolve_settings(
            env={"DEEPAGENTS_RAW_TRACE": "fille"},
            profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml",
        )

    with pytest.raises(SystemExit, match="must be one of"):
        cfg.resolve_settings(
            cli={"raw_trace": "fille"}, env={},
            profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml",
        )


def test_raw_trace_choices_are_the_sinks_own_modes():
    # One declaration: the registry validating a different list than the writer
    # accepts is exactly the drift M5.1 exists to remove.
    rawtrace = _load("harness.rawtrace")
    assert cfg.SPECS_BY_NAME["raw_trace"].choices == rawtrace.MODES
    assert cfg.SPECS_BY_NAME["raw_trace"].cast is str  # invariant 19: choices => str


def test_bool_knobs_still_accept_every_launcher_spelling(tmp_path):
    """Guard against "fixing" the gap by giving bools a `choices` tuple: the
    launchers pass `1`, `.env` files carry `true`, the wizard writes `on`."""
    for raw in ("1", "true", "TRUE", "yes", "on"):
        settings, _ = cfg.resolve_settings(
            env={"DEEPAGENTS_JAIL": raw},
            profile_path=tmp_path / "none.yaml",
            hitl_path=tmp_path / "none-hitl.yaml",
        )
        assert settings.jail is True, raw


# --- one renderer, two presentations (R3) --------------------------------------


def test_format_config_lines_prefix_and_width_are_parameters(tmp_path):
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml"
    )
    narrow = cfg.format_config_lines(settings, sources)
    wide = cfg.format_config_lines(settings, sources, prefix="[harness] ", width=24)
    assert len(narrow) == len(wide)
    assert narrow[0].startswith("model")
    assert wide[0].startswith("[harness] model")

    def field_names(lines):
        return [l.split("=")[0].strip().removeprefix("[harness]").strip() for l in lines]

    # Same fields, same order, same source tags -- only the presentation differs.
    assert field_names(narrow) == field_names(wide)


def test_format_config_lines_overrides_and_session_tags(tmp_path):
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml"
    )
    lines = cfg.format_config_lines(
        settings, sources, overrides={"model": "openai:gpt-6"}, edited={"model"}
    )
    assert "openai:gpt-6" in lines[0] and "(session)" in lines[0]


# --- R7: every persisted pre-spinup knob is read by BOTH launchers -------------


def test_prespinup_profile_keys_are_consumed_by_both_launchers():
    """A profile key nothing consumes is exactly the M5 §0.1 bug class: the
    wizard writes `cpus:`/`net_jail:` and `docker run` never sees them. The
    launchers stay hand-written (fork 3 -- run-docker needs no host Python), so
    the duplication is guarded rather than removed."""
    scripts = Path(__file__).resolve().parent.parent.parent / "scripts"
    ps1, sh = scripts / "run-docker.ps1", scripts / "run-docker.sh"
    if not (ps1.is_file() and sh.is_file()):
        pytest.skip("launchers are host-side only (scripts/ is not COPYed into the image)")
    ps1_text, sh_text = ps1.read_text(encoding="utf-8"), sh.read_text(encoding="utf-8")
    for spec in cfg.WIZARD_PRESPINUP_SPECS:
        assert spec.profile_key in ps1_text, f"run-docker.ps1 never reads {spec.profile_key!r}"
        assert spec.profile_key in sh_text, f"run-docker.sh never reads {spec.profile_key!r}"


def test_wizard_prespinup_specs_are_the_persisted_prespinup_half():
    assert cfg.WIZARD_PRESPINUP_SPECS == tuple(
        s for s in cfg.FIELD_SPECS if s.tier == "prespinup" and s.profile_key
    )
    assert [s.name for s in cfg.WIZARD_PRESPINUP_SPECS] == [
        "mask_mode", "jail", "jail_apparmor", "jail_systempaths",
        "cpus", "memory", "pids_limit", "net_jail",
    ]


# --- Milestone 8 B1: the three hard stops -------------------------------------


def test_hard_stops_resolve_through_all_four_tiers(tmp_path):
    p = tmp_path / cfg.PROFILE_NAME
    p.write_text(
        "max_steps: 40\nmax_seconds: 600\nmax_turns: 5\n", encoding="utf-8"
    )
    hitl = tmp_path / "none-hitl.yaml"

    settings, sources = cfg.resolve_settings(env={}, profile_path=p, hitl_path=hitl)
    assert (settings.max_steps, sources.max_steps) == (40, "profile")
    assert (settings.max_seconds, sources.max_seconds) == (600.0, "profile")
    assert (settings.max_turns, sources.max_turns) == (5, "profile")

    env = {
        "DEEPAGENTS_MAX_STEPS": "60",
        "DEEPAGENTS_MAX_SECONDS": "900.5",
        "DEEPAGENTS_MAX_TURNS": "9",
    }
    settings, sources = cfg.resolve_settings(env=env, profile_path=p, hitl_path=hitl)
    assert (settings.max_steps, sources.max_steps) == (60, "env")
    assert (settings.max_seconds, sources.max_seconds) == (900.5, "env")
    assert (settings.max_turns, sources.max_turns) == (9, "env")

    settings, sources = cfg.resolve_settings(
        cli={"max_steps": 12, "max_seconds": 30.0, "max_turns": 1},
        env=env, profile_path=p, hitl_path=hitl,
    )
    assert (settings.max_steps, sources.max_steps) == (12, "cli")
    assert (settings.max_seconds, sources.max_seconds) == (30.0, "cli")
    assert (settings.max_turns, sources.max_turns) == (1, "cli")


def test_hard_stops_default_to_none_not_to_a_number(tmp_path):
    """The removable contract, at the resolver.

    `None` is what makes the pass-through structural downstream: `cli.main` puts
    no `recursion_limit` key in the graph config, builds no `Deadline` and no
    `TurnCounter`. A default of "a very large number" would look identical in a
    passing test and would be a behaviour change (`milestone8.md` §10, M7
    invariant 18's lesson).
    """
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml"
    )
    assert settings.max_steps is None
    assert settings.max_seconds is None
    assert settings.max_turns is None
    for name in ("max_steps", "max_seconds", "max_turns"):
        assert getattr(sources, name) == "default"


def test_hard_stops_reject_a_non_numeric_value_at_every_point_of_entry(tmp_path):
    bad_profile = tmp_path / cfg.PROFILE_NAME
    bad_profile.write_text("max_steps: forty\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cfg.load_profile(bad_profile)

    with pytest.raises(SystemExit):
        cfg.resolve_settings(
            env={"DEEPAGENTS_MAX_SECONDS": "soon"},
            profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml",
        )

    with pytest.raises(SystemExit):
        cfg.resolve_settings(
            env={"DEEPAGENTS_MAX_TURNS": "lots"},
            profile_path=tmp_path / "none.yaml", hitl_path=tmp_path / "none-hitl.yaml",
        )


def test_hard_stops_are_live_and_persisted():
    # Live (read by the harness process, not by `docker run`) and persisted
    # ("never let a session run more than an hour" is a standing preference, in a
    # way `headless` and `raw_trace` are not -- §13 item 4).
    for name in ("max_steps", "max_seconds", "max_turns"):
        spec = cfg.SPECS_BY_NAME[name]
        assert spec.tier == "live", name
        assert spec.settable is True, name
        assert spec.profile_key == name, name
        assert spec.env_var == f"DEEPAGENTS_{name.upper()}", name
        # Numeric knobs, so no `choices` -- a value list on an int field would
        # reject every number outside it.
        assert spec.choices is None, name


def test_emit_patch_is_a_prespinup_knob_that_is_not_persisted():
    """Milestone 8 B2, on `headless`'s precedent (§13 item 4).

    A real FieldSpec, so it gets validation and `harness doctor` display for
    free, but `profile_key=None`: it is a per-sweep mode, not a preference.
    Pre-spinup rather than live because the base commit is resolved once at
    startup -- a live toggle could not take effect, and a knob that silently
    does nothing is worse than one that is honestly fixed.
    """
    spec = cfg.SPECS_BY_NAME["emit_patch"]
    assert spec.tier == "prespinup"
    assert spec.profile_key is None
    assert spec.settable is False
    assert spec.env_var == "DEEPAGENTS_EMIT_PATCH"
    # No `choices` on a bool: a value list would reject `DEEPAGENTS_EMIT_PATCH=1`,
    # the spelling a launcher passes (M5.1 invariant 19).
    assert spec.choices is None


def test_emit_patch_resolves_from_env_and_defaults_off(tmp_path):
    hitl = tmp_path / "none-hitl.yaml"
    settings, sources = cfg.resolve_settings(
        env={}, profile_path=tmp_path / "none.yaml", hitl_path=hitl
    )
    assert settings.emit_patch is False and sources.emit_patch == "default"

    settings, sources = cfg.resolve_settings(
        env={"DEEPAGENTS_EMIT_PATCH": "1"},
        profile_path=tmp_path / "none.yaml", hitl_path=hitl,
    )
    assert settings.emit_patch is True and sources.emit_patch == "env"

    settings, sources = cfg.resolve_settings(
        cli={"emit_patch": True}, env={"DEEPAGENTS_EMIT_PATCH": "0"},
        profile_path=tmp_path / "none.yaml", hitl_path=hitl,
    )
    assert settings.emit_patch is True and sources.emit_patch == "cli"
