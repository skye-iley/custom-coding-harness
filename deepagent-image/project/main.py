from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from pprint import pprint
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from dotenv import load_dotenv
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.checkpoint.sqlite import SqliteSaver

# NOTE: eventually set up a config file to define the ordering and default models given the API keys
DEFAULT_OPENAI_MODEL = "openai:gpt-5.5"
DEFAULT_GOOGLE_MODEL = "google_genai:gemini-3.5-flash"
DEFAULT_CLAUDE_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_CURSOR_MODEL = "cursor:composer-2.5"
DEFAULT_DEEPSEEK_MODEL = "deepseek:deepseek-v4-flash"
# left as "None" intentionally until I have them set up.
DEFAULT_OLLAMA_MODEL = None
DEFAULT_LMSTUDIO_MODEL = None
DEFAULT_OPENROUTER_MODEL = None

@dataclass(frozen=True)
class Provider:
    """One provider declared once. choose_model, validate_credentials, and
    resolve_chat_model all derive from this registry so the maps can't drift."""

    prefix: str              # model spec prefix, e.g. "openai:"
    api_key_env: str         # env var that holds the key / opts the provider in
    default_model: str | None  # auto-select default; None => never auto-selected
    requires_key: bool       # validate_credentials enforces api_key_env
    base_url_env: str | None = None  # set => OpenAI-compatible, routed via ChatOpenAI


# Auto-selection scans this list top-to-bottom, so order = priority when several
# provider keys are set. Local providers (ollama, lmstudio) need no real key.
PROVIDERS: list[Provider] = [
    Provider("google_genai:", "GOOGLE_API_KEY", DEFAULT_GOOGLE_MODEL, requires_key=True),
    Provider("anthropic:", "ANTHROPIC_API_KEY", DEFAULT_CLAUDE_MODEL, requires_key=True),
    Provider("openai:", "OPENAI_API_KEY", DEFAULT_OPENAI_MODEL, requires_key=True),
    Provider("cursor:", "CURSOR_API_KEY", DEFAULT_CURSOR_MODEL, requires_key=True, base_url_env="CURSOR_BASE_URL"),
    Provider("ollama:", "OLLAMA_API_KEY", DEFAULT_OLLAMA_MODEL, requires_key=False),
    Provider("lmstudio:", "LMSTUDIO_API_KEY", DEFAULT_LMSTUDIO_MODEL, requires_key=False, base_url_env="LMSTUDIO_BASE_URL"),
    Provider("deepseek:", "DEEPSEEK_API_KEY", DEFAULT_DEEPSEEK_MODEL, requires_key=True),
    Provider("openrouter:", "OPENROUTER_API_KEY", DEFAULT_OPENROUTER_MODEL, requires_key=True, base_url_env="OPENROUTER_BASE_URL"),
]


def _provider_for(model: str) -> Provider | None:
    """Registry entry whose prefix matches the model spec (None if unknown)."""
    for provider in PROVIDERS:
        if model.startswith(provider.prefix):
            return provider
    return None

DEFAULT_TASK = (
    "inspect workspace, summarize structure." 
)


BASE_SYSTEM_PROMPT = """You are an expert coding assistant operating inside a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files."""

## ------------------------------------------------##
#                  Helpful Functions                #
## ------------------------------------------------##

def _read_optional_text(path: Path) -> str:
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _read_optional_json(path: Path) -> dict:
    text = _read_optional_text(path)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}")


def _normalize_mcp_connections(servers: dict[str, dict]) -> dict[str, dict]:
    """Add the transport field langchain_mcp_adapters needs, inferring it from
    the Claude/Cursor-style .mcp.json shape (command -> stdio, url -> http)."""
    connections: dict[str, dict] = {}
    for name, cfg in servers.items():
        cfg = dict(cfg)
        if "transport" not in cfg:
            if "command" in cfg:
                cfg["transport"] = "stdio"
            elif "url" in cfg:
                cfg["transport"] = "streamable_http"
            else:
                raise SystemExit(
                    f"MCP server '{name}' in .mcp.json needs a 'command' or 'url'."
                )
        connections[name] = cfg
    return connections


def load_mcp_tools(config_path: Path) -> list:
    """Load tools from the MCP servers declared in .mcp.json (empty if none)."""
    servers = _read_optional_json(config_path).get("mcpServers") or {}
    if not servers:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(_normalize_mcp_connections(servers))
    return asyncio.run(client.get_tools())


# Events fired once in main() around the whole run (process/session scope),
# NOT per agent invocation. Everything else is per-turn / per-call middleware.
SESSION_EVENTS = ("session.start", "session.end")


def _run_hook_commands(commands: list[str]) -> None:
    # shell=True is intentional: hooks.json declares shell commands. Trust it.
    for command in commands:
        subprocess.run(command, shell=True, check=False)


def load_hooks(config_path: Path) -> dict[str, list[str]]:
    """Read hooks.json into an {event: [commands...]} map (empty if none)."""
    hooks = _read_optional_json(config_path).get("hooks") or []
    by_event: dict[str, list[str]] = {}
    for hook in hooks:
        commands = hook.get("command", [])
        if isinstance(commands, str):
            commands = [commands]
        for event in hook.get("events", []):
            by_event.setdefault(event, []).extend(commands)
    return by_event


