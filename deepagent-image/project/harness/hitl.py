"""HITL integration — wires the interrupt spine into the running agent (Milestone 3).

This is the image-side glue that the pure cores (``interrupt``, ``config``,
``audit``) hang off of:

  * **S1 channel + resume loop** — ``run_interrupt_loop`` drains a graph result's
    ``__interrupt__`` list: it renders each request on the REPL channel, collects
    a typed human reply (or resolves it headless, S5), records it to the audit
    trail (S7), and resumes the graph with ``Command(resume=value)``. The loop
    logic takes an injected ``resume_fn`` and ``channel`` so it is host-testable
    with fakes — no langgraph needed.
  * **S2 pause gate** — ``PauseMiddleware`` gates ``tool.start`` / ``session.end``
    by the ``autonomy_level`` preset + ``review_triggers``, raising ``interrupt()``
    in-process (the ``pause`` action tier).
  * **S3 ask_human tool** — ``make_ask_human_tool`` lets the agent itself raise an
    interrupt when it decides it is blocked.

The channel abstraction (``Channel.ask``) keeps the terminal out of the loop
logic; ``ReplChannel`` is the concrete REPL renderer (cap+expand + ``/show``, §6).

Only imported/wired when ``.harness-config.yaml`` is present (config.load_config
!= None); otherwise nothing here runs and the harness is byte-for-byte MVP.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness import audit, interrupt
from harness.config import Config, match_triggers
from harness.interrupt import (
    InterruptRequest,
    headless_decision,
    interpret_reply,
    new_request,
    render,
)

try:  # image-only base; degrade to object on a bare host (mirrors cost.py)
    from langchain.agents.middleware.types import AgentMiddleware
except ModuleNotFoundError:  # pragma: no cover - off-image
    AgentMiddleware = object  # type: ignore[assignment,misc]


@dataclass
class HitlContext:
    """Everything ``run_turn`` needs to drain graph interrupts for a turn.

    Built once in ``cli.main`` when ``.harness-config.yaml`` is present; ``None``
    otherwise (MVP path). ``channel`` is the REPL renderer (interactive) or
    ``None`` (headless, resolve via the §6 fail-closed policy)."""

    config: Config
    workspace: Path
    channel: "Channel | None"
    headless: bool


# --- extracting interrupts from a graph result -------------------------------

INTERRUPT_KEY = "__interrupt__"


def extract_interrupts(result) -> list[InterruptRequest]:
    """Pull pending interrupt requests out of a LangGraph invoke result.

    LangGraph surfaces pending interrupts under ``result["__interrupt__"]`` as a
    list of ``Interrupt`` objects whose ``.value`` is the payload we passed to
    ``interrupt()`` — our request dict. Tolerates both the object form and a plain
    ``{"value": {...}}`` dict (what the tests feed in)."""
    if not isinstance(result, dict):
        return []
    items = result.get(INTERRUPT_KEY) or []
    out: list[InterruptRequest] = []
    for item in items:
        value = getattr(item, "value", None)
        if value is None and isinstance(item, dict):
            value = item.get("value")
        if isinstance(value, dict) and value.get("kind"):
            out.append(InterruptRequest.from_dict(value))
    return out


# --- the REPL channel (§6 cap + expand) --------------------------------------


class Channel:
    """Surface a request to a human and return the resume value. The loop only
    depends on this small surface, so a test channel is trivial."""

    def ask(self, request: InterruptRequest):  # pragma: no cover - interface
        raise NotImplementedError


class ReplChannel(Channel):
    """Terminal channel: render the request (capped context + ``/show`` expand),
    read a reply through the same seam the prompt loop uses, and interpret it for
    the request's kind. Re-prompts on an unparseable reply."""

    def __init__(self, read_line: Callable[[str], str], emit: Callable[[str], None] | None = None):
        self._read_line = read_line
        self._emit = emit or (lambda s: print(s, file=sys.stderr))

    def ask(self, request: InterruptRequest):
        self._emit(render(request))
        while True:
            reply = self._read_line("answer> ")
            if reply.strip().lower() in ("/show", "/expand"):
                self._emit(interrupt.expand(request))
                continue
            try:
                return interpret_reply(request, reply)
            except interrupt.ReplyError as exc:
                self._emit(f"[harness] {exc}; try again (/show to expand)")


# --- the resume loop (S1) — host-testable via injected resume_fn/channel ------


class InterruptAborted(Exception):
    """A headless run hit a blocking interrupt with no valid fall-through (§6).

    Carries the distinct exit code so the headless entrypoint can surface it."""

    def __init__(self, request: InterruptRequest, reason: str):
        super().__init__(reason)
        self.request = request
        self.exit_code = interrupt.EXIT_INTERRUPT_ABORT


def resolve_value(
    request: InterruptRequest,
    *,
    channel: Channel | None,
    headless: bool,
    config: Config,
) -> tuple[object, str]:
    """Resolve one interrupt to (value, resolved_by).

    Headless (no human): apply the §6 fail-closed policy — fall through to the
    default, deny an approve, or abort (raising ``InterruptAborted``). Interactive:
    ask the channel. ``resolved_by`` is "human" | "headless-default" | "denied"."""
    if headless or channel is None:
        decision = headless_decision(
            request,
            autonomy_level=config.autonomy_level,
            interruption_policy=config.interruption_policy,
        )
        if decision.abort:
            raise InterruptAborted(request, decision.reason)
        return decision.value, ("denied" if decision.value is False else "headless-default")
    return channel.ask(request), "human"


