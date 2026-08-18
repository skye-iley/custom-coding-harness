"""Unified config surface (Milestone 5) + HITL configuration (Milestone 3, §9).

Two things share this module deliberately (milestone5_spec.md §1, fork 1
"absorb at the API level, not the file level"):

- **HITL** (``.harness-config.yaml``, unchanged since Milestone 3): the four-key
  config surface (§5) and the deterministic gate machinery S2 needs — the
  ``review_triggers`` ``{on, pattern}`` match contract (§6) and the
  ``autonomy_level`` presets that decide which hook points are gated by default.
  **Presence of the file turns HITL on.** Absent, ``load_config`` returns
  ``None`` and the harness behaves exactly like Milestone 2 (removable seam).
- **``Settings``** (Milestone 5): the resolver for every other run knob (model,
  budgets, mask/jail/security posture, resource caps, ...), precedence
  ``CLI flag > env var > profile file (.harness-profile.yaml) > built-in
  default``. ``Settings.hitl`` nests the HITL section above; the two **files**
  stay separate (the HITL parser is a tested, non-trivial grammar not worth
  risking in a merge), but ``resolve_settings()`` is the one Python object
  everything else reads.

Stdlib only — tiny purpose-built parsers (no ``pyyaml``, matching the workflow
engine's choice) cover the exact shapes each file uses. Host-testable.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# The only ``harness`` import here, and deliberately a leaf one: ``rawtrace``
# pulls stdlib plus ``harness.scrub`` and nothing else (no langchain), so the
# keyless/host import profile this module is pinned to is untouched. Taken so
# the raw-trace mode names have **one** declaration -- the sink and the registry
# validating different lists is exactly the drift M5.1 exists to remove.
from harness.rawtrace import MODES as RAW_TRACE_MODES

CONFIG_NAME = ".harness-config.yaml"

# --- valid enum values (validated loudly, like the workflow manifest) --------
AUTONOMY_LEVELS = ("strict", "guided", "autonomous")
INTERRUPTION_POLICIES = ("blocking", "shadow")
# What happens to the turn after a human DENIES a gated tool call:
#   halt     — end the agent turn immediately, return control to the human prompt
#              (no post-deny LLM turn; no bypass window; cheaper). Default.
#   continue — feed a denial ToolMessage back to the model and let the ReAct loop
#              continue (the model may adapt/pivot in the same turn).
ON_DENY_MODES = ("halt", "continue")
TRIGGER_TARGETS = ("tool_name", "arg", "path", "command")
SYSTEM_INTERRUPT_KEYS = ("missing_price", "provider_error", "permission_denied")

# autonomy_level -> the hook points that are *always* gated at that level (S2).
# review_triggers gate tool.start conditionally on top of these.
#   strict     — approve every tool call and the PR.
#   guided     — approve the PR; tool calls only when a review_trigger matches.
#   autonomous — nothing by default; only explicit review_triggers gate.
AUTONOMY_PRESETS = {
    "strict": frozenset({"tool.start", "session.end"}),
    "guided": frozenset({"session.end"}),
    "autonomous": frozenset(),
}


@dataclass(frozen=True)
class Trigger:
    """One ``review_triggers`` entry: force a pause when ``on`` matches ``pattern``.

    ``on`` ∈ TRIGGER_TARGETS. ``pattern`` is an fnmatch glob by default; a leading
    ``re:`` opts into a regex (``re.search``). ``path`` matches files in the
    pending diff; ``command`` matches the raw shell string (§6)."""

    on: str
    pattern: str

    def matches(self, value: str) -> bool:
        if value is None:
            return False
        if self.pattern.startswith("re:"):
            return re.search(self.pattern[3:], value) is not None
        return fnmatch.fnmatch(value, self.pattern)


@dataclass(frozen=True)
class HitlSection:
    autonomy_level: str = "guided"
    review_triggers: tuple[Trigger, ...] = ()
    interruption_policy: str = "blocking"
    on_deny: str = "halt"
    # Which harness system events raise (vs. log/crash). Default True: a present
    # config surfaces these unless explicitly disabled (matches the §5 example).
    system_interrupts: dict = field(
        default_factory=lambda: {k: True for k in SYSTEM_INTERRUPT_KEYS}
    )

    def gated_hooks(self) -> frozenset[str]:
        """Hook points always gated at this autonomy level (S2 preset)."""
        return AUTONOMY_PRESETS[self.autonomy_level]

    def system_interrupt_enabled(self, name: str) -> bool:
        return bool(self.system_interrupts.get(name, False))


# --- matching (S2 gate) ------------------------------------------------------


def match_triggers(
    triggers,
    *,
    tool_name: str | None = None,
    args=None,
    paths=None,
    command: str | None = None,
) -> Trigger | None:
    """The first ``review_triggers`` entry that fires for this event, or ``None``.

    Each trigger is checked only against the target it declares (§6):
      * ``tool_name`` — the tool being called.
      * ``arg``       — any string value in ``args``.
      * ``path``      — any file in ``paths`` (the pending diff).
      * ``command``   — the raw shell ``command`` string.
    """
    args = args or []
    paths = paths or []
    for t in triggers:
        if t.on == "tool_name" and tool_name is not None and t.matches(tool_name):
            return t
        if t.on == "arg" and any(t.matches(str(a)) for a in args):
            return t
        if t.on == "path" and any(t.matches(str(p)) for p in paths):
            return t
        if t.on == "command" and command is not None and t.matches(command):
            return t
    return None


# --- loading + parsing -------------------------------------------------------


def load_config(path: Path) -> HitlSection | None:
    """Load ``.harness-config.yaml`` from `path`, or ``None`` when it is absent
    (HITL off, MVP behaviour). A malformed file fails loudly (SystemExit), like a
    bad workflow manifest."""
    path = Path(path)
    if not path.is_file():
        return None
    # utf-8-sig, not utf-8: a file written by Windows PowerShell 5.1's
    # `Set-Content -Encoding utf8` (or Notepad) carries a BOM, which plain utf-8
    # preserves -- the first key then parses as "﻿autonomy_level" and hits
    # the unknown-key branch, so the harness refuses to start. utf-8-sig strips a
    # leading BOM when present and is a no-op otherwise. The *writers* stay on
    # plain utf-8: tolerate BOMs, never emit them.
    return parse_config(path.read_text(encoding="utf-8-sig"), source=str(path))


def find_config(start: Path) -> HitlSection | None:
    """Load the config from ``<start>/.harness-config.yaml`` if present."""
    return load_config(Path(start) / CONFIG_NAME)


def _fail(source: str, msg: str):
    raise SystemExit(f"{source}: {msg}")


def parse_config(text: str, source: str = CONFIG_NAME) -> HitlSection:
    """Parse the HITL config text into a validated ``HitlSection``.

    Recognizes exactly the §5 shapes: top-level scalars (``autonomy_level``,
    ``interruption_policy``), a ``review_triggers:`` list of inline ``{on, pattern}``
    maps, and a nested ``system_interrupts:`` bool block. Unknown top-level keys
    are rejected so a typo (e.g. ``autonomy_levels:``) fails loud instead of being
    silently ignored."""
    autonomy = "guided"
    policy = "blocking"
    on_deny = "halt"
    triggers: list[Trigger] = []
    system: dict = {k: True for k in SYSTEM_INTERRUPT_KEYS}

    section: str | None = None  # "review_triggers" | "system_interrupts" | None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[0] in " \t"
        stripped = raw.strip()

        if not indented:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = _strip_comment(value.strip())
            if key == "autonomy_level":
                autonomy, section = _scalar(value), None
            elif key == "interruption_policy":
                policy, section = _scalar(value), None
            elif key == "on_deny":
                on_deny, section = _scalar(value), None
            elif key == "review_triggers":
                section = "review_triggers"
                if value:  # inline list on the same line is not supported
                    _fail(source, "review_triggers must be a block list of {on, pattern}")
            elif key == "system_interrupts":
                section = "system_interrupts"
            else:
                _fail(source, f"unknown key {key!r}")
            continue

        # indented line — belongs to the current section
        if section == "review_triggers":
            triggers.append(_parse_trigger(stripped, source))
        elif section == "system_interrupts":
            k, _, v = stripped.partition(":")
            k = k.strip()
            if k not in SYSTEM_INTERRUPT_KEYS:
                _fail(source, f"unknown system_interrupts key {k!r}")
            system[k] = _parse_bool(_strip_comment(v.strip()), source)
        else:
            _fail(source, f"unexpected indented line {stripped!r}")

    if autonomy not in AUTONOMY_LEVELS:
        _fail(source, f"autonomy_level must be one of {AUTONOMY_LEVELS}, got {autonomy!r}")
    if policy not in INTERRUPTION_POLICIES:
        _fail(source, f"interruption_policy must be one of {INTERRUPTION_POLICIES}, got {policy!r}")
    if on_deny not in ON_DENY_MODES:
        _fail(source, f"on_deny must be one of {ON_DENY_MODES}, got {on_deny!r}")

    return HitlSection(
        autonomy_level=autonomy,
        review_triggers=tuple(triggers),
        interruption_policy=policy,
        on_deny=on_deny,
        system_interrupts=system,
    )


def _parse_trigger(item: str, source: str) -> Trigger:
    """Parse ``- { on: path, pattern: "*.env" }`` into a Trigger."""
    if not item.startswith("-"):
        _fail(source, f"review_triggers item must start with '-', got {item!r}")
    body = item[1:].strip()
    if not (body.startswith("{") and body.endswith("}")):
        _fail(source, f"review_triggers item must be an inline {{on, pattern}} map, got {body!r}")
    fields: dict[str, str] = {}
    for pair in _split_top_commas(body[1:-1]):
        k, _, v = pair.partition(":")
        k = k.strip()
        if not k:
            continue
        fields[k] = _scalar(v.strip())
    on = fields.get("on")
    pattern = fields.get("pattern")
    if on not in TRIGGER_TARGETS:
        _fail(source, f"trigger 'on' must be one of {TRIGGER_TARGETS}, got {on!r}")
    if not pattern:
        _fail(source, f"trigger needs a non-empty 'pattern' ({item!r})")
    return Trigger(on=on, pattern=pattern)


def _split_top_commas(s: str) -> list[str]:
    """Split on commas not inside quotes (so a pattern may contain a comma)."""
    out, cur, quote = [], [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            cur.append(ch)
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [p for p in (x.strip() for x in out) if p]


def _strip_comment(value: str) -> str:
    """Drop a trailing ``# comment`` on an unquoted value."""
    if value and value[0] not in "'\"":
        pos = value.find(" #")
        if pos != -1:
            return value[:pos].rstrip()
    return value


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        return value[1:-1]
    return value