class ShellHooksMiddleware(AgentMiddleware):
    """Run hooks.json shell commands on agent/model/tool lifecycle events.

    Scope (see LangChain middleware execution flow):
      agent.start / agent.end  -> once per user input (.invoke), via before/after_agent
      model.start / model.end  -> once per LLM call (fires on every reasoning step)
      tool.start / tool.end    -> once per tool call, around each tool execution

    Session-scoped hooks (session.start/session.end) are NOT handled here; they
    fire once in main() around the whole run.
    """

    def __init__(self, by_event: dict[str, list[str]]):
        super().__init__()
        self._by_event = by_event

    def before_agent(self, state, runtime):
        _run_hook_commands(self._by_event.get("agent.start", []))

    def after_agent(self, state, runtime):
        _run_hook_commands(self._by_event.get("agent.end", []))

    def before_model(self, state, runtime):
        _run_hook_commands(self._by_event.get("model.start", []))

    def after_model(self, state, runtime):
        _run_hook_commands(self._by_event.get("model.end", []))

    def wrap_tool_call(self, request, handler):
        _run_hook_commands(self._by_event.get("tool.start", []))
        try:
            return handler(request)
        finally:
            _run_hook_commands(self._by_event.get("tool.end", []))


def build_hook_middleware(by_event: dict[str, list[str]]) -> list[AgentMiddleware]:
    """Middleware for the non-session events (empty if none declared)."""
    if any(event not in SESSION_EVENTS for event in by_event):
        return [ShellHooksMiddleware(by_event)]
    return []

def choose_model(explicit_model: str | None) -> str:
    if explicit_model:
        return explicit_model

    env_model = os.getenv("DEEPAGENTS_MODEL")
    if env_model:
        return env_model

    for provider in PROVIDERS:
        if provider.default_model and os.getenv(provider.api_key_env):
            return provider.default_model

    raise SystemExit(
        "No model configured. Set DEEPAGENTS_MODEL plus the matching provider "
        "API key, or set OPENAI_API_KEY / GOOGLE_API_KEY."
    )


def validate_credentials(model: str) -> None:
    # Local providers (ollama, lmstudio) carry requires_key=False, so they are
    # not enforced here. Unknown prefixes pass through to init_chat_model.
    provider = _provider_for(model)
    if provider and provider.requires_key and not os.getenv(provider.api_key_env):
        raise SystemExit(f"Model '{model}' requires {provider.api_key_env}.")


def resolve_chat_model(model: str):
    """Turn a model spec into something create_deep_agent accepts.

    Native init_chat_model providers (openai/anthropic/google_genai/deepseek/
    ollama) pass through unchanged as a string. OpenAI-compatible providers
    (those with a base_url_env: cursor/openrouter/lmstudio) have no native
    prefix, so build a ChatOpenAI client pointed at their base_url. LM Studio
    runs keyless, so the api key falls back to a placeholder when unset.
    """
    provider = _provider_for(model)
    if provider and provider.base_url_env:
        base_url = os.getenv(provider.base_url_env)
        if not base_url:
            raise SystemExit(f"Model '{model}' requires {provider.base_url_env}.")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model[len(provider.prefix):],
            base_url=base_url,
            api_key=os.getenv(provider.api_key_env) or "not-needed",
        )
    return model


def resolve_workspace(raw: str) -> Path:
    workspace = Path(raw).expanduser().resolve()
    # The image sets DEEPAGENTS_IN_CONTAINER=1 (see Dockerfile). An explicit
    # marker beats sniffing the filesystem: it can't be tripped by a stray
    # /project on a host, and survives moving main.py.
    in_container = os.getenv("DEEPAGENTS_IN_CONTAINER") == "1"
    container_root = Path("/project")
    # is_relative_to handles the boundary correctly: /project itself and any
    # path under it pass, while paths outside (/home/agent, /project-evil) are
    # rejected. The old startswith("/project/") wrongly rejected exactly /project.
    if in_container and not workspace.is_relative_to(container_root):
        raise SystemExit(
            f"AGENT_WORKSPACE={workspace} is invalid inside the container. "
            "Use AGENT_WORKSPACE=/project/workspace in project/.env, or run via scripts/run-docker.ps1."
        )
    return workspace


def final_message_text(result: Any) -> str:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    if not messages:
        return str(result)

    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None and isinstance(last, dict):
        content = last.get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)

    return str(content)

def build_agent(
    model: Any,
    workspace: Path,
    tools: list | None = None,
    middleware: list[AgentMiddleware] | None = None,
    checkpointer: Any = None,
):
    workspace.mkdir(parents=True, exist_ok=True)
    backend = LocalShellBackend(
        root_dir=str(workspace),
        virtual_mode=True,
        inherit_env=True,
        env={"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin")},
    )

    agents_md = _read_optional_text(Path.cwd() / "AGENTS.md")
    system_prompt = BASE_SYSTEM_PROMPT
    if agents_md:
        system_prompt += "\nAdditional project instructions from AGENTS.md:\n" + agents_md

    custom_tools = []
    custom_tools.extend(tools or [])

    return create_deep_agent(
        model=model,
        tools=custom_tools,
        middleware=middleware or [],
        backend=backend,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
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
    return parser.parse_args()

## ------------------------------------------------##
#                        Tools                      #
## ------------------------------------------------##

## callable tool by all agents
#@tool
#def function(param_name: type) -> output_type:
# """docstring describing the function"""
# #PUT CODE HERE
# return [OUTPUT]

## -----------------------------------------------##
#               Runs the main function             #
## -----------------------------------------------##
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