def run_interrupt_loop(
    result,
    resume_fn: Callable[[object], object],
    *,
    channel: Channel | None,
    headless: bool,
    config: Config,
    workspace: Path,
    audit_on: bool = True,
):
    """Drain every interrupt from `result`, resuming the graph until it runs clean.

    ``resume_fn(value)`` re-invokes the graph with ``Command(resume=value)`` and
    returns the next result (the caller supplies it so this stays langgraph-free
    and testable). One interrupt is resolved per iteration (the blocking path,
    the M3 acceptance bar; shadow batching is §6-open). Each resolution is
    audited (S7). Returns the final interrupt-free result.

    A bounded iteration cap guards against a pathological gate that re-raises an
    interrupt every resume — it aborts loudly rather than spinning forever.
    """
    guard = 0
    while True:
        pending = extract_interrupts(result)
        if not pending:
            return result
        guard += 1
        if guard > 1000:
            raise RuntimeError("interrupt loop did not converge (re-raised >1000 times)")
        request = pending[0]
        value, resolved_by = resolve_value(
            request, channel=channel, headless=headless, config=config
        )
        if audit_on:
            try:
                audit.record_interrupt(workspace, request, value, resolved_by=resolved_by)
            except Exception as exc:  # noqa: BLE001 - audit must never fail a turn
                print(f"[harness] audit: failed to record interrupt ({exc})", file=sys.stderr)
        result = resume_fn(value)


def resume_command(value):
    """``Command(resume=value)`` — the langgraph resume input (lazy import)."""
    from langgraph.types import Command

    return Command(resume=value)


# --- S3: the ask_human agent tool --------------------------------------------


def make_ask_human_tool(default_source: str = interrupt.SOURCE_ASK_HUMAN):
    """Build the ``ask_human`` deep-agents tool (S3).

    The agent calls this when *it* decides it is blocked — ambiguous requirements,
    a missing credential, a design fork it should not guess. It raises an interrupt
    through the same spine and returns the human's answer as the tool result
    (trusted input, §6). Returns ``None`` if langchain's tool decorator is absent."""
    from langchain_core.tools import tool

    @tool
    def ask_human(question: str, options: list[str] | None = None, context: str | None = None) -> str:
        """Ask the human operator a question and BLOCK until they answer.

        Use ONLY when you are genuinely blocked and cannot proceed safely by
        yourself: ambiguous requirements, a missing secret/credential, or a
        design decision you should not guess. Prefer acting when you reasonably
        can. `options` offers a fixed choice; `context` is extra detail (a diff,
        a command) shown to the human. Returns the human's typed answer.
        """
        kind = interrupt.KIND_CHOOSE if options else interrupt.KIND_INPUT
        request = new_request(
            kind,
            question,
            options=tuple(options or ()),
            context=context,
            source=default_source,
        )
        return interrupt.raise_interrupt(request)

    return ask_human


# --- S2: deterministic pause gate --------------------------------------------


def _tool_call_fields(request) -> tuple[str | None, list, str | None]:
    """Best-effort (tool_name, args, command) off a deepagents tool-call request.

    Kept defensive: the exact request shape is deepagents-internal, so anything
    missing degrades to None/[] rather than raising inside the middleware."""
    name = getattr(request, "tool_name", None) or getattr(request, "name", None)
    args = getattr(request, "args", None) or getattr(request, "tool_input", None) or {}
    if isinstance(args, dict):
        values = list(args.values())
        command = args.get("command") or args.get("cmd")
    else:
        values = [args]
        command = None
    return name, values, command


class PauseMiddleware(AgentMiddleware):
    """Gate tool calls (and, via cli, the PR) on a human approval (S2, the §3
    ``pause`` action tier).

    A tool call is gated when the autonomy preset gates ``tool.start`` (strict),
    or a ``review_triggers`` entry matches the call (any level). The gate raises an
    ``approve``/``resolve`` interrupt in-process: on deny the tool call is blocked
    and a refusal is returned to the model; on approve it proceeds. Deferred: the
    ``ask_human`` dedupe by tool_call_id (§6) is recorded in ``meta`` for the loop
    to honor.
    """

    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self._gate_all_tools = "tool.start" in config.gated_hooks()

    def _should_gate(self, name, values, command):
        if self._gate_all_tools:
            return True, None
        hit = match_triggers(
            self._config.review_triggers,
            tool_name=name,
            args=values,
            paths=[v for v in values if isinstance(v, str)],
            command=command,
        )
        return (hit is not None), hit

    def wrap_tool_call(self, request, handler):
        name, values, command = _tool_call_fields(request)
        gated, hit = self._should_gate(name, values, command)
        if not gated:
            return handler(request)
        reason = f"matched trigger {{{hit.on}: {hit.pattern}}}" if hit else "strict autonomy"
        req = new_request(
            interrupt.KIND_APPROVE,
            f"Approve tool call '{name}'? ({reason})",
            context=str(command or values or name),
            source=interrupt.SOURCE_DETERMINISTIC,
            default=False,  # fail-closed if unanswered headless
            meta={"tool_name": name or ""},
        )
        approved = interrupt.raise_interrupt(req)
        if approved:
            return handler(request)
        return _blocked_result(request, name)


def _blocked_result(request, name):
    """A tool result standing in for a denied call, fed back to the model so it
    can adapt rather than crash. Shape mirrors what a normal tool return yields;
    kept minimal + defensive since the deepagents result type is internal."""
    from langchain_core.messages import ToolMessage

    tool_call_id = getattr(request, "tool_call_id", None) or getattr(request, "id", None) or ""
    return ToolMessage(
        content=f"Tool call '{name}' was denied by the human operator. Do not retry it; choose another approach.",
        tool_call_id=tool_call_id,
    )
