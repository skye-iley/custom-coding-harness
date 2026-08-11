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
    return parse_config(path.read_text(encoding="utf-8"), source=str(path))


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

# Profile-file fields, by parse strategy. Deliberately excludes thread_id
# (per-run, not a standing preference), headless (a mode picked per-invocation),
# mask_enabled (a debugging escape hatch, not a saveable default), and hitl
# (lives in its own file, CONFIG_NAME) -- milestone5_spec.md §5.
_PROFILE_BOOL_FIELDS = frozenset({"jail", "net_jail"})
_PROFILE_FLOAT_FIELDS = frozenset({"max_cost"})
_PROFILE_INT_FIELDS = frozenset({"max_tokens"})
_PROFILE_STR_FIELDS = frozenset(
    {"model", "topic", "mask_mode", "jail_apparmor", "cpus", "memory", "pids_limit"}
)
PROFILE_FIELDS = frozenset(_PROFILE_BOOL_FIELDS | _PROFILE_FLOAT_FIELDS | _PROFILE_INT_FIELDS | _PROFILE_STR_FIELDS)

# Field ordering for a written profile file (matches milestone5_spec.md §5's example).
_PROFILE_WRITE_ORDER = (
    "model", "topic", "max_cost", "max_tokens",
    "mask_mode", "jail", "jail_apparmor", "cpus", "memory", "pids_limit", "net_jail",
)


@dataclass(frozen=True)
class Settings:
    # --- in-session-live (can change via /config without a restart) ---
    model: str | None = None
    thread_id: str | None = None
    topic: str | None = None
    max_cost: float | None = None
    max_tokens: int | None = None
    hitl: HitlSection | None = None

    # --- pre-spinup-only (fixed at container start; shown read-only in /config) ---
    headless: bool = False
    mask_enabled: bool = True
    mask_mode: str = "deny"
    jail: bool = False
    jail_apparmor: str | None = None
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
    hitl: str = "default"
    headless: str = "default"
    mask_enabled: str = "default"
    mask_mode: str = "default"
    jail: str = "default"
    jail_apparmor: str = "default"
    cpus: str = "default"
    memory: str = "default"
    pids_limit: str = "default"
    net_jail: str = "default"


# The pre-spinup/in-session split (milestone5.md §3's table) -- the single
# source of truth both /config's editor and `harness doctor`'s report filter
# on, so the table in the milestone doc and the code can't drift apart.
LIVE_FIELDS = frozenset({"model", "thread_id", "topic", "max_cost", "max_tokens", "hitl"})


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


def _resolve(cli_val, env_raw: str | None, profile_val, default, *, cast=str):
    """One field's precedence: CLI > env > profile > default (milestone5_spec.md §4)."""
    if cli_val is not None:
        return cast(cli_val), "cli"
    if env_raw:
        try:
            return cast(env_raw), "env"
        except ValueError:
            raise SystemExit(f"invalid value {env_raw!r}")
    if profile_val is not None:
        return cast(profile_val), "profile"
    return default, "default"


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
    for raw in path.read_text(encoding="utf-8").splitlines():
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
        if key in _PROFILE_BOOL_FIELDS:
            values[key] = _parse_bool(value, source)
        elif key in _PROFILE_FLOAT_FIELDS:
            try:
                values[key] = float(_scalar(value))
            except ValueError:
                _fail(source, f"{key} must be a number, got {value!r}")
        elif key in _PROFILE_INT_FIELDS:
            try:
                values[key] = int(_scalar(value))
            except ValueError:
                _fail(source, f"{key} must be an integer, got {value!r}")
        else:
            values[key] = _scalar(value)
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
    (or the wizard) set. Unknown keys are rejected like ``load_profile`` does."""
    path = Path(path)
    unknown = set(values) - PROFILE_FIELDS
    if unknown:
        raise SystemExit(f"{path}: unknown key(s) {sorted(unknown)!r}")

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

    def field(name: str, env_name: str, *, cast=str, default=None):
        v, s = _resolve(cli_val(name), env.get(env_name), profile.get(name), default, cast=cast)
        values[name] = v
        sources[name] = s

    field(
        "thread_id",
        "DEEPAGENTS_THREAD_ID",
        cast=str,
        default=f"session-{datetime.now():%Y%m%d-%H%M%S}",
    )
    field("model", "DEEPAGENTS_MODEL", cast=str, default=None)
    field("topic", "DEEPAGENTS_TOPIC", cast=str, default=None)
    field("max_cost", "DEEPAGENTS_MAX_COST", cast=float, default=None)
    field("max_tokens", "DEEPAGENTS_MAX_TOKENS", cast=int, default=None)

    field("headless", "DEEPAGENTS_HEADLESS", cast=_to_bool, default=False)
    field("mask_enabled", "DEEPAGENTS_MASK", cast=_mask_enabled_cast, default=True)
    field("mask_mode", "DEEPAGENTS_MASK_MODE", cast=str, default="deny")
    field("jail", "DEEPAGENTS_JAIL", cast=_to_bool, default=False)
    field("jail_apparmor", "DEEPAGENTS_JAIL_APPARMOR", cast=str, default=None)
    field("cpus", "CPUS", cast=str, default="2")
    field("memory", "MEMORY", cast=str, default="4g")
    field("pids_limit", "PIDS_LIMIT", cast=str, default="512")
    field("net_jail", "NET_JAIL", cast=_to_bool, default=False)

    # hitl resolves as a whole object, not per-field: presence-of-file still
    # means HITL-on, untouched by this milestone (milestone5_spec.md §4).
    hitl_conf = load_config(hitl_path)
    values["hitl"] = hitl_conf
    sources["hitl"] = "profile" if hitl_conf is not None else "default"

    return Settings(**values), SettingsSources(**sources)
