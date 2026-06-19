"""Agent construction: workspace resolution, system prompt, result extraction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware.types import AgentMiddleware

from harness.loaders import _read_optional_text

DEFAULT_TASK = (
    "inspect workspace, summarize structure."
)


BASE_SYSTEM_PROMPT = """You are an expert coding assistant operating inside a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files."""


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