def _parse_bool(value: str, source: str) -> bool:
    low = _scalar(value).lower()
    if low in ("true", "yes", "on", "1"):
        return True
    if low in ("false", "no", "off", "0"):
        return False
    _fail(source, f"expected a boolean, got {value!r}")


# =============================================================================
# Milestone 5: unified Settings resolver
# =============================================================================

PROFILE_NAME = ".harness-profile.yaml"

_TRUTHY = {"1", "true", "yes", "on"}


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _mask_enabled_cast(value) -> bool:
    """Mirrors the pre-M5 semantics: mask is on unless the value is literally
    ``"0"`` (``os.environ.get("DEEPAGENTS_MASK", "1").strip() != "0"``)."""
    if isinstance(value, bool):
        return value
    return str(value).strip() != "0"


# =============================================================================
# Milestone 5.1: the field registry
# =============================================================================
#
# ONE declaration per knob. Everything that used to be a hand-maintained
# parallel list derives from ``FIELD_SPECS`` below: the profile-file field set
# and its per-cast parse buckets, the write order, ``LIVE_FIELDS``, the
# resolver loop, both display renderers, ``/config set``'s settable set and
# validators, and the wizard's custom-posture screen. Adding a live field is
# one entry here; a test fails if any derived structure was hand-written
# instead (milestone5.1.md §1).


