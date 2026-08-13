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

import os

import pytest

pytest.importorskip("deepagents")  # image-only; skipped on a bare host

from _bootstrap import _load  # noqa: E402

agent = _load("harness.agent")


# --- resolve_workspace (container trust boundary) --------------------------

def test_resolve_workspace_outside_container_allows_any_path(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)
    assert agent.resolve_workspace(str(tmp_path)) == tmp_path.resolve()


@pytest.mark.skipif(os.name == "nt", reason="posix container paths: '/project' resolves to C:\\project on Windows")
def test_resolve_workspace_in_container_accepts_under_project(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    assert agent.resolve_workspace("/project/workspace") == \
        __import__("pathlib").Path("/project/workspace")


@pytest.mark.skipif(os.name == "nt", reason="posix container paths: '/project' resolves to C:\\project on Windows")
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

def test_agent_shell_env_allowlists_known_vars_drops_the_rest(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")  # secret suffix
    monkeypatch.setenv("SOME_TOKEN", "t")                  # secret suffix
    monkeypatch.setenv("DB_PASSWORD", "p")                 # secret suffix
    monkeypatch.setenv("GITHUB_PAT", "ghp_x")              # secret, no suffix
    monkeypatch.setenv("RANDOM_UNLISTED", "x")             # not a secret, not allowed
    monkeypatch.setenv("EDITOR", "vim")                    # allowlisted exact
    monkeypatch.setenv("CONDA_PREFIX", "/opt/conda")       # allowlisted prefix
    monkeypatch.setenv("LC_ALL", "C.UTF-8")                # allowlisted prefix
    env = agent._agent_shell_env()
    # Anything not on the allowlist is gone — including a secret a suffix
    # denylist would have missed (GITHUB_PAT) and a benign-but-unlisted var.
    for dropped in ("ANTHROPIC_API_KEY", "SOME_TOKEN", "DB_PASSWORD", "GITHUB_PAT", "RANDOM_UNLISTED"):
        assert dropped not in env
    assert env.get("EDITOR") == "vim"
    assert env.get("CONDA_PREFIX") == "/opt/conda"
    assert env.get("LC_ALL") == "C.UTF-8"


def test_agent_shell_env_strips_registry_provider_keys(monkeypatch):
    # Every provider's api_key_env must be scrubbed. OPENAI_API_KEY isn't on the
    # allowlist anyway, but the explicit provider-key drop guards a user who
    # allowlists a prefix that would otherwise admit it.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert "OPENAI_API_KEY" not in agent._agent_shell_env()


def test_agent_shell_env_user_can_add_exact_var(monkeypatch):
    # A bare name in DEEPAGENTS_SHELL_ENV_ALLOW is passed through, even though it
    # isn't a default and even though it looks like a secret — naming it exactly
    # is explicit opt-in.
    monkeypatch.setenv("MYAPP_URL", "https://x")
    monkeypatch.setenv("MYAPP_API_KEY", "opted-in")
    monkeypatch.setenv(agent._SHELL_ENV_ALLOW_VAR, "MYAPP_URL, MYAPP_API_KEY")
    env = agent._agent_shell_env()
    assert env.get("MYAPP_URL") == "https://x"
    assert env.get("MYAPP_API_KEY") == "opted-in"


def test_agent_shell_env_user_prefix_still_backstops_secrets(monkeypatch):
    # A trailing '*' adds a prefix. Ordinary matches pass, but a credential that
    # merely sits under that prefix is still dropped (only an exact name bypasses
    # the backstop).
    monkeypatch.setenv("MYAPP_REGION", "us")
    monkeypatch.setenv("MYAPP_TOKEN", "leaky")
    monkeypatch.setenv(agent._SHELL_ENV_ALLOW_VAR, "MYAPP_*")
    env = agent._agent_shell_env()
    assert env.get("MYAPP_REGION") == "us"
    assert "MYAPP_TOKEN" not in env


def test_agent_shell_env_always_sets_path(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv(agent._SHELL_ENV_ALLOW_VAR, raising=False)
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


def test_final_message_text_drops_thinking_parts():
    # Regression: Gemma/Anthropic emit a reasoning part alongside the answer text.
    # The old str(item) fallback leaked "{'type': 'thinking', ...}" into the reply
    # (and the headless JSON). Only the text part is the answer.
    msg = _Msg([
        {"type": "thinking", "thinking": "the user wants the sum; 17+25=42"},
        {"type": "text", "text": "42"},
    ])
    out = agent.final_message_text({"messages": [msg]})
    assert out == "42"
    assert "thinking" not in out


def test_final_message_text_drops_unknown_non_text_parts():
    # Non-text parts with no text (e.g. a tool_use block) contribute nothing,
    # rather than being stringified into the answer.
    msg = _Msg([{"type": "tool_use", "name": "x", "input": {}}, {"type": "text", "text": "done"}])
    assert agent.final_message_text({"messages": [msg]}) == "done"


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


# --- _WorkspaceShellBackend on_path_denied seam (M4 slice D) ---------------
#
# deepagents' own LocalShellBackend._resolve_path already rejects most
# traversal/absolute-escape forms before our override's pathguard check even
# runs (pathguard is a belt-and-suspenders backstop, per its module docstring).
# So to exercise OUR guard-calling logic in isolation -- independent of exactly
# which escape shapes the current upstream version happens to catch first --
# these stub out the parent resolution to simulate an upstream that returns an
# out-of-bounds path unchecked.

def test_backend_calls_on_path_denied_on_escape(workspace_sandbox, monkeypatch):
    outside = str(workspace_sandbox.parent / "evil" / "x")
    monkeypatch.setattr(agent.LocalShellBackend, "_resolve_path", lambda self, key: outside)

    seen = {}

    def _on_denied(resolved, base):
        seen["resolved"] = resolved
        seen["base"] = base
        return False  # never approve -- matches hitl.make_path_denied_handler

    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={},
        on_path_denied=_on_denied,
    )
    with pytest.raises(agent.PathGuardDenied):
        backend._resolve_path("whatever")
    assert seen == {"resolved": outside, "base": str(workspace_sandbox)}


def test_backend_honors_true_return_from_on_path_denied(workspace_sandbox, monkeypatch):
    # Generic seam test: if a callback ever DOES approve (True), the backend
    # must not re-raise. hitl.make_path_denied_handler never returns True in
    # v1 (tested separately) -- this proves the backend-side contract only.
    outside = str(workspace_sandbox.parent / "evil" / "x")
    monkeypatch.setattr(agent.LocalShellBackend, "_resolve_path", lambda self, key: outside)

    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={},
        on_path_denied=lambda resolved, base: True,
    )
    backend._resolve_path("whatever")  # should not raise


def test_backend_denies_escape_without_a_handler(workspace_sandbox, monkeypatch):
    # M4 invariant 18 (off-HITL = plain refusal). HITL off means on_path_denied is
    # None -- the guard must still refuse. Previously only asserted indirectly via
    # cli._should_audit_path_denials returning False, which says nothing about
    # what the backend actually does.
    outside = str(workspace_sandbox.parent / "evil" / "x")
    monkeypatch.setattr(agent.LocalShellBackend, "_resolve_path", lambda self, key: outside)

    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={},
        on_path_denied=None,
    )
    with pytest.raises(agent.PathGuardDenied):
        backend._resolve_path("whatever")


