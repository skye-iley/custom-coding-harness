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
from harness.hooks import _run_hook_commands, build_hook_middleware
from harness.loaders import load_hooks, load_mcp_tools
from harness.providers import choose_model, resolve_chat_model, validate_credentials


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
    return parser.parse_args()


EXIT_TOKENS = {"/exit", "/quit"}


def _stage(message: str) -> None:
    """Lifecycle marker, printed with a prefix distinct from agent replies."""
    print(f"[harness] {message}")


def _is_exit_command(line: str) -> bool:
    # Matched in Python before the line ever reaches the agent, so quitting
    # never depends on the model choosing to call a tool (MVP §1a req 5).
    return line.strip().lower() in EXIT_TOKENS


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


def run_repl(agent, config: dict, initial_task: str, stream: bool = False) -> int:
    """Container-lifetime loop: build once (by the caller), then prompt -> invoke
    -> answer until /exit, /quit, or EOF. A non-TTY stdin collapses to the single
    initial turn (CI / smoke), matching MVP §1a's non-interactive fallback.
    """
    interactive = sys.stdin.isatty()

    if initial_task:
        try:
            answer = run_turn(agent, initial_task, config, stream=stream)
        except KeyboardInterrupt:
            # Ctrl-C during a turn cancels that turn only; the session survives.
            print("\n[harness] turn cancelled")
            answer = None
        if answer is not None:
            print(answer)
            print()

    if not interactive:
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
            print("\n[harness] turn cancelled")
            continue

        if answer is not None:
            print(answer)
            print()

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
    hooks_by_event = load_hooks(Path.cwd() / "hooks.json")

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
    _run_hook_commands(hooks_by_event.get("session.start", []))
    try:
        # SqliteSaver holds an open connection, so the whole REPL must run
        # inside the context manager, not just the first invoke.
        with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
            _stage("building agent")
            agent = build_agent(
                resolve_chat_model(model),
                workspace,
                tools=mcp_tools,
                middleware=build_hook_middleware(hooks_by_event),
                checkpointer=checkpointer,
            )
            return run_repl(agent, config, task, stream=args.stream)
    finally:
        _run_hook_commands(hooks_by_event.get("session.end", []))


if __name__ == "__main__":
    sys.exit(main())