@dataclass(frozen=True)
class FieldSpec:
    """One run knob, declared once.

    ``name``            -- the ``Settings`` attribute, or a dotted ``hitl.*``
                           sub-field (dotted names live in ``.harness-config.yaml``,
                           resolved as part of the whole ``HitlSection`` object, so
                           the resolver skips them while ``/config`` includes them).
    ``tier``            -- ``"live"`` (changeable in-session) | ``"prespinup"``
                           (fixed the moment ``docker run`` executes).
    ``env_var``         -- ``None`` => not env-settable, and therefore not walked
                           by ``resolve_settings`` (``hitl`` + the dotted subfields).
    ``profile_key``     -- ``None`` => deliberately not persisted; the reason is in
                           the entry's own comment, not a module-level note.
    ``cast``            -- the env/CLI cast. Also selects the profile-file parse
                           strategy (``_profile_parser``), so the two can't drift.
    ``choices``         -- the legal values of an *enum* knob. Drives validation at
                           every point of entry (milestone5.1.md §3.1), the
                           ``/config set`` picker, and the wizard's numbered menu.
                           ``None`` for free text and for bools (``_to_bool``
                           accepts ``1``/``true``/``on``/... -- a value list would
                           reject the very spellings the launchers pass).
    ``label``           -- wizard/menu prompt text.
    ``wizard``          -- how the wizard renders it: ``"auto"`` (menu when the
                           field is enum or bool, else a text prompt) or
                           ``"confirm"`` (a y/N question).
    ``settable``        -- editable via ``/config set`` in-session. The *behaviour*
                           lives in ``cli._LIVE_APPLIERS`` (it mutates the tracker /
                           archive / agent, which ``config.py`` must not import);
                           a test asserts the two agree exactly both ways.
    ``nullable``        -- a bare ``/config set <field>`` clears it.
    """

    name: str
    tier: str
    env_var: str | None = None
    profile_key: str | None = None
    cast: object = str
    default: object = None
    default_factory: object = None
    choices: tuple[str, ...] | None = None
    label: str = ""
    wizard: str = "auto"
    settable: bool = False
    nullable: bool = False

    def resolved_default(self):
        return self.default_factory() if self.default_factory is not None else self.default