@pytest.mark.parametrize("handler", [None, lambda resolved, base: False])
def test_backend_reports_denial_on_stderr(workspace_sandbox, monkeypatch, capsys, handler):
    # A workspace escape is never silent to the operator, HITL or not. Without
    # this the only trace off-HITL is the tool-error string the MODEL reads back,
    # which it can quietly route around. Must be stderr -- stdout is the headless
    # JSON result contract.
    outside = str(workspace_sandbox.parent / "evil" / "x")
    monkeypatch.setattr(agent.LocalShellBackend, "_resolve_path", lambda self, key: outside)

    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={},
        on_path_denied=handler,
    )
    with pytest.raises(agent.PathGuardDenied):
        backend._resolve_path("whatever")

    captured = capsys.readouterr()
    assert "path-guard DENIED" in captured.err
    assert captured.out == ""


def test_backend_stays_quiet_when_a_handler_approves(workspace_sandbox, monkeypatch, capsys):
    # The denial line reports a REFUSAL. An approved access (unreachable in v1,
    # see hitl.make_path_denied_handler) is not one, so it must not print.
    outside = str(workspace_sandbox.parent / "evil" / "x")
    monkeypatch.setattr(agent.LocalShellBackend, "_resolve_path", lambda self, key: outside)

    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace_sandbox), virtual_mode=True, inherit_env=False, env={},
        on_path_denied=lambda resolved, base: True,
    )
    backend._resolve_path("whatever")

    assert "DENIED" not in capsys.readouterr().err


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


