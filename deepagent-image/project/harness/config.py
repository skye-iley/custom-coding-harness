"""HITL configuration — ``.harness-config.yaml`` (design_doc.md §9, Milestone 3).

Parses the four-key HITL config surface (§5) and implements the deterministic
gate machinery S2 needs: the ``review_triggers`` ``{on, pattern}`` match contract
(§6) and the ``autonomy_level`` presets that decide which hook points are gated by
default.

**Presence of the file turns HITL on.** Absent, ``load_config`` returns ``None`` and
the harness behaves exactly like Milestone 2 (removable seam). So a repo that never
adds ``.harness-config.yaml`` is byte-for-byte unaffected by this milestone.

Stdlib only — a tiny purpose-built parser (no ``pyyaml``, matching the workflow
engine's choice) covers the exact shapes §5 uses: scalar keys, a list of inline
``{on, pattern}`` maps, and a nested bool block. Host-testable.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
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
class Config:
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


def load_config(path: Path) -> Config | None:
    """Load ``.harness-config.yaml`` from `path`, or ``None`` when it is absent
    (HITL off, MVP behaviour). A malformed file fails loudly (SystemExit), like a
    bad workflow manifest."""
    path = Path(path)
    if not path.is_file():
        return None
    return parse_config(path.read_text(encoding="utf-8"), source=str(path))


def find_config(start: Path) -> Config | None:
    """Load the config from ``<start>/.harness-config.yaml`` if present."""
    return load_config(Path(start) / CONFIG_NAME)


def _fail(source: str, msg: str):
    raise SystemExit(f"{source}: {msg}")


def parse_config(text: str, source: str = CONFIG_NAME) -> Config:
    """Parse the HITL config text into a validated ``Config``.

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

    return Config(
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
