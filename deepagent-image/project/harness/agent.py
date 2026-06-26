"""Agent construction: workspace resolution, system prompt, result extraction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware.types import AgentMiddleware

from harness.loaders import _read_optional_text
from harness.providers import PROVIDERS

DEFAULT_TASK = "inspect workspace, summarize structure."

# Suffixes that mark an env var as a credential the agent's shell must not see.
# Provider key names come from the PROVIDERS registry (single source of truth);
# these suffixes catch any other secret the harness env happens to carry.
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _agent_shell_env() -> dict[str, str]:
    """Env handed to the agent's shell tool.

    Starts from the harness process env so PATH/HOME/CONDA_*/GIT_* still work,
    but strips provider credentials. The workspace is a host bind-mount, so a
    prompt-injected agent that could read ANTHROPIC_API_KEY/OPENAI_API_KEY/etc.
    via `printenv` could write them to host disk — scrub the keys before they
    ever reach the shell (secrets hard-rule). This replaces inherit_env=True,
    whose merge semantics would otherwise leak the whole environment.
    """
    provider_keys = {p.api_key_env for p in PROVIDERS}
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in provider_keys or key.endswith(_SECRET_ENV_SUFFIXES):
            continue
        env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


BASE_SYSTEM_PROMPT = """You are an expert coding assistant operating inside a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files."""


class _WorkspaceShellBackend(LocalShellBackend):
    """LocalShellBackend that tolerates real-root-prefixed virtual paths.

    `virtual_mode=True` treats every path the model passes to file tools
    (write_file/read_file/ls/glob/grep) as already relative to `root_dir`, so
    `/foo.py` and `foo.py` both land at `{root_dir}/foo.py`. But the shell tool
    runs with `cwd=root_dir` too, and a real `pwd` there prints root_dir's real
    absolute path (e.g. `/project/workspace`). A model that shells out, sees
    that path, and then reuses it verbatim in a file tool call (instead of a
    root_dir-relative path) gets it silently re-anchored under root_dir a
    second time -- e.g. `/project/workspace/foo.py` resolves to
    `{root_dir}/project/workspace/foo.py`. This is exactly the
    project/workspace/project/workspace nesting bug: two tools share one real
    directory but expose two different path namespaces, and nothing in either
    tool's response surfaces the mismatch for the model to notice.

    Telling the model not to do this (see AGENTS.md) helps but isn't reliable.
    This strips a literal root_dir prefix off incoming paths before the parent
    class's virtual resolution runs, so both conventions land in the same
    place instead of nesting.

    Upstream-API coupling: the de-nesting hooks the parent's *private*
    `_resolve_path`. A deepagents upgrade that renames or drops it would leave
    this override dead (never called) or break the `super()` call — silently
    reintroducing the nesting bug. `__init__` asserts the parent still defines
    it, so an upstream change fails loud at construction instead of at runtime
    (see tests/test_agent.py::test_backend_guards_upstream_resolve_path).
    """

    def __init__(self, *args, **kwargs):
        if not any(
            "_resolve_path" in klass.__dict__ for klass in LocalShellBackend.__mro__
        ):
            raise RuntimeError(
                "deepagents LocalShellBackend no longer defines _resolve_path; the "
                "path de-nesting in _WorkspaceShellBackend is dead. Re-check the "
                "upstream backend API and update the override."
            )
        super().__init__(*args, **kwargs)

    def _resolve_path(self, key: str) -> Path:
        if self.virtual_mode:
            vpath = key if key.startswith("/") else "/" + key
            marker = "/" + str(self.cwd).lstrip("/")
            if vpath == marker or vpath.startswith(marker + "/"):
                key = vpath[len(marker):] or "/"
        return super()._resolve_path(key)


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
    backend = _WorkspaceShellBackend(
        root_dir=str(workspace),
        virtual_mode=True,
        inherit_env=False,
        env=_agent_shell_env(),
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
