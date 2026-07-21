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

import json
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
    the request's kind. Re-prompts on an unparseable reply.

    ``select`` (optional) is the S6 PR-b arrow-key menu: for a ``choose`` request it
    is called with the request and returns the picked option — or ``None`` to fall
    back to the typed index/name loop (menu cancelled, or arrows unavailable). Left
    ``None`` (headless, non-TTY, or no prompt_toolkit) the channel is typed-only, so
    the whole class stays host-testable with plain fakes."""

    def __init__(
        self,
        read_line: Callable[[str], str],
        emit: Callable[[str], None] | None = None,
        select: "Callable[[InterruptRequest], str | None] | None" = None,
    ):
        self._read_line = read_line
        self._emit = emit or (lambda s: print(s, file=sys.stderr))
        self._select = select

    def ask(self, request: InterruptRequest):
        # S6 PR-b: a `choose` with options gets the arrow-key menu when one is
        # wired. The menu draws its own option list, so render without it; a None
        # return (cancelled / arrows off) falls through to the typed loop below.
        if self._select is not None and request.kind == interrupt.KIND_CHOOSE and request.options:
            self._emit(render(request, show_options=False))
            chosen = self._select(request)
            if chosen is not None:
                return chosen
            self._emit("[harness] menu cancelled — type a choice (index or name)")
            self._emit(render(request))
        else:
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


class HaltTurn(Exception):
    """Raised by the pause gate on a DENY when ``on_deny='halt'``: abandon the agent
    turn *now* rather than feed a denial back and let the ReAct loop continue.

    Ending the turn on deny (vs. ``continue``) avoids the post-deny model call, the
    token/429 cost of it, and the bypass window where a denied ``rm -rf`` gets
    re-issued as ``rmdir``. Carries the denial ``ToolMessage`` so the caller can pair
    the now-dangling tool_call in checkpoint state (``update_state``) before returning
    to the human prompt — otherwise the next turn resumes with an unanswered
    tool_call and some providers reject that."""

    def __init__(self, tool_message, name: str):
        super().__init__(f"tool call {name!r} denied; turn halted")
        self.tool_message = tool_message
        self.tool_name = name


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


# --- S2: the session-end PR approval gate ------------------------------------


def should_gate_pr(config: "Config | None", *, interactive: bool, has_session: bool) -> bool:
    """Whether the session-end PR should pause for human approval (the ``pause``
    action tier applied to the ``session.end`` hook — the "approve the PR" half of
    the strict/guided presets).

    True only when **all** hold:
      * HITL is on (``config`` present);
      * the autonomy preset gates ``session.end`` (strict, guided — not autonomous);
      * there is actually a PR to gate (``has_session`` — a git-branch session.env
        exists; otherwise git-pr no-ops and a prompt would confuse);
      * a human is present (``interactive``).

    A non-interactive/headless run returns False and the PR **proceeds** as before:
    git-pr never auto-merges, so opening it without a human is safe, and a stuck
    prompt in CI is worse than an unreviewed-but-openable PR (§6). The gate is the
    interactive veto, not a headless block. (Contrast the tool gate, which
    fails-closed headless — a tool call can be destructive; a PR cannot.)"""
    if config is None:
        return False
    if "session.end" not in config.gated_hooks():
        return False
    if not has_session:
        return False
    return interactive


def make_pr_gate_request(
    branch: str | None = None, base: str | None = None, summary: str | None = None
) -> InterruptRequest:
    """Build the ``approve`` interrupt for the session-end PR gate.

    ``summary`` (a git log/diff-stat of what would be pushed) rides as the
    expandable ``context`` (``/show``). No ``default`` — the gate only runs
    interactively (``should_gate_pr``), so a fall-through value is never consulted."""
    src = f" from '{branch}'" if branch else ""
    dest = f" into {base}" if base else ""
    return new_request(
        interrupt.KIND_APPROVE,
        f"Approve opening the pull request{src}{dest}?",
        context=summary,
        source=interrupt.SOURCE_DETERMINISTIC,
        meta={"gate": "pr", "branch": branch or "", "base": base or ""},
    )


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
    """Best-effort (tool_name, args, command) off a langchain tool-call request.

    The live shape is a ``ToolCallRequest`` whose ``.tool_call`` is the standard
    langchain ToolCall dict — ``{"name", "args", "id"}`` — NOT top-level
    ``tool_name``/``args`` attributes. Reading the wrong place silently yielded
    ``name=None, args=[], command=None`` for every call, so under a non-``strict``
    preset the ``review_triggers`` gate matched nothing and every tool call
    (including ``rm -rf``) ran ungated. Prefer ``.tool_call``; fall back to the old
    attribute shapes so test fakes / other request types still degrade, not raise."""
    call = getattr(request, "tool_call", None)
    if isinstance(call, dict):
        name = call.get("name")
        args = call.get("args")
    else:
        name = getattr(request, "tool_name", None) or getattr(request, "name", None)
        args = getattr(request, "args", None) or getattr(request, "tool_input", None)
    args = args or {}
    if isinstance(args, dict):
        values = list(args.values())
        command = args.get("command") or args.get("cmd")
    else:
        values = [args]
        command = None
    return name, values, command


def _tool_call_args(request) -> dict:
    """The tool-call args as a dict ({} when unavailable), same source as
    ``_tool_call_fields``. Used to surface *parameters* (not just the tool name) in
    the approval prompt so a human can see what the call would actually do."""
    call = getattr(request, "tool_call", None)
    if isinstance(call, dict):
        args = call.get("args")
    else:
        args = getattr(request, "args", None) or getattr(request, "tool_input", None)
    return args if isinstance(args, dict) else {}


def _format_call_params(args: dict, command: str | None, *, cap: int = 200) -> str:
    """A compact one-line ``k=v, …`` summary of a call's params for the prompt line.

    Per-value and whole-line length caps keep the prompt readable; the full,
    untruncated args go in the request ``context`` (revealed by ``/show``)."""
    if not args:
        return command or ""
    parts = []
    for k, v in args.items():
        s = " ".join(str(v).split())
        if len(s) > 80:
            s = s[:80] + "…"
        parts.append(f"{k}={s}")
    line = ", ".join(parts)
    return line if len(line) <= cap else line[:cap] + "…"


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
        self._on_deny = getattr(config, "on_deny", "halt")

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
        args = _tool_call_args(request)
        # Show the *parameters*, not just the tool name — a human approving 'execute'
        # needs to see the command, and 'write_file' the path/content. Compact
        # summary on the prompt line; full args as expandable context (/show).
        params = _format_call_params(args, command)
        prompt = f"Approve tool call '{name}'? ({reason})"
        if params:
            prompt = f"Approve tool call '{name}'({params})? ({reason})"
        try:
            full_args = json.dumps(args, indent=2, default=str) if args else ""
        except (TypeError, ValueError):
            full_args = str(args)
        context = full_args or str(command or values or name)
        req = new_request(
            interrupt.KIND_APPROVE,
            prompt,
            context=context,
            source=interrupt.SOURCE_DETERMINISTIC,
            default=False,  # fail-closed if unanswered headless
            meta={"tool_name": name or ""},
        )
        approved = interrupt.raise_interrupt(req)
        if approved:
            return handler(request)
        # Announce the block on stderr: the model is fed a denial ToolMessage but a
        # weak model may still *claim* it did the action in its reply. This line is
        # the ground truth the operator can trust over the model's prose.
        print(
            f"[harness] tool call '{name}' DENIED by operator — NOT executed"
            + (f" ({command})" if command else ""),
            file=sys.stderr,
        )
        blocked = _blocked_result(request, name)
        if self._on_deny == "halt":
            # End the turn now: no post-deny model call, no bypass window. The caller
            # pairs `blocked` into checkpoint state and returns to the human prompt.
            raise HaltTurn(blocked, name or "")
        return blocked


def _blocked_result(request, name):
    """A tool result standing in for a denied call, fed back to the model.

    **Stop-and-report, not "choose another approach."** The earlier wording invited
    the model to reach the same goal by a different command — which is exactly how a
    denied ``rm -rf`` got bypassed with ``rmdir`` (the review-trigger gate is
    phrasing-blind, so the workaround runs ungated). A hard deny must tell the model
    to abandon the goal and report, not to route around the human's decision. This is
    guidance, not enforcement (a model can still disobey; only a tool-name gate or an
    fs jail enforces) — but it stops actively coaching the bypass."""
    from langchain_core.messages import ToolMessage

    call = getattr(request, "tool_call", None)
    tool_call_id = (
        (call.get("id") if isinstance(call, dict) else None)
        or getattr(request, "tool_call_id", None)
        or getattr(request, "id", None)
        or ""
    )
    return ToolMessage(
        content=(
            f"The operator DENIED the tool call '{name}'. Do NOT retry it, and do NOT "
            "attempt to achieve the same result by any other command, tool, or "
            "workaround — the denial applies to the intended action, not just this "
            "exact call. Stop pursuing this goal now and tell the user what you were "
            "trying to do and that it was denied."
        ),
        tool_call_id=tool_call_id,
    )