FIELD_SPECS: tuple[FieldSpec, ...] = (
    # --- in-session-live (can change via /config without a restart) -----------
    FieldSpec(
        name="model", tier="live", env_var="DEEPAGENTS_MODEL", profile_key="model",
        label="Model", settable=True,
    ),
    FieldSpec(
        # profile_key=None: per-run, not a standing preference. Persisting it would
        # silently resume yesterday's thread on every launch (milestone5_spec.md §5).
        name="thread_id", tier="live", env_var="DEEPAGENTS_THREAD_ID", profile_key=None,
        default_factory=lambda: f"session-{datetime.now():%Y%m%d-%H%M%S}",
        label="Thread id", settable=True,
    ),
    FieldSpec(
        name="topic", tier="live", env_var="DEEPAGENTS_TOPIC", profile_key="topic",
        label="Topic", settable=True, nullable=True,
    ),
    FieldSpec(
        name="max_cost", tier="live", env_var="DEEPAGENTS_MAX_COST", profile_key="max_cost",
        cast=float, label="Max cost (USD)", settable=True,
    ),
    FieldSpec(
        name="max_tokens", tier="live", env_var="DEEPAGENTS_MAX_TOKENS", profile_key="max_tokens",
        cast=int, label="Max tokens", settable=True,
    ),
    # --- Milestone 8 B1: the three hard stops ---------------------------------
    #
    # All three default to None and stay None unless set: absence is the removable
    # contract (`milestone8.md` §10). `--max-steps` unset means the graph config
    # carries no `recursion_limit` key at all -- NOT a large number -- so the
    # pass-through is structural rather than arithmetic.
    #
    # They sit in the live tier beside max_cost/max_tokens because they are read
    # by the harness process itself, not by `docker run`, and they persist
    # (profile_key set) because "never let a session run more than an hour" is a
    # standing preference in a way `headless` or `raw_trace` is not.
    FieldSpec(
        # Bounds the ReAct loop *inside* one turn -- the runaway `--max-turns`
        # structurally cannot catch, since a headless benchmark instance is one
        # turn (`milestone8.md` §3). Live because the graph `config` dict is built
        # once in `cli.main` and passed into every `run_turn`, so an applier
        # mutating it takes effect on the next turn with no agent rebuild (§13
        # item 5).
        name="max_steps", tier="live", env_var="DEEPAGENTS_MAX_STEPS",
        profile_key="max_steps", cast=int, label="Max steps (per turn)",
        settable=True,
    ),
    FieldSpec(
        # A *session* deadline, checked at a step boundary. `float` rather than
        # `int` so a test or a tight sweep can express sub-second bounds without a
        # second unit.
        name="max_seconds", tier="live", env_var="DEEPAGENTS_MAX_SECONDS",
        profile_key="max_seconds", cast=float, label="Max seconds (session)",
        settable=True,
    ),
    FieldSpec(
        # Kept, and genuinely useful outside benchmarking (an interactive session
        # that should not run away overnight, a multi-task headless invocation) --
        # it is simply not the benchmark bound, and §3 is explicit that the doc
        # must not imply it is.
        name="max_turns", tier="live", env_var="DEEPAGENTS_MAX_TURNS",
        profile_key="max_turns", cast=int, label="Max turns (session)",
        settable=True,
    ),
    FieldSpec(
        # Milestone 7. A four-valued enum, not a bool plus a "where does it go"
        # knob -- two knobs that can disagree. `cast=str` because `choices` is
        # set (invariant 19), and the four values are the sink's own MODES, so
        # the validator and the writer cannot drift.
        #
        # tier="live": the operator case is flipping tracing on and re-running
        # the same prompt **in the same session**, against the same thread and
        # accumulated context. Restarting the container loses exactly the state
        # that made the failure reproducible. Safe to toggle live because `off`
        # is a true pass-through, not an absent middleware -- the removable
        # contract is about observable behaviour, not the middleware list's
        # element count (milestone7.md §10.1/§10.2).
        name="raw_trace", tier="live", env_var="DEEPAGENTS_RAW_TRACE",
        profile_key="raw_trace", cast=str, default="off",
        choices=RAW_TRACE_MODES, label="Raw trace", settable=True,
    ),
    FieldSpec(
        # env_var=None / profile_key=None: HITL is a whole-file object in its own
        # file (CONFIG_NAME) whose *presence* is the on/off switch, so it resolves
        # as one object rather than through the scalar precedence chain. The three
        # editable sub-fields follow as dotted specs.
        name="hitl", tier="live", env_var=None, profile_key=None, label="HITL",
    ),
    FieldSpec(
        name="hitl.autonomy_level", tier="live", choices=AUTONOMY_LEVELS,
        default="guided", label="Autonomy level", settable=True,
    ),
    FieldSpec(
        name="hitl.on_deny", tier="live", choices=ON_DENY_MODES,
        default="halt", label="On deny", settable=True,
    ),
    FieldSpec(
        name="hitl.interruption_policy", tier="live", choices=INTERRUPTION_POLICIES,
        default="blocking", label="Interruption policy", settable=True,
    ),
    # --- pre-spinup-only (fixed at container start; shown read-only in /config) --
    FieldSpec(
        # profile_key=None: a mode picked per-invocation (one-shot JSON vs. REPL),
        # not a standing preference.
        name="headless", tier="prespinup", env_var="DEEPAGENTS_HEADLESS", profile_key=None,
        cast=_to_bool, default=False, label="Headless",
    ),
    FieldSpec(
        # Milestone 8 B2. profile_key=None on `headless`'s precedent (§13 item 4):
        # a per-sweep mode, not a preference. Being a real FieldSpec still buys
        # validation and `harness doctor` display for free.
        #
        # tier="prespinup" rather than "live" because the base commit is resolved
        # once, at startup, before the agent touches anything -- turning the flag
        # on mid-session would have no base to diff against, and turning it off
        # would leave a resolved base nothing reads. A knob whose live value could
        # not take effect is worse than one that is honestly fixed.
        name="emit_patch", tier="prespinup", env_var="DEEPAGENTS_EMIT_PATCH",
        profile_key=None, cast=_to_bool, default=False, label="Emit patch",
    ),
    FieldSpec(
        # profile_key=None: a debugging escape hatch (M4's removable contract), not
        # a saveable default -- saving "masking off" is exactly the setting nobody
        # should acquire by accident.
        name="mask_enabled", tier="prespinup", env_var="DEEPAGENTS_MASK", profile_key=None,
        cast=_mask_enabled_cast, default=True, label="Mask enabled",
    ),
    FieldSpec(
        # profile_key=None: an audit surface's off switch is an operator escape
        # hatch, not a standing preference. Persisting it would also oblige BOTH
        # launchers to resolve and forward it
        # (test_prespinup_profile_keys_are_consumed_by_both_launchers) and would
        # put "do you want telemetry?" in the security wizard
        # (test_wizard_prespinup_specs_are_the_persisted_prespinup_half) -- asking
        # every operator to reconfirm a knob that defaults ON contradicts
        # defaulting it on. Env/.env is the whole off switch, and .env already
        # reaches the container through --env-file (milestone6_spec.md §7).
        #
        # tier="prespinup": read once when the middleware list is built. Toggling
        # mid-session would leave a half-recorded run, which is worse than either
        # state. No `choices`: it is a bool, and a value list would reject the very
        # spellings the launchers pass (DEEPAGENTS_TELEMETRY=1).
        name="telemetry", tier="prespinup", env_var="DEEPAGENTS_TELEMETRY", profile_key=None,
        cast=_to_bool, default=True, label="Telemetry",
    ),
    FieldSpec(
        name="mask_mode", tier="prespinup", env_var="DEEPAGENTS_MASK_MODE", profile_key="mask_mode",
        default="deny", choices=("deny", "allow"), label="Mask mode",
    ),
    FieldSpec(
        name="jail", tier="prespinup", env_var="DEEPAGENTS_JAIL", profile_key="jail",
        cast=_to_bool, default=False, label="Jail",
    ),
    FieldSpec(
        # No `choices`: any host-loaded AppArmor profile name is legal, plus the
        # special "unconfined". An open set, so validation would be wrong.
        name="jail_apparmor", tier="prespinup", env_var="DEEPAGENTS_JAIL_APPARMOR",
        profile_key="jail_apparmor", default=None, label="AppArmor profile",
    ),
    FieldSpec(
        # The third gate's knob (milestone4.1.md §13.7, fork J5). Docker's
        # maskedPaths/readonlyPaths cover the container's procfs, and the kernel
        # refuses bwrap's fresh `--proc` while they do -- so with the jail on the
        # launchers pass `--security-opt systempaths=unconfined` by default.
        # `default` here means Docker's default (masks kept), which is the LSM-only
        # control the measurement needed and the right choice on a host where the
        # jail starts without the relaxation (Docker Desktop/WSL2, measured).
        #
        # default=None, not "unconfined": unset means "let the launcher decide from
        # `jail`", and only the launchers know whether the jail is on. A literal
        # default would report `systempaths: unconfined` on a jail-off run, where
        # nothing is passed at all.
        name="jail_systempaths", tier="prespinup", env_var="DEEPAGENTS_JAIL_SYSTEMPATHS",
        profile_key="jail_systempaths", default=None,
        choices=("unconfined", "default"), label="Jail /proc masks (systempaths)",
    ),
    FieldSpec(
        name="cpus", tier="prespinup", env_var="CPUS", profile_key="cpus",
        default="2", label="CPU limit",
    ),
    FieldSpec(
        name="memory", tier="prespinup", env_var="MEMORY", profile_key="memory",
        default="4g", label="Memory limit",
    ),
    FieldSpec(
        name="pids_limit", tier="prespinup", env_var="PIDS_LIMIT", profile_key="pids_limit",
        default="512", label="PIDs limit",
    ),
    FieldSpec(
        name="net_jail", tier="prespinup", env_var="NET_JAIL", profile_key="net_jail",
        cast=_to_bool, default=False, wizard="confirm",
        label="Enable NetJail (deny-all egress + allowlist)?",
    ),
)

