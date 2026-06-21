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


def main() -> int:
    load_dotenv()
    args = parse_args()

    workspace = resolve_workspace(args.workspace)
    model = choose_model(args.model)
    validate_credentials(model)

    task = " ".join(args.task).strip() or os.getenv("DEEPAGENTS_TASK") or DEFAULT_TASK
    mcp_tools = load_mcp_tools(Path.cwd() / ".mcp.json")
    hooks_by_event = load_hooks(Path.cwd() / "hooks.json")

    # Checkpointer DB lives under the workspace so it rides the same host mount
    # the workspace does: thread state (conversation memory) survives across
    # --rm runs. Resume a thread by reusing its --thread-id / DEEPAGENTS_THREAD_ID.
    checkpoint_db = workspace / ".deepagents" / "checkpoints.sqlite"
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

    inputs = {"messages": [{"role": "user", "content": task}]}
    config = {"configurable": {"thread_id": args.thread_id}}

    print(f"Model: {model}")
    print(f"Workspace: {workspace}")
    print(f"Task: {task}")
    print()

    # session.start / session.end bracket the whole run (process scope), so they
    # fire once even if the agent is invoked multiple times on one thread later.
    _run_hook_commands(hooks_by_event.get("session.start", []))
    try:
        # SqliteSaver holds an open connection, so build + invoke must run inside
        # the context manager.
        with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
            agent = build_agent(
                resolve_chat_model(model),
                workspace,
                tools=mcp_tools,
                middleware=build_hook_middleware(hooks_by_event),
                checkpointer=checkpointer,
            )
            if args.stream:
                for event in agent.stream(inputs, config=config):
                    pprint(event)
                return 0

            result = agent.invoke(inputs, config=config)
            print(final_message_text(result))
            return 0
    finally:
        _run_hook_commands(hooks_by_event.get("session.end", []))


if __name__ == "__main__":
    sys.exit(main())