# --- optional tool-shedding (DEEPAGENTS_LEAN_TOOLS / _EXCLUDE_TOOLS) ----------

def test_excluded_tools_env_off_by_default():
    assert agent.excluded_tools_from_env(env={}) == frozenset()


def test_lean_tools_flag_sheds_task_and_todos():
    got = agent.excluded_tools_from_env(env={"DEEPAGENTS_LEAN_TOOLS": "1"})
    assert got == frozenset({"task", "write_todos"})


def test_explicit_exclude_list_and_lean_combine():
    got = agent.excluded_tools_from_env(
        env={"DEEPAGENTS_LEAN_TOOLS": "true", "DEEPAGENTS_EXCLUDE_TOOLS": "grep, glob"}
    )
    assert got == frozenset({"task", "write_todos", "grep", "glob"})


class _FakeModelRequest:
    def __init__(self, tools):
        self.tools = tools
    def override(self, *, tools):
        return _FakeModelRequest(tools)


class _NamedTool:
    def __init__(self, name):
        self.name = name


def test_exclude_middleware_filters_named_tools():
    mw = agent._ExcludeToolsMiddleware(frozenset({"task", "write_todos"}))
    req = _FakeModelRequest([_NamedTool("execute"), _NamedTool("task"),
                             _NamedTool("write_todos"), {"function": {"name": "grep"}}])
    seen = {}
    def handler(r):
        seen["names"] = [agent._tool_display_name(t) for t in r.tools]
        return "resp"
    out = mw.wrap_model_call(req, handler)
    assert out == "resp"
    assert seen["names"] == ["execute", "grep"]  # task + write_todos stripped


def test_exclude_middleware_noop_when_empty():
    mw = agent._ExcludeToolsMiddleware(frozenset())
    req = _FakeModelRequest([_NamedTool("execute")])
    mw.wrap_model_call(req, lambda r: r.tools)
    # empty exclusion must not touch the request
    assert [agent._tool_display_name(t) for t in req.tools] == ["execute"]


def test_build_agent_appends_exclusion_middleware_when_env_set(workspace_sandbox, monkeypatch):
    captured = {}
    monkeypatch.setattr(agent, "create_deep_agent", lambda **kw: captured.update(kw) or "AGENT")
    monkeypatch.setenv("DEEPAGENTS_LEAN_TOOLS", "1")
    agent.build_agent("model:x", workspace_sandbox)
    mw = captured["middleware"]
    assert any(isinstance(m, agent._ExcludeToolsMiddleware) for m in mw)
    # and absent when the knob is off
    captured.clear()
    monkeypatch.delenv("DEEPAGENTS_LEAN_TOOLS", raising=False)
    agent.build_agent("model:x", workspace_sandbox)
    assert not any(isinstance(m, agent._ExcludeToolsMiddleware) for m in captured["middleware"])