SPECS_BY_NAME: dict[str, FieldSpec] = {s.name: s for s in FIELD_SPECS}

# Specs the scalar precedence chain walks: everything with an env var. The two
# without one (`hitl` and its dotted sub-fields) are the whole-object file tier.
_RESOLVED_SPECS = tuple(s for s in FIELD_SPECS if s.env_var is not None)

# --- everything below is DERIVED; do not hand-maintain ------------------------

PROFILE_SPECS = tuple(s for s in FIELD_SPECS if s.profile_key is not None)
PROFILE_FIELDS = frozenset(s.profile_key for s in PROFILE_SPECS)
_PROFILE_SPECS_BY_KEY = {s.profile_key: s for s in PROFILE_SPECS}
# Field ordering for a written profile file = registry order.
_PROFILE_WRITE_ORDER = tuple(s.profile_key for s in PROFILE_SPECS)

# The pre-spinup/in-session split (milestone5.md §3's table) -- the single
# source of truth both /config's editor and `harness doctor`'s report filter
# on, so the table in the milestone doc and the code can't drift apart.
LIVE_FIELDS = frozenset(s.name for s in FIELD_SPECS if s.tier == "live" and "." not in s.name)

# The knobs `harness config`'s custom-posture screen asks about, in order: every
# persisted pre-spinup field. A new one appears in the wizard for free.
WIZARD_PRESPINUP_SPECS = tuple(
    s for s in PROFILE_SPECS if s.tier == "prespinup"
)


