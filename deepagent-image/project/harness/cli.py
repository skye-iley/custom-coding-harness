"""CLI entrypoint: argument parsing and the run loop.

Wires the other modules together: pick a model (providers), load optional
config (loaders), build hook middleware (hooks), build + invoke the agent
(agent), all around a SqliteSaver checkpointer keyed by --thread-id.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from pprint import pprint

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver

from harness.agent import DEFAULT_TASK, build_agent, final_message_text, resolve_workspace
from harness.cost import (
    BudgetExceeded,
    CostTrackerMiddleware,
    Free,
    format_session_total,
)
from harness.loaders import load_hooks, load_mcp_tools
from harness.providers import choose_model, provider_for, resolve_chat_model, validate_credentials
from harness.workflows import (
    GateContext,
    build_workflow_middleware,
    hooks_to_workflows,
    load_workflows,
    run_hook,
    workflows_by_hook,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a LangChain Deep Agents coding harness.")
    parser.add_argument("task", nargs="*", help="Task for the agent. Defaults to a workspace inspection.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model spec, for example 'openai:gpt-5.5' or 'google_genai:gemini-3.5-flash'.",
    )
    parser.add_argument(
        "--workspace",
        default=os.getenv("AGENT_WORKSPACE", str(Path.cwd() / "workspace")),
        help="Directory exposed to the coding agent.",
    )
    parser.add_argument(
        "--thread-id",
        default=os.getenv("DEEPAGENTS_THREAD_ID", "default"),
        help="LangGraph checkpointer thread id.",
    )
    parser.add_argument("--stream", action="store_true", help="Print raw LangGraph stream events.")
    # Cost/token tracker (Milestone 1). Budgets default unset = no ceiling; env
    # fallbacks let the container be capped via --env-file without editing argv.
    parser.add_argument(
        "--max-cost",
        type=float,
        default=_env_float("DEEPAGENTS_MAX_COST"),
        help="End the session once cumulative USD cost crosses this (also DEEPAGENTS_MAX_COST).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=_env_int("DEEPAGENTS_MAX_TOKENS"),
        help="End the session once cumulative tokens cross this (also DEEPAGENTS_MAX_TOKENS).",
    )
    return parser.parse_args()


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw else None


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw else None


EXIT_TOKENS = {"/exit", "/quit"}


def _stage(message: str) -> None:
    """Lifecycle marker. Written to stderr (not stdout) with a distinct prefix
    so it stays out of the agent's reply stream and can be grepped/suppressed
    independently (MVP §1a req 6)."""
    print(f"[harness] {message}", file=sys.stderr)


def _is_exit_command(line: str) -> bool:
    # Matched in Python before the line ever reaches the agent, so quitting
    # never depends on the model choosing to call a tool (MVP §1a req 5).
    return line.strip().lower() in EXIT_TOKENS


def build_cost_tracker(
    model: str, max_cost: float | None, max_tokens: int | None
) -> CostTrackerMiddleware | None:
    """The cost tracker, or None when there's nothing to track (null = MVP).

    Built only when the resolved model can report a non-zero cost (non-Free
    pricing), carries an energy estimate, or a budget ceiling is set. Otherwise
    return None and main() appends no middleware — byte-for-byte MVP behavior
    (design_doc_milestone1.md §2.5 "remove-without-functional-change").
    """
    provider = provider_for(model)
    pricing = provider.pricing if provider else Free()
    rates = provider.rates_for(model) if provider else None
    has_energy = rates is not None and rates.has_energy
    budgeted = max_cost is not None or max_tokens is not None

    if isinstance(pricing, Free) and not has_energy and not budgeted:
        return None

    estimate = _env_float("DEEPAGENTS_PRICE_ESTIMATE")  # USD/Mtok for unpriced models
    electricity_rate = _env_float("DEEPAGENTS_ELECTRICITY_RATE")  # USD/kWh
    bare_model = model[len(provider.prefix):] if provider else model
    return CostTrackerMiddleware(
        pricing,
        bare_model,
        rates=rates,
        max_cost=max_cost,
        max_tokens=max_tokens,
        estimate_per_mtok=estimate,
        electricity_rate=electricity_rate,
    )


def run_turn(agent, text: str, config: dict, stream: bool = False) -> str | None:
    """One invoke on the given thread. Returns the answer text, or None when
    --stream already printed raw events instead of a final message."""
    inputs = {"messages": [{"role": "user", "content": text}]}
    _stage("thinking")
    if stream:
        for event in agent.stream(inputs, config=config):
            pprint(event)
        return None
    result = agent.invoke(inputs, config=config)
    return final_message_text(result)


def _print_session_total(tracker: CostTrackerMiddleware | None) -> None:
    """End-of-session token/cost/energy total (§1 req 2). No-op without a tracker."""
    if tracker is not None:
        print(
            format_session_total(tracker.session, electricity_rate=tracker._electricity_rate),
            file=sys.stderr,
        )


def run_repl(
    agent,
    config: dict,
    initial_task: str,
    stream: bool = False,
    tracker: CostTrackerMiddleware | None = None,
) -> int:
    """Container-lifetime loop: build once (by the caller), then prompt -> invoke
    -> answer until /exit, /quit, or EOF. A non-TTY stdin collapses to the single
    initial turn (CI / smoke), matching MVP §1a's non-interactive fallback.

    The optional `tracker` is the same CostTrackerMiddleware appended to the
    agent: a crossed budget surfaces as BudgetExceeded out of invoke (raised in
    its after_model), caught here to end the session deterministically like
    /exit. When tracker is None the clause is inert (null = MVP, §2.5).
    """
    interactive = sys.stdin.isatty()

    if initial_task:
        try:
            answer = run_turn(agent, initial_task, config, stream=stream)
        except KeyboardInterrupt:
            # Ctrl-C during a turn cancels that turn only; the session survives.
            print("\n[harness] turn cancelled")
            answer = None
        except BudgetExceeded as exc:
            _stage(f"budget exceeded: {exc}")
            _print_session_total(tracker)
            _stage("session closed")
            return 0
        if answer is not None:
            print(answer)
            print()

    if not interactive:
        _print_session_total(tracker)
        _stage("session closed")
        return 0

    while True:
        _stage("reading prompt")
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            # EOF (Ctrl-D), or Ctrl-C at an idle prompt: both end the session
            # deterministically (MVP §1a req 5 / decision on Ctrl-C).
            print()
            break

        if not line.strip():
            continue
        if _is_exit_command(line):
            break

        try:
            answer = run_turn(agent, line, config, stream=stream)
        except KeyboardInterrupt:
            # Relies on KeyboardInterrupt propagating out of the synchronous
            # invoke. Caveat: if the SIGINT lands mid-superstep (e.g. during a
            # checkpoint write) the thread state for this thread_id can be left
            # partial — the human message may or may not have persisted — so a
            # later resume of the same thread could see slightly inconsistent
            # history. Acceptable for the MVP; revisit if cancellation gets flaky.
            print("\n[harness] turn cancelled")
            continue
        except BudgetExceeded as exc:
            # Budget crossed mid-turn: end the session like /exit (§2.5).
            _stage(f"budget exceeded: {exc}")
            break

        if answer is not None:
            print(answer)
            print()

    _print_session_total(tracker)
    _stage("session closed")
    return 0


def main() -> int:
    load_dotenv()
    args = parse_args()

    _stage("container loading")
    workspace = resolve_workspace(args.workspace)
    model = choose_model(args.model)
    validate_credentials(model)

    # Empty + TTY -> no first turn, straight to the prompt (per §1a decisions).
    # Empty + no TTY -> DEFAULT_TASK, so smoke/CI keeps getting one real turn.
    task = " ".join(args.task).strip() or os.getenv("DEEPAGENTS_TASK") or ""
    if not task and not sys.stdin.isatty():
        task = DEFAULT_TASK

    mcp_tools = load_mcp_tools(Path.cwd() / ".mcp.json")

    # Workflows (§3): folder format under workflows/ (gated), plus the flat
    # hooks.json precursor adapted into always-gate side-effect workflows. One
    # execution path for both; grouped by hook point for dispatch.
    workflows_dir = Path(os.getenv("DEEPAGENTS_WORKFLOWS_DIR", str(Path.cwd() / "workflows")))
    all_workflows = load_workflows(workflows_dir) + hooks_to_workflows(
        load_hooks(Path.cwd() / "hooks.json")
    )
    by_hook = workflows_by_hook(all_workflows)

    # Checkpointer DB lives under the workspace so it rides the same host mount
    # the workspace does: thread state (conversation memory) survives across
    # --rm runs. Resume a thread by reusing its --thread-id / DEEPAGENTS_THREAD_ID.
    checkpoint_db = workspace / ".deepagents" / "checkpoints.sqlite"
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

    config = {"configurable": {"thread_id": args.thread_id}}

    print(f"Model: {model}")
    print(f"Workspace: {workspace}")
    if task:
        print(f"Task: {task}")
    print()

    # session.start / session.end bracket the whole run (process scope), so they
    # fire once even though the agent is invoked once per turn within the loop.
    # These run gated, like every other workflow (the git-branch workflow lives
    # on session.start; git-pr on session.end).
    run_hook(by_hook.get("session.start", []), GateContext("session.start", workspace))
    try:
        # SqliteSaver holds an open connection, so the whole REPL must run
        # inside the context manager, not just the first invoke.
        with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
            _stage("building agent")
            # Cost tracker is one more middleware, appended only when there is
            # something to track (§2.5). None => append nothing => MVP behavior.
            tracker = build_cost_tracker(model, args.max_cost, args.max_tokens)
            middleware = build_workflow_middleware(by_hook, workspace)
            if tracker is not None:
                middleware.append(tracker)
            agent = build_agent(
                resolve_chat_model(model),
                workspace,
                tools=mcp_tools,
                middleware=middleware,
                checkpointer=checkpointer,
            )
            return run_repl(agent, config, task, stream=args.stream, tracker=tracker)
    finally:
        run_hook(by_hook.get("session.end", []), GateContext("session.end", workspace))


if __name__ == "__main__":
    sys.exit(main())
