"""Tests for harness/agent.py — workspace boundary, secret scrubbing, prompt.

agent.py imports `deepagents` at module top, so the whole module is gated behind
importorskip: it runs in the runtime/test image (deps present) and is skipped on
a bare host. The cases here are the security- and correctness-critical pure
logic — the container workspace boundary, the credential scrub on the agent's
shell env, final-message extraction, and the AGENTS.md prompt append — none of
which call a model or the network.

All filesystem work goes through tmp_path / the workspace_sandbox fixture, both
auto-removed, so nothing lands in the repo or the mounted workspace.
"""

from __future__ import annotations

import pytest

pytest.importorskip("deepagents")  # image-only; skipped on a bare host

from _bootstrap import _load  # noqa: E402

agent = _load("harness.agent")


# --- resolve_workspace (container trust boundary) --------------------------

def test_resolve_workspace_outside_container_allows_any_path(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)
    assert agent.resolve_workspace(str(tmp_path)) == tmp_path.resolve()


def test_resolve_workspace_in_container_accepts_under_project(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    assert agent.resolve_workspace("/project/workspace") == \
        __import__("pathlib").Path("/project/workspace")


def test_resolve_workspace_in_container_accepts_project_root(monkeypatch):
    # is_relative_to must treat /project itself as inside (the old startswith
    # check wrongly rejected it).
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    assert str(agent.resolve_workspace("/project")) == "/project"


def test_resolve_workspace_in_container_rejects_outside(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    with pytest.raises(SystemExit) as exc:
        agent.resolve_workspace("/home/agent")
    assert "invalid inside the container" in str(exc.value)


def test_resolve_workspace_in_container_rejects_lookalike_prefix(monkeypatch):
    # /project-evil must NOT pass as "inside /project".
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    with pytest.raises(SystemExit):
        agent.resolve_workspace("/project-evil")


# --- _agent_shell_env (credential scrub before the shell tool) -------------

def test_agent_shell_env_strips_secrets_keeps_normal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("SOME_TOKEN", "t")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("AWS_SECRET", "s")
    monkeypatch.setenv("EDITOR", "vim")  # ordinary var must survive
    env = agent._agent_shell_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "SOME_TOKEN" not in env
    assert "DB_PASSWORD" not in env
    assert "AWS_SECRET" not in env
    assert env.get("EDITOR") == "vim"


def test_agent_shell_env_strips_registry_provider_keys(monkeypatch):
    # Every provider's api_key_env must be scrubbed even if it didn't match a
    # suffix. OPENAI_API_KEY is a registry key (and also *_API_KEY), so it's gone.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert "OPENAI_API_KEY" not in agent._agent_shell_env()


def test_agent_shell_env_always_sets_path(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    assert agent._agent_shell_env()["PATH"]  # falls back to a sane default


# --- final_message_text ----------------------------------------------------

class _Msg:
    def __init__(self, content):
        self.content = content


def test_final_message_text_string_content():
    result = {"messages": [_Msg("hi"), _Msg("final answer")]}
    assert agent.final_message_text(result) == "final answer"


def test_final_message_text_list_content_joins_text_parts():
    msg = _Msg([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert agent.final_message_text({"messages": [msg]}) == "a\nb"


def test_final_message_text_dict_message_content():
    result = {"messages": [{"content": "from dict"}]}
    assert agent.final_message_text(result) == "from dict"


def test_final_message_text_no_messages_stringifies():
    assert agent.final_message_text({"messages": []}) == "{'messages': []}"


def test_final_message_text_non_dict_result():
    assert agent.final_message_text("raw") == "raw"


# --- _WorkspaceShellBackend path nesting fix -------------------------------

def test_backend_strips_real_root_prefix_to_avoid_nesting(workspace_sandbox):
    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={}
    )
    # A model that echoes the real cwd back into a file-tool path must land in
    # the same place as the root-relative form, not nested a second time.
    nested = backend._resolve_path(f"{workspace_sandbox}/foo.py")
    relative = backend._resolve_path("foo.py")
    assert nested == relative


def test_backend_guards_upstream_resolve_path(workspace_sandbox):
    # Regression sentinel: constructing the backend asserts the upstream parent
    # still defines _resolve_path. If a deepagents upgrade drops/renames it, the
    # de-nesting override goes dead — this construction must fail loud instead.
    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={}
    )
    assert any(
        "_resolve_path" in klass.__dict__
        for klass in type(backend).__mro__[1:]  # excludes our override
    )


# --- build_agent prompt assembly (AGENTS.md append) ------------------------

def test_build_agent_appends_agents_md(workspace_sandbox, monkeypatch):
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(agent, "create_deep_agent", _spy)
    # workspace_sandbox chdirs to tmp_path; AGENTS.md is read from cwd.
    (workspace_sandbox.parent / "AGENTS.md").write_text(
        "PROJECT RULES", encoding="utf-8"
    )
    out = agent.build_agent("model:x", workspace_sandbox)
    assert out == "AGENT"
    assert captured["system_prompt"].startswith(agent.BASE_SYSTEM_PROMPT)
    assert "PROJECT RULES" in captured["system_prompt"]


def test_build_agent_without_agents_md_uses_base_prompt(workspace_sandbox, monkeypatch):
    captured = {}
    monkeypatch.setattr(agent, "create_deep_agent", lambda **kw: captured.update(kw))
    agent.build_agent("model:x", workspace_sandbox)
    assert captured["system_prompt"] == agent.BASE_SYSTEM_PROMPT
