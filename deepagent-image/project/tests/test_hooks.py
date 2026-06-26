"""Tests for harness/hooks.py — lifecycle hook dispatch.

hooks.py imports the LangChain middleware base, so the module is gated behind
importorskip (runs in the runtime/test image, skipped on a bare host). No real
subprocess is spawned: `_run_hook_commands` is exercised against a stubbed
subprocess.run, and the middleware's event routing against a stubbed
`_run_hook_commands`, so the tests assert *which* commands fire on *which* event
without side effects.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain.agents.middleware.types")  # image-only

from _bootstrap import _load  # noqa: E402

hooks = _load("harness.hooks")


# --- _run_hook_commands ----------------------------------------------------

def test_run_hook_commands_runs_each_with_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(
        hooks.subprocess, "run",
        lambda cmd, **kw: calls.append((cmd, kw)),
    )
    hooks._run_hook_commands(["one", "two"])
    assert [c[0] for c in calls] == ["one", "two"]          # order preserved
    assert all(c[1]["shell"] is True for c in calls)        # shell=True
    assert all(c[1]["check"] is False for c in calls)       # never raises on rc


def test_run_hook_commands_empty_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **k: calls.append(a))
    hooks._run_hook_commands([])
    assert calls == []


# --- ShellHooksMiddleware event routing ------------------------------------

@pytest.fixture
def recorder(monkeypatch):
    """Capture the command lists handed to _run_hook_commands per call."""
    fired = []
    monkeypatch.setattr(hooks, "_run_hook_commands", lambda cmds: fired.append(cmds))
    return fired


def _mw(by_event):
    return hooks.ShellHooksMiddleware(by_event)


def test_before_agent_fires_agent_start(recorder):
    _mw({"agent.start": ["a"]}).before_agent(None, None)
    assert recorder == [["a"]]


def test_after_agent_fires_agent_end(recorder):
    _mw({"agent.end": ["z"]}).after_agent(None, None)
    assert recorder == [["z"]]


def test_model_events_route_to_their_lists(recorder):
    mw = _mw({"model.start": ["s"], "model.end": ["e"]})
    mw.before_model(None, None)
    mw.after_model(None, None)
    assert recorder == [["s"], ["e"]]


def test_unconfigured_event_fires_empty(recorder):
    _mw({"agent.start": ["a"]}).after_agent(None, None)  # no agent.end declared
    assert recorder == [[]]


def test_wrap_tool_call_brackets_handler_and_returns_result(recorder):
    mw = _mw({"tool.start": ["s"], "tool.end": ["e"]})
    seen = []

    def handler(req):
        seen.append(("handler", req))
        return "RESULT"

    out = mw.wrap_tool_call("REQ", handler)
    assert out == "RESULT"
    # tool.start fires, then the handler, then tool.end.
    assert recorder == [["s"], ["e"]]
    assert seen == [("handler", "REQ")]


def test_wrap_tool_call_runs_tool_end_even_on_error(recorder):
    mw = _mw({"tool.start": ["s"], "tool.end": ["e"]})

    def boom(req):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        mw.wrap_tool_call("REQ", boom)
    assert recorder == [["s"], ["e"]]  # tool.end still ran via the finally


# --- build_hook_middleware -------------------------------------------------

def test_build_returns_middleware_for_non_session_events():
    out = hooks.build_hook_middleware({"tool.start": ["x"]})
    assert len(out) == 1
    assert isinstance(out[0], hooks.ShellHooksMiddleware)


def test_build_empty_for_session_only_events():
    # session.start/.end fire in main(), not via middleware -> no middleware.
    assert hooks.build_hook_middleware({"session.start": ["x"], "session.end": ["y"]}) == []


def test_build_empty_for_no_events():
    assert hooks.build_hook_middleware({}) == []


def test_build_returns_middleware_when_mix_of_session_and_other():
    out = hooks.build_hook_middleware({"session.start": ["a"], "model.start": ["b"]})
    assert len(out) == 1  # the non-session event forces a middleware