@dataclass(frozen=True)
class Settings:
    # --- in-session-live (can change via /config without a restart) ---
    model: str | None = None
    thread_id: str | None = None
    topic: str | None = None
    max_cost: float | None = None
    max_tokens: int | None = None
    max_steps: int | None = None
    max_seconds: float | None = None
    max_turns: int | None = None
    raw_trace: str = "off"
    hitl: HitlSection | None = None

    # --- pre-spinup-only (fixed at container start; shown read-only in /config) ---
    headless: bool = False
    emit_patch: bool = False
    mask_enabled: bool = True
    telemetry: bool = True
    mask_mode: str = "deny"
    jail: bool = False
    jail_apparmor: str | None = None
    jail_systempaths: str | None = None
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: str = "512"
    net_jail: bool = False


@dataclass(frozen=True)
class SettingsSources:
    """Same field names as ``Settings``; each value is one of "cli" | "env" |
    "profile" | "default". Powers ``/config``'s provenance display and
    ``harness doctor``'s resolved-config report."""

    model: str = "default"
    thread_id: str = "default"
    topic: str = "default"
    max_cost: str = "default"
    max_tokens: str = "default"
    max_steps: str = "default"
    max_seconds: str = "default"
    max_turns: str = "default"
    raw_trace: str = "default"
    hitl: str = "default"
    headless: str = "default"
    emit_patch: str = "default"
    mask_enabled: str = "default"
    telemetry: str = "default"
    mask_mode: str = "default"
    jail: str = "default"
    jail_apparmor: str = "default"
    jail_systempaths: str = "default"
    cpus: str = "default"
    memory: str = "default"
    pids_limit: str = "default"
    net_jail: str = "default"


def _check_choices(spec: FieldSpec, value, *, prefix: str = ""):
    """Reject a value outside an enum knob's declared ``choices``.

    The M5 gap this closes (milestone5.1.md §3.1): nothing knew a field's legal
    values, so `mask_mode: alow` persisted, resolved, and then silently yielded
    **deny** -- fail-safe, but the operator got the opposite of what they asked
    for plus a success message. One check, every enum knob, every entry point."""
    if spec.choices is not None and value not in spec.choices:
        raise SystemExit(
            f"{prefix}{spec.name} must be one of {spec.choices}, got {value!r}"
        )
    return value


