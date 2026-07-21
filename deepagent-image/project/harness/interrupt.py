"""Interrupt spine — the one primitive behind Milestone 3 HITL (design_doc.md §9).

*One interrupt request object, three trigger sources, one human channel.* This
module is the **channel-agnostic core**: the request data model, its stable-`id`
keying, JSON (de)serialization for the checkpoint round-trip, the REPL
`render`/`expand` presentation (§6 "cap + expand"), reply interpretation, and the
headless fail-closed resolution (§6). It is pure stdlib — no langchain, no
langgraph — so all of it is host-testable; the actual ``langgraph.interrupt()``
call is a thin lazy wrapper (`raise_interrupt`) the graph-side wiring uses.

Why a request *object* with a uuid ``id`` and not LangGraph positional resume
(§6, keying): once shadow mode batches more than one interrupt, positional resume
binds a reply to the wrong pause. A stable ``id`` lets a reply reference the exact
interrupt it answers — and lets the *same* keyed prompt re-surface after a
mid-interrupt process restart (the S1 acceptance bar), because the request dict
itself round-trips through ``checkpoints.sqlite``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

# --- kinds -------------------------------------------------------------------
# What shape of answer the human gives. Kept as plain strings so the request
# serializes to JSON for the checkpoint/audit round-trip without custom codecs.
KIND_APPROVE = "approve"  # yes/no gate on a proposed action
KIND_CHOOSE = "choose"    # pick one of `options`
KIND_INPUT = "input"      # free-text answer
KIND_RESOLVE = "resolve"  # human EDITS the proposed value (re-runs the gates, §6)
KINDS = frozenset({KIND_APPROVE, KIND_CHOOSE, KIND_INPUT, KIND_RESOLVE})

# Where the interrupt originated (§9 three sources). Recorded for the audit trail
# (S7) and for source-specific policy (e.g. missing-price suppression, §6).
SOURCE_DETERMINISTIC = "deterministic"  # a §3 pause gate (S2)
SOURCE_ASK_HUMAN = "ask_human"          # the agent asked (S3)
SOURCE_SYSTEM = "system"                # a harness system event (S4)

# Per-request headless behaviour (§4 S5). "default" => fall through to the
# request's `default` value on a non-TTY run; "abort" => stop the run.
TIMEOUT_DEFAULT = "default"
TIMEOUT_ABORT = "abort"

# Distinct non-zero exit code for a headless run that hit a blocking interrupt
# with no valid fall-through (§6: "a stuck pause in CI is worse than a labelled
# abort"). Chosen out of the way of the shell's 0/1/2 and 126-130 range.
EXIT_INTERRUPT_ABORT = 42


@dataclass(frozen=True)
class InterruptRequest:
    """A single suspend-and-ask request. Immutable; carried by value through the
    checkpointer and the audit log.

    ``id``            stable uuid; a resume value references it (§6 keying).
    ``kind``          one of KINDS — the shape of the expected answer.
    ``prompt``        the one-line question shown to the human.
    ``options``       choices for ``choose`` (ignored otherwise).
    ``context``       optional large payload (a diff / command) shown capped (§6).
    ``default``       fall-through value when no human answers (headless, §6).
    ``timeout_policy`` "default" | "abort" — headless behaviour (S5).
    ``source``        deterministic | ask_human | system (§9).
    ``meta``          free dict for source bookkeeping (e.g. tool_call_id for the
                      gate/ask_human dedupe in §6). Must stay JSON-serializable.
    """

    kind: str
    prompt: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    options: tuple[str, ...] = ()
    context: str | None = None
    default: object | None = None
    timeout_policy: str = TIMEOUT_DEFAULT
    source: str = SOURCE_SYSTEM
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown interrupt kind {self.kind!r}; expected one of {sorted(KINDS)}")
        if self.kind == KIND_CHOOSE and not self.options:
            raise ValueError("a 'choose' interrupt needs at least one option")

    # --- serialization (checkpoint round-trip + audit) -----------------------

    def to_dict(self) -> dict:
        """A plain JSON-serializable dict. This is exactly the value handed to
        ``langgraph.interrupt()``, so it persists in ``checkpoints.sqlite`` and a
        container that dies mid-wait re-surfaces the *same* keyed request."""
        return {
            "id": self.id,
            "kind": self.kind,
            "prompt": self.prompt,
            "options": list(self.options),
            "context": self.context,
            "default": self.default,
            "timeout_policy": self.timeout_policy,
            "source": self.source,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InterruptRequest":
        return cls(
            id=d["id"],
            kind=d["kind"],
            prompt=d["prompt"],
            options=tuple(d.get("options") or ()),
            context=d.get("context"),
            default=d.get("default"),
            timeout_policy=d.get("timeout_policy", TIMEOUT_DEFAULT),
            source=d.get("source", SOURCE_SYSTEM),
            meta=dict(d.get("meta") or {}),
        )


def new_request(kind: str, prompt: str, **kwargs) -> InterruptRequest:
    """Build a request with a fresh uuid `id`. Thin factory kept so callers never
    forget the id (positional resume is the bug we are avoiding, §6)."""
    return InterruptRequest(kind=kind, prompt=prompt, **kwargs)


# --- presentation (REPL "cap + expand", §6) ----------------------------------

# The REPL truncates a large `context` payload to this many lines with a
# "… +M lines — /show to expand" footer; `/show` prints the full payload.
DEFAULT_CONTEXT_LINES = 20


def render(
    request: InterruptRequest,
    *,
    max_context_lines: int = DEFAULT_CONTEXT_LINES,
    show_options: bool = True,
) -> str:
    """The REPL presentation of a request: prompt, numbered options, and a
    length-capped context (§6 cap + expand). Pure — no terminal — so a test asserts
    the rendered text (including the truncation footer) without a tty.

    ``show_options=False`` omits the numbered option list — used by the arrow-key
    select menu (S6 PR-b), which draws its own interactive option list, so the
    channel only prints the prompt + context header above it."""
    lines = [f"[interrupt · {request.kind}] {request.prompt}"]
    if show_options:
        for i, opt in enumerate(request.options, 1):
            lines.append(f"  {i}) {opt}")
    if request.context:
        clines = request.context.splitlines()
        if max_context_lines >= 0 and len(clines) > max_context_lines:
            lines.extend(clines[:max_context_lines])
            hidden = len(clines) - max_context_lines
            lines.append(f"  … +{hidden} lines — /show to expand")
        else:
            lines.extend(clines)
    if request.default is not None:
        lines.append(f"  [default if unanswered: {request.default!r}]")
    return "\n".join(lines)


def expand(request: InterruptRequest) -> str:
    """The full, untruncated context payload (what `/show` prints)."""
    return request.context or "(no additional context)"


# --- reply interpretation (collect side) -------------------------------------

_AFFIRMATIVE = {"y", "yes", "approve", "approved", "allow", "ok", "okay"}
_NEGATIVE = {"n", "no", "deny", "denied", "reject", "block", "abort"}


class ReplyError(ValueError):
    """A human reply that doesn't parse for the request's kind (re-prompt)."""


def interpret_reply(request: InterruptRequest, reply: str):
    """Map a typed human reply to the value that resumes the graph.

    * ``approve`` -> bool (empty reply uses ``default`` when set, else raises).
    * ``choose``  -> the chosen option (a 1-based index, or a case-insensitive
                     exact option string).
    * ``input`` / ``resolve`` -> the raw text (resolve = the edited value; an
      empty reply on either uses ``default`` when set).

    Raises ``ReplyError`` on an unparseable reply so the caller re-prompts rather
    than resuming with a wrong value.
    """
    text = reply.strip()

    if request.kind == KIND_APPROVE:
        low = text.lower()
        if not low:
            if request.default is not None:
                return request.default
            raise ReplyError("answer yes or no")
        if low in _AFFIRMATIVE:
            return True
        if low in _NEGATIVE:
            return False
        raise ReplyError(f"expected yes/no, got {text!r}")

    if request.kind == KIND_CHOOSE:
        if not text and request.default is not None:
            return request.default
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(request.options):
                return request.options[idx - 1]
            raise ReplyError(f"choice {idx} out of range 1..{len(request.options)}")
        for opt in request.options:
            if opt.lower() == text.lower():
                return opt
        raise ReplyError(f"{text!r} is not one of the offered options")

    # input / resolve: free text.
    if not text and request.default is not None:
        return request.default
    return reply


# --- headless / timeout resolution (§6 fail-closed) --------------------------

@dataclass(frozen=True)
class HeadlessDecision:
    """The outcome of resolving an interrupt with no human present (non-TTY run).

    Exactly one of ``value`` (fall-through answer to resume with) applies, or
    ``abort`` is True (stop the run with EXIT_INTERRUPT_ABORT). ``reason`` is a
    human-readable stage-marker string either way.
    """

    value: object | None = None
    abort: bool = False
    reason: str = ""


def headless_decision(
    request: InterruptRequest,
    *,
    autonomy_level: str = "guided",
    interruption_policy: str = "blocking",
) -> HeadlessDecision:
    """Resolve `request` on a run with no human present (P2 headless / non-TTY).

    Fail-closed policy (§6):
      * ``timeout_policy == "abort"`` -> abort.
      * else fall through to ``request.default`` when one is set.
      * no default + ``approve`` kind -> deny (False), the least-privilege answer,
        UNLESS ``strict`` + ``blocking`` (then abort — strict has no safe default).
      * no default + choose/input/resolve -> abort (nothing safe to synthesize).

    ``strict`` + ``blocking`` with no valid fall-through aborts with a distinct
    non-zero exit code rather than blocking forever — a stuck pause in CI is worse
    than a labelled abort (§6).
    """
    if request.timeout_policy == TIMEOUT_ABORT:
        return HeadlessDecision(abort=True, reason="timeout_policy=abort")

    if request.default is not None:
        return HeadlessDecision(value=request.default, reason="fell through to default")

    strict_blocking = autonomy_level == "strict" and interruption_policy == "blocking"

    if request.kind == KIND_APPROVE and not strict_blocking:
        return HeadlessDecision(value=False, reason="no default; denied (fail-closed)")

    return HeadlessDecision(
        abort=True,
        reason=(
            "no valid fall-through for a blocking interrupt on a non-TTY run"
            + (" under strict autonomy" if strict_blocking else "")
        ),
    )


# --- graph-side raise (lazy langgraph import) --------------------------------


def raise_interrupt(request: InterruptRequest):
    """Suspend the graph on `request` and return the human's resume value.

    Thin wrapper over ``langgraph.types.interrupt`` (imported lazily so this
    module stays host-importable). The request *dict* is the interrupt value, so
    it persists in the checkpoint and re-surfaces verbatim on a resume/restart.
    On resume LangGraph returns the ``Command(resume=...)`` value supplied by the
    channel.
    """
    from langgraph.types import interrupt  # lazy: image-only dependency

    return interrupt(request.to_dict())


def with_id(request: InterruptRequest, new_id: str) -> InterruptRequest:
    """A copy of `request` with a different id (test/util helper)."""
    return replace(request, id=new_id)
