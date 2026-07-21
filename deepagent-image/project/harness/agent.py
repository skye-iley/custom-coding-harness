"""Agent construction: workspace resolution, system prompt, result extraction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware.types import AgentMiddleware

from harness.loaders import _read_optional_text
from harness.mask import append_deny
from harness.pathguard import PathGuardDenied, validate_path
from harness.providers import PROVIDERS

DEFAULT_TASK = "inspect workspace, summarize structure."

# Env var allowlist for the agent's shell tool. The workspace is a host
# bind-mount, so a prompt-injected agent that could read a credential via
# `printenv` could write it to host disk (secrets hard-rule). Rather than guess
# which vars are *secret* (a denylist misses any oddly-named key, e.g. GITHUB_PAT),
# we pass through only vars known to be *safe* — everything else is dropped.
# This replaces inherit_env=True, whose merge semantics would leak the whole env.
#
# Exact names: ordinary shell/session vars the agent's commands rely on.
_SHELL_ENV_ALLOW_EXACT = frozenset({
    "PATH", "HOME", "PWD", "OLDPWD", "SHELL", "SHLVL",
    "USER", "LOGNAME", "HOSTNAME", "TERM",
    "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TMP", "TEMP",
    "EDITOR", "VISUAL", "PAGER",
    "COLUMNS", "LINES",
})

# Name prefixes whose whole family is allowed: locale (LC_*), the workspace
# conda/mamba stack (CONDA_*/MAMBA_* — the two-stack env the agent builds in),
# and git config (GIT_*). None of these families carry credentials in practice;
# the secret-suffix backstop below still guards against a stray one.
_SHELL_ENV_ALLOW_PREFIXES = ("LC_", "CONDA_", "MAMBA_", "GIT_")

# User extension knob: a comma/space-separated list of additional vars to pass
# through. A bare name is an exact allow; a trailing '*' makes it a prefix
# (e.g. "MYAPP_URL,MYAPP_*"). Set it in project/.env like any other DEEPAGENTS_*.
_SHELL_ENV_ALLOW_VAR = "DEEPAGENTS_SHELL_ENV_ALLOW"

# Backstop applied to *prefix* and default matches only: a var that merely sits
# under an allowed prefix must still not leak if it looks like a credential.
# An explicitly user-named exact var overrides this (naming it is opt-in intent).
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def _user_allowlist() -> tuple[frozenset[str], tuple[str, ...]]:
    """Parse `DEEPAGENTS_SHELL_ENV_ALLOW` into (exact names, prefixes)."""
    exact: set[str] = set()
    prefixes: list[str] = []
    for item in os.getenv(_SHELL_ENV_ALLOW_VAR, "").replace(",", " ").split():
        if item.endswith("*"):
            prefixes.append(item[:-1])
        else:
            exact.add(item)
    return frozenset(exact), tuple(prefixes)


def _agent_shell_env() -> dict[str, str]:
    """Env handed to the agent's shell tool — an allowlist (see above).

    A var passes if it is an allowed exact name or sits under an allowed prefix
    (defaults plus anything the user added via DEEPAGENTS_SHELL_ENV_ALLOW).
    Provider credentials and secret-suffixed vars are still dropped even when a
    prefix would admit them, unless the user named that exact var explicitly.
    """
    user_exact, user_prefixes = _user_allowlist()
    allow_prefixes = _SHELL_ENV_ALLOW_PREFIXES + user_prefixes
    provider_keys = {p.api_key_env for p in PROVIDERS}

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in user_exact:
            env[key] = value  # explicit opt-in wins over the secret backstop
            continue
        if key not in _SHELL_ENV_ALLOW_EXACT and not key.startswith(allow_prefixes):
            continue
        if key in provider_keys or key.endswith(_SECRET_ENV_SUFFIXES):
            continue  # don't leak a credential that merely matched a prefix
        env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


BASE_SYSTEM_PROMPT = """You are an expert coding assistant operating inside a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files. You *only* check the previous sessions **if** there is some clear reference to them or clearly missing info, otherwise take the inputs as-is"""


# Convenience set for DEEPAGENTS_LEAN_TOOLS: the two biggest tool schemas the agent
# rarely needs for a simple task (~3k tokens/request between them, measured). The
# subagent `task` tool (~1.9k) and `write_todos` (~1.1k). Dropping them shrinks
# every model call — useful to fit a tight free-tier TPM while testing.
_LEAN_EXCLUDED_TOOLS = frozenset({"task", "write_todos"})


def _tool_display_name(tool) -> str | None:
    """A bound tool's name across the shapes a model request carries (BaseTool or
    an OpenAI-style ``{"function": {"name": ...}}`` / ``{"name": ...}`` dict)."""
    name = getattr(tool, "name", None)
    if name:
        return name
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return fn["name"]
        return tool.get("name")
    return None


def excluded_tools_from_env(env: dict | None = None) -> frozenset[str]:
    """Tool names to hide from the model, from env (empty => hide nothing).

    ``DEEPAGENTS_EXCLUDE_TOOLS`` is a comma/space list of exact tool names;
    ``DEEPAGENTS_LEAN_TOOLS`` (truthy) adds the ``_LEAN_EXCLUDED_TOOLS`` set. A
    **test/token knob** — off by default, so a normal run offers the full toolset."""
    env = os.environ if env is None else env
    names = {t.strip() for t in env.get("DEEPAGENTS_EXCLUDE_TOOLS", "").replace(",", " ").split() if t.strip()}
    if env.get("DEEPAGENTS_LEAN_TOOLS", "").strip().lower() in ("1", "true", "yes", "on"):
        names |= _LEAN_EXCLUDED_TOOLS
    return frozenset(names)


class _ExcludeToolsMiddleware(AgentMiddleware):
    """Strip named tools from what the model sees on each call (token/test knob).

    Placed last in the stack so it filters tools *injected* by earlier deepagents
    middleware (subagent `task`, `write_todos`, filesystem tools). Only the model's
    view is narrowed — the tools still exist in the node — so this trims tool-schema
    tokens per request without touching execution paths. Mirrors deepagents' own
    internal ``_ToolExclusionMiddleware`` but avoids depending on that private API."""

    def __init__(self, excluded: frozenset[str]):
        super().__init__()
        self._excluded = frozenset(excluded)

    def _filter(self, request):
        if not self._excluded:
            return request
        kept = [t for t in request.tools if _tool_display_name(t) not in self._excluded]
        return request.override(tools=kept)

    def wrap_model_call(self, request, handler):
        return handler(self._filter(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._filter(request))


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

    def __init__(self, *args, on_path_denied=None, **kwargs):
        if not any(
            "_resolve_path" in klass.__dict__ for klass in LocalShellBackend.__mro__
        ):
            raise RuntimeError(
                "deepagents LocalShellBackend no longer defines _resolve_path; the "
                "path de-nesting in _WorkspaceShellBackend is dead. Re-check the "
                "upstream backend API and update the override."
            )
        self._on_path_denied = on_path_denied
        super().__init__(*args, **kwargs)

    def _resolve_path(self, key: str) -> Path:
        if self.virtual_mode:
            vpath = key if key.startswith("/") else "/" + key
            marker = "/" + str(self.cwd).lstrip("/")
            if vpath == marker or vpath.startswith(marker + "/"):
                key = vpath[len(marker):] or "/"
        resolved = super()._resolve_path(key)
        base = self.root_dir if hasattr(self, "root_dir") else str(self.cwd)
        if base:
            try:
                validate_path(str(resolved), base)
            except PathGuardDenied:
                if self._on_path_denied:
                    self._on_path_denied(str(resolved), base)
                raise
        return resolved


def make_recall_past_tool(conn, default_topic: str | None):
    """Build the `recall_past` agent tool over an open archive connection.

    Lets the model pull prior-session context mid-turn ("what did we decide about
    X"). Same `archive.recall()` the `/recall` REPL command uses; defaults to the
    session's continual topic, and the model may widen by passing a topic or "".
    Guarded by DEEPAGENTS_ARCHIVE at the call site (cli only builds it when the
    archive is enabled). Returns None if langchain's tool decorator is unavailable
    so a bare host still imports this module.
    """
    from langchain_core.tools import tool

    from harness import archive

    # Sentinel default so "omitted" (use the session topic) is distinguishable
    # from an explicit None/"" (widen to the whole archive).
    _UNSET = "\x00use-session-topic"

    @tool
    def recall_past(query: str, topic: str | None = _UNSET) -> str:
        """Search the PAST archive of prior sessions for context relevant to `query`.

        The past archive is a separate store that is NOT in your current context.
        Call this ONLY when the task clearly references earlier work you cannot see
        in this thread. `topic`: omit to search this session's topic lane; pass a
        specific topic name, or an empty string, to search the whole archive.
        Returns up to a few matching session summaries with truncated transcript
        excerpts (the slice is capped to protect the context window).
        """
        scope = default_topic if topic == _UNSET else (topic or None)
        hits = archive.recall(conn, query, topic=scope, limit=5)
        return archive.format_hits(hits, with_turns=True) or "(no matching past sessions)"

    return recall_past


def make_refresh_workspace_tool(workspace: Path):
    """Build the `refresh_workspace` agent tool (ephemeral runs only).

    Lets the model pull live host edits from the read-only source mount into its
    working copy mid-turn — e.g. to pick up a file a human changed while it was
    working. Source wins on conflict; the copy stays throwaway (reverted at close).
    cli only builds this when the source mount is present (`refresh.workspace_src()`
    is not None). Returns None if langchain's tool decorator is unavailable so a
    bare host still imports this module.
    """
    from langchain_core.tools import tool

    from harness import refresh as refresh_mod

    @tool
    def refresh_workspace(path: str | None = None) -> str:
        """Pull the latest host-side edits into your workspace (ephemeral runs only).

        Your workspace is a throwaway COPY; a human may edit the real files on the
        host while you work, and those edits are NOT visible to you until you sync.
        Call this to pull them in. `path`: omit to refresh everything, or pass a
        single file/dir to refresh just that. The host copy WINS on conflict, so
        your own unsaved edits to the same file are overwritten. Returns a short
        summary of what changed.
        """
        src = refresh_mod.workspace_src()
        if src is None:
            return "refresh unavailable: not an ephemeral run (no source mount)."
        try:
            written = refresh_mod.refresh_into(workspace, src, path)
        except (ValueError, FileNotFoundError) as exc:
            return f"refresh failed: {exc}"
        scope = f" under {path!r}" if path else ""
        return f"Refreshed {len(written)} file(s) from the host{scope}."

    return refresh_workspace


def make_mask_add_tool(state_dir: str | Path | None = None):
    """Build the `mask_add` agent tool (raise-only, next-run).

    Lets the model add a path to the state-dir authoritative config so it is
    masked on the *next* run. Cannot unmask a path in the current session.
    Gated behind DEEPAGENTS_MASK != 0. Returns None if langchain's tool
    decorator is unavailable.
    """
    from langchain_core.tools import tool

    from harness.mask import append_deny, append_floor

    if state_dir is None:
        sd = os.environ.get("DEEPAGENTS_STATE_DIR", "")
        state_dir = Path(sd) if sd else None

    @tool
    def mask_add(path: str, tier: str = "deny") -> str:
        """Add PATH to the mask set (state-dir authoritative config).

        Takes effect on the NEXT run — the current session's mask is frozen
        at launch. `tier` is "deny" (pattern-default/general tier) or "floor"
        (designated-secret tier, never negatable). Floor is for credentials
        that must never leak; deny is for everything else. The operator can
        always override next launch by editing .agentignore.
        """
        import os
        from pathlib import Path

        sd = state_dir
        if sd is None:
            raw = os.environ.get("DEEPAGENTS_STATE_DIR", "")
            sd = Path(raw) if raw else None
        if sd is None:
            return "mask_add: DEEPAGENTS_STATE_DIR not set — cannot persist"
        sd = Path(sd)
        sd.mkdir(parents=True, exist_ok=True)
        if tier == "floor":
            append_floor(str(sd), path)
            return f"added floor path '{path}' — will be masked next run (never negatable)"
        append_deny(str(sd), path)
        return f"added deny path '{path}' — will be masked next run"

    return mask_add


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
        # Content is a list of parts. Keep only the human-facing TEXT: a bare
        # string, or a {"type": "text", "text": ...} block. Non-text parts —
        # reasoning/thinking, tool_use, etc. — are NOT the answer and must not be
        # stringified into it. The old `str(item)` fallback leaked Gemma/Anthropic
        # 'thinking' dicts (e.g. "{'type': 'thinking', ...}") into the reply and the
        # headless JSON. Unknown part shapes are dropped, not dumped.
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict) and "text" in item and "type" not in item:
                # Some providers emit a typeless {"text": ...} block.
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part)

    return str(content)


def build_agent(
    model: Any,
    workspace: Path,
    tools: list | None = None,
    middleware: list[AgentMiddleware] | None = None,
    checkpointer: Any = None,
    on_path_denied: callable | None = None,
):
    workspace.mkdir(parents=True, exist_ok=True)
    backend = _WorkspaceShellBackend(
        root_dir=str(workspace),
        virtual_mode=True,
        inherit_env=False,
        env=_agent_shell_env(),
        on_path_denied=on_path_denied,
    )

    agents_md = _read_optional_text(Path.cwd() / "AGENTS.md")
    system_prompt = BASE_SYSTEM_PROMPT
    if agents_md:
        system_prompt += "\nAdditional project instructions from AGENTS.md:\n" + agents_md

    custom_tools = []
    custom_tools.extend(tools or [])

    mw = list(middleware or [])
    # Optional tool-shedding (DEEPAGENTS_LEAN_TOOLS / DEEPAGENTS_EXCLUDE_TOOLS):
    # appended LAST so it strips tools injected by deepagents' own middleware.
    excluded = excluded_tools_from_env()
    if excluded:
        mw.append(_ExcludeToolsMiddleware(excluded))

    return create_deep_agent(
        model=model,
        tools=custom_tools,
        middleware=mw,
        backend=backend,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
    )