def _resolve(spec: FieldSpec, cli_val, env_raw: str | None, profile_val):
    """One field's precedence: CLI > env > profile > default (milestone5_spec.md §4).

    The env var name is carried on the spec purely so a bad env value names
    *which* variable was bad -- an unqualified "invalid value '3x'" leaves the
    operator hunting. The profile tier is not re-checked here: ``load_profile``
    already casts and validates it against the file it came from, so its errors
    can name the path."""
    cast = spec.cast
    if cli_val is not None:
        return _check_choices(spec, cast(cli_val)), "cli"
    if env_raw:
        try:
            value = cast(env_raw)
        except ValueError:
            prefix = f"{spec.env_var}: " if spec.env_var else ""
            raise SystemExit(f"{prefix}invalid value {env_raw!r}")
        return _check_choices(spec, value, prefix=f"{spec.env_var}: "), "env"
    if profile_val is not None:
        return cast(profile_val), "profile"
    return spec.resolved_default(), "default"


def _parse_profile_value(spec: FieldSpec, value: str, source: str):
    """Parse one profile-file scalar with the strategy its spec's ``cast`` implies.

    Strict where the env tier is lenient, deliberately: a file the operator wrote
    by hand (or `harness config set` wrote for them) should fail loudly on
    garbage, whereas ``_to_bool`` on an env var has always been "anything not
    truthy is false". Keying off ``cast`` is what keeps the two from drifting --
    there is no second per-cast bucket list to forget to update."""
    cast = spec.cast
    if cast is _to_bool:
        parsed = _parse_bool(value, source)
    elif cast is float:
        try:
            parsed = float(_scalar(value))
        except ValueError:
            _fail(source, f"{spec.profile_key} must be a number, got {value!r}")
    elif cast is int:
        try:
            parsed = int(_scalar(value))
        except ValueError:
            _fail(source, f"{spec.profile_key} must be an integer, got {value!r}")
    elif cast is str:
        parsed = _scalar(value)
    else:  # pragma: no cover - no profile field uses another cast today
        try:
            parsed = cast(_scalar(value))
        except ValueError:
            _fail(source, f"{spec.profile_key} has an invalid value {value!r}")
    return _check_choices(spec, parsed, prefix=f"{source}: ")


def load_profile(path: Path) -> dict:
    """Load ``.harness-profile.yaml`` from `path` as a raw dict (pre-``Settings``),
    or ``{}`` when the file is absent. Unknown top-level keys fail loudly
    (``SystemExit``), same policy as ``.harness-config.yaml``. Flat scalars only
    -- reuses the tiny parser primitives above, no nested blocks."""
    path = Path(path)
    if not path.is_file():
        return {}
    source = str(path)
    values: dict = {}
    # utf-8-sig for the same reason as load_config: tolerate a BOM from a
    # hand-edited or PowerShell-written file rather than failing on key 1.
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t":
            _fail(source, f"unexpected indented line {raw.strip()!r} (profile is flat scalars only)")
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        # A key with nothing but a trailing comment ("key:   # note") is an
        # unset key, not a literal value starting with "#" -- _strip_comment
        # only trims a *trailing* comment off a real value.
        value = "" if value.startswith("#") else _strip_comment(value)
        if key not in PROFILE_FIELDS:
            _fail(source, f"unknown key {key!r}")
        if not value:
            continue  # blank value => unset, falls through to the next tier
        values[key] = _parse_profile_value(_PROFILE_SPECS_BY_KEY[key], value, source)
    return values


def _format_scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def save_profile(path: Path, values: dict) -> None:
    """Merge `values` into the on-disk profile at `path` and write atomically.

    Read-modify-write, so saving one field never clobbers others a prior save
    (or the wizard) set. Unknown keys -- and, since M5.1, values outside an enum
    field's ``choices`` -- are rejected before anything is written, so no writer
    can produce a file that ``load_profile`` would then refuse."""
    path = Path(path)
    unknown = set(values) - PROFILE_FIELDS
    if unknown:
        raise SystemExit(f"{path}: unknown key(s) {sorted(unknown)!r}")
    for key, value in values.items():
        _check_choices(_PROFILE_SPECS_BY_KEY[key], value, prefix=f"{path}: ")

    merged = load_profile(path)
    merged.update(values)

    lines = [
        "# .harness-profile.yaml -- persisted defaults for knobs not already in .env.",
        "# Written by `harness config` / `harness config security` (--save) or `/config save`.",
        "# Any key you don't set here falls through to env / .env / built-in default (see",
        "# harness/config.py precedence). Delete this file to fall back to that chain entirely",
        "# (removable contract -- identical to no profile ever having existed).",
        "",
    ]
    for name in _PROFILE_WRITE_ORDER:
        lines.append(f"{name}: {_format_scalar(merged.get(name))}")

    text = "\n".join(lines) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # `run-docker` bind-mounts this file into the container as a single-file
        # mount, and a rename over a mount point fails with EBUSY -- so an
        # in-session `/config save` can only write in place. Losing atomicity is
        # acceptable here: the file has one writer at a time (a wizard on the host
        # OR one REPL), and a torn write still parses or fails loud on next load.
        #
        # If THIS write fails too the target is genuinely unwritable -- notably
        # `/project` bound read-only under DEEPAGENTS_JAIL=1 (M4 slice H). The
        # OSError propagates deliberately (not SystemExit: an in-session
        # `/config save` must be able to catch it and stay in the REPL), so every
        # caller has to handle it.
        tmp.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