# --- M4 slice H backstop: the namespace guard on the shell tool ---------------
# The seccomp relaxation the jail needs is applied to the WHOLE container, so it
# hands the agent's shell the same five syscalls. These cover the backend seam;
# the matcher itself is host-tested in tests/test_nsguard.py.

def _ns_backend(workspace, **kw):
    return agent._WorkspaceShellBackend(
        root_dir=str(workspace), virtual_mode=True, inherit_env=False, env={}, **kw
    )


def test_shell_refuses_namespace_command_under_the_jail(workspace_sandbox, monkeypatch, capsys):
    monkeypatch.setattr(agent.jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(agent.jail, "already_jailed", lambda: False)
    monkeypatch.delenv("DEEPAGENTS_NS_GUARD", raising=False)
    monkeypatch.setattr(
        agent.LocalShellBackend, "execute",
        lambda self, command, **kw: pytest.fail("command must not reach the shell"),
    )
    backend = _ns_backend(workspace_sandbox)
    with pytest.raises(agent.nsguard.NamespaceGuardDenied):
        backend.execute("unshare -Ur /bin/sh")
    # never silent to the operator, and on stderr so headless stdout stays clean
    err = capsys.readouterr().err
    assert "ns-guard DENIED" in err


def test_shell_guard_is_inert_with_the_jail_off(workspace_sandbox, monkeypatch):
    # Removable contract: no jail -> no seccomp relaxation -> nothing to
    # compensate for, so the shell behaves exactly as it did in M3.
    monkeypatch.setattr(agent.jail, "jail_enabled", lambda: False)
    monkeypatch.setattr(agent.jail, "already_jailed", lambda: False)
    monkeypatch.delenv("DEEPAGENTS_NS_GUARD", raising=False)
    seen = {}
    monkeypatch.setattr(
        agent.LocalShellBackend, "execute",
        lambda self, command, **kw: seen.update(cmd=command) or "ran",
    )
    backend = _ns_backend(workspace_sandbox)
    assert backend.execute("unshare -Ur /bin/sh") == "ran"
    assert seen["cmd"] == "unshare -Ur /bin/sh"


def test_shell_guard_can_be_forced_on_without_the_jail(workspace_sandbox, monkeypatch):
    monkeypatch.setattr(agent.jail, "jail_enabled", lambda: False)
    monkeypatch.setattr(agent.jail, "already_jailed", lambda: False)
    monkeypatch.setenv("DEEPAGENTS_NS_GUARD", "1")
    backend = _ns_backend(workspace_sandbox)
    with pytest.raises(agent.nsguard.NamespaceGuardDenied):
        backend.execute("mount -o bind /a /b")


def test_shell_guard_warn_mode_records_but_still_runs(workspace_sandbox, monkeypatch, capsys):
    monkeypatch.setattr(agent.jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(agent.jail, "already_jailed", lambda: False)
    monkeypatch.setenv("DEEPAGENTS_NS_GUARD", "warn")
    monkeypatch.setattr(agent.LocalShellBackend, "execute", lambda self, command, **kw: "ran")
    calls = []
    backend = _ns_backend(
        workspace_sandbox,
        on_command_denied=lambda match, reason, mode: calls.append((match, mode)),
    )
    assert backend.execute("unshare -Ur sh") == "ran"
    assert calls == [("unshare", "warn")]
    assert "ns-guard WARNING" in capsys.readouterr().err


def test_shell_guard_notifies_the_audit_seam_on_block(workspace_sandbox, monkeypatch):
    monkeypatch.setattr(agent.jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(agent.jail, "already_jailed", lambda: False)
    monkeypatch.delenv("DEEPAGENTS_NS_GUARD", raising=False)
    calls = []
    backend = _ns_backend(
        workspace_sandbox,
        on_command_denied=lambda match, reason, mode: calls.append((match, reason, mode)),
    )
    with pytest.raises(agent.nsguard.NamespaceGuardDenied):
        backend.execute("nsenter -t 1 -m sh")
    assert len(calls) == 1
    match, reason, mode = calls[0]
    assert match == "nsenter" and mode == "block"


def test_shell_guard_lets_ordinary_commands_through(workspace_sandbox, monkeypatch):
    monkeypatch.setattr(agent.jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(agent.jail, "already_jailed", lambda: False)
    monkeypatch.delenv("DEEPAGENTS_NS_GUARD", raising=False)
    monkeypatch.setattr(agent.LocalShellBackend, "execute", lambda self, command, **kw: "ran")
    backend = _ns_backend(workspace_sandbox)
    assert backend.execute("pytest tests/ -v") == "ran"


# --- Milestone 6: the telemetry sink is agent-unreachable (invariants 14/15) ---
#
# Telemetry is an audit surface, so the audited party must not be able to rewrite
# the record (milestone6.md §5a) -- the same reasoning M4 slice D applied to
# denials.jsonl. This is the leg that holds UNCONDITIONALLY: the file tools are
# rooted at the workspace and cannot address the state dir. The shell leg holds
# only under DEEPAGENTS_JAIL=1, and is asserted in the jail tests, not here.


def test_telemetry_files_resolve_under_the_state_dir(tmp_path, monkeypatch):
    """Invariant 14, on the RESOLVED paths rather than a string prefix."""
    archive = _load("harness.archive")
    tm = _load("harness.telemetry")

    state = tmp_path / "outside-state"
    monkeypatch.setenv("DEEPAGENTS_STATE_DIR", str(state))
    workspace = tmp_path / "ws"
    workspace.mkdir()

    resolved_state = archive.state_dir(workspace).resolve()
    for path in (tm.usage_path(resolved_state), tm.session_path(resolved_state)):
        assert path.parent == resolved_state
        assert workspace.resolve() not in path.resolve().parents


def test_file_tools_cannot_reach_the_telemetry_sink(tmp_path, monkeypatch):
    """Invariant 15: a write_file/read_file aimed at the resolved usage.jsonl is
    refused OR redirected back inside the workspace — either way the sink is not
    what the tool addresses. (On Linux, upstream's `_resolve_path` re-roots the
    absolute path under the workspace rather than raising; the property under
    test is the same, so assert the property, not the mechanism.)

    Driven through the same backend the agent's file tools use, with the state
    dir OUTSIDE the workspace -- which is the configuration run-docker always
    produces and the one the placement decision assumes."""
    archive = _load("harness.archive")
    tm = _load("harness.telemetry")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("DEEPAGENTS_STATE_DIR", str(state))

    sink = tm.usage_path(archive.state_dir(workspace))
    sink.write_text('{"schema": 1}\n', encoding="utf-8")

    backend = agent._WorkspaceShellBackend(
        root_dir=str(workspace), virtual_mode=True, inherit_env=False, env={}
    )
    # Whether upstream's own resolution refuses it first or our pathguard does,
    # the property under test is the same: the path does not resolve to the sink.
    import pathlib

    try:
        resolved = backend._resolve_path(str(sink))
    except Exception:
        return  # refused outright -- the strongest form of the same guarantee
    assert pathlib.Path(resolved).resolve() != sink.resolve(), (
        "a file tool addressed the telemetry sink -- the agent could edit its own record"
    )


def test_telemetry_adds_no_second_state_dir_guard():
    """Invariant 19: state_dir() falls back to <workspace>/.deepagents when
    DEEPAGENTS_STATE_DIR is unset, which would put the sink back inside the mount.
    `harness doctor` already errors on that; telemetry must INHERIT that check
    rather than grow a second, divergent one."""
    import inspect

    tm = _load("harness.telemetry")
    source = inspect.getsource(tm)
    # No re-implementation of the workspace-containment test in telemetry.py.
    assert "is_relative_to" not in source
    assert "DEEPAGENTS_STATE_DIR" not in source
    doctor = _load("harness.doctor")
    assert "state" in inspect.getsource(doctor)