def resolve_settings(
    *,
    cli=None,
    env: Mapping[str, str] | None = None,
    profile_path: Path | None = None,
    hitl_path: Path | None = None,
) -> tuple[Settings, SettingsSources]:
    """Resolve every ``Settings`` field through CLI > env > profile > default.

    ``cli`` is an ``argparse.Namespace``, a dict, or ``None``; a field absent
    or ``None`` on it means "not explicitly passed" and falls through. ``env``
    defaults to ``os.environ``; ``profile_path``/``hitl_path`` default to
    ``Path.cwd() / PROFILE_NAME`` / ``Path.cwd() / CONFIG_NAME``.
    """
    if env is None:
        env = os.environ
    profile_path = Path(profile_path) if profile_path is not None else Path.cwd() / PROFILE_NAME
    hitl_path = Path(hitl_path) if hitl_path is not None else Path.cwd() / CONFIG_NAME

    profile = load_profile(profile_path)

    def cli_val(name: str):
        if cli is None:
            return None
        if isinstance(cli, dict):
            return cli.get(name)
        return getattr(cli, name, None)

    values: dict = {}
    sources: dict = {}

    # One loop over the registry, in registry order (milestone5.1.md §4 R1) --
    # the hand-listed `field(...)` block this replaced was edit site #6 of ten.
    for spec in _RESOLVED_SPECS:
        v, s = _resolve(
            spec,
            cli_val(spec.name),
            env.get(spec.env_var),
            profile.get(spec.profile_key) if spec.profile_key else None,
        )
        values[spec.name] = v
        sources[spec.name] = s

    # hitl resolves as a whole object, not per-field: presence-of-file still
    # means HITL-on, untouched by this milestone (milestone5_spec.md §4).
    hitl_conf = load_config(hitl_path)
    values["hitl"] = hitl_conf
    sources["hitl"] = "profile" if hitl_conf is not None else "default"

    return Settings(**values), SettingsSources(**sources)


# --- the one display renderer (milestone5.1.md §4 R3) -------------------------
#
# `cli._config_display_lines` and `config_cli.format_settings_lines` used to
# render the same data twice, at two widths, with one `[harness] ` prefix --
# same logic, drifting independently. Both are now thin wrappers over this.

def format_config_lines(
    settings: Settings,
    sources: SettingsSources,
    *,
    prefix: str = "",
    width: int = 16,
    prespinup_header: str = "--- pre-spinup (fixed at container start) ---",
    overrides: Mapping | None = None,
    edited=(),
) -> list[str]:
    """Every knob, source-tagged: live fields first (registry order), then the
    pre-spinup half read-only.

    `overrides` carries the REPL's *session* values -- the live model/thread/
    budget the process is actually running with, which have moved on from what
    `resolve_settings` saw at startup. A name in `edited` is tagged ``session``
    rather than with its original tier. Pure -- no I/O -- so both callers stay
    host-testable without a terminal."""
    overrides = overrides or {}

    def fmt(name: str, value, source: str) -> str:
        shown = "(unset)" if value in (None, "") else value
        return f"{prefix}{name:<{width}} = {str(shown):<28} ({source})"

    def value_of(name, fallback):
        return overrides[name] if name in overrides else fallback

    def source_of(name, tier_source):
        return "session" if name in edited else tier_source

    hitl_obj = value_of("hitl", settings.hitl)
    lines: list[str] = []
    for spec in FIELD_SPECS:
        if spec.tier != "live":
            continue
        if spec.name == "hitl":
            if hitl_obj is None:
                lines.append(fmt("hitl", "off", sources.hitl))
            continue
        if "." in spec.name:
            if hitl_obj is None:
                continue  # HITL off => no sub-fields to show
            attr = spec.name.split(".", 1)[1]
            lines.append(fmt(spec.name, getattr(hitl_obj, attr), source_of(spec.name, sources.hitl)))
            continue
        lines.append(fmt(
            spec.name,
            value_of(spec.name, getattr(settings, spec.name)),
            source_of(spec.name, getattr(sources, spec.name)),
        ))

    lines.append(f"{prefix}{prespinup_header}")
    for spec in FIELD_SPECS:
        if spec.tier != "prespinup":
            continue
        lines.append(fmt(spec.name, getattr(settings, spec.name), getattr(sources, spec.name)))
    return lines
