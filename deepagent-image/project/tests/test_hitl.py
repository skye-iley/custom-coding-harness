"""Tests for harness/hitl.py loop/channel logic (Milestone 3 S1/S2/S5/S7).

hitl.py degrades its AgentMiddleware base to ``object`` off-image and takes an
injected ``resume_fn`` + ``channel``, so the interrupt drain/resume loop, the REPL
channel, and the headless fail-closed resolution are host-testable without
langgraph/langchain. The tool builder and the actual middleware dispatch (image
only) are not exercised here.
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

it = _load("harness.interrupt")
cfg = _load("harness.config")
audit = _load("harness.audit")
archive = _load("harness.archive")
hitl = _load("harness.hitl")


def _cfg(level="guided", policy="blocking"):
    return cfg.parse_config(f"autonomy_level: {level}\ninterruption_policy: {policy}\n")


def _result_with(request):
    return {"__interrupt__": [{"value": request.to_dict()}]}


# --- extract -----------------------------------------------------------------

def test_extract_none_when_clean():
    assert hitl.extract_interrupts({"messages": []}) == []
    assert hitl.extract_interrupts("not a dict") == []


def test_extract_from_value_dict():
    r = it.new_request(it.KIND_APPROVE, "ok?")
    got = hitl.extract_interrupts(_result_with(r))
    assert len(got) == 1 and got[0].id == r.id


def test_extract_from_object_value():
    class _Interrupt:
        def __init__(self, value):
            self.value = value

    r = it.new_request(it.KIND_INPUT, "q")
    res = {"__interrupt__": [_Interrupt(r.to_dict())]}
    assert hitl.extract_interrupts(res)[0].id == r.id


# --- ReplChannel -------------------------------------------------------------

def test_repl_channel_approve():
    ch = hitl.ReplChannel(read_line=lambda p: "yes", emit=lambda s: None)
    r = it.new_request(it.KIND_APPROVE, "ok?")
    assert ch.ask(r) is True


def test_repl_channel_show_then_answer():
    replies = iter(["/show", "no"])
    shown = []
    ch = hitl.ReplChannel(read_line=lambda p: next(replies), emit=shown.append)
    r = it.new_request(it.KIND_APPROVE, "ok?", context="secret\ndetails")
    assert ch.ask(r) is False
    assert any("secret" in s for s in shown)  # /show expanded the context


def test_repl_channel_reprompts_on_garbage():
    replies = iter(["maybe", "y"])
    ch = hitl.ReplChannel(read_line=lambda p: next(replies), emit=lambda s: None)
    r = it.new_request(it.KIND_APPROVE, "ok?")
    assert ch.ask(r) is True


# --- ReplChannel arrow-key select (S6 PR-b) ----------------------------------

def test_select_used_for_choose_returns_pick():
    # A wired `select` resolves a `choose` without any typed input.
    ch = hitl.ReplChannel(
        read_line=lambda p: pytest.fail("should not read a line"),
        emit=lambda s: None,
        select=lambda req: req.options[1],
    )
    r = it.new_request(it.KIND_CHOOSE, "pick", options=("a", "b", "c"))
    assert ch.ask(r) == "b"


def test_select_none_falls_back_to_typed():
    # select returning None (menu cancelled / arrows off) => typed loop resolves it.
    ch = hitl.ReplChannel(
        read_line=lambda p: "c",
        emit=lambda s: None,
        select=lambda req: None,
    )
    r = it.new_request(it.KIND_CHOOSE, "pick", options=("a", "b", "c"))
    assert ch.ask(r) == "c"


def test_select_not_used_for_approve():
    # The arrow menu is a `choose`-only affordance; approve still reads a line.
    ch = hitl.ReplChannel(
        read_line=lambda p: "yes",
        emit=lambda s: None,
        select=lambda req: pytest.fail("select must not fire for approve"),
    )
    r = it.new_request(it.KIND_APPROVE, "ok?")
    assert ch.ask(r) is True


# --- PR gate (S2 session.end tier) -------------------------------------------

def test_should_gate_pr_true_when_interactive_gated_with_session():
    assert hitl.should_gate_pr(_cfg("guided"), interactive=True, has_session=True) is True
    assert hitl.should_gate_pr(_cfg("strict"), interactive=True, has_session=True) is True


def test_should_gate_pr_false_when_headless_or_no_session_or_off():
    # non-interactive => PR proceeds (never blocks CI); git-pr never auto-merges.
    assert hitl.should_gate_pr(_cfg("guided"), interactive=False, has_session=True) is False
    # no git session (no session.env) => nothing to gate.
    assert hitl.should_gate_pr(_cfg("guided"), interactive=True, has_session=False) is False
    # autonomous preset doesn't gate session.end.
    assert hitl.should_gate_pr(_cfg("autonomous"), interactive=True, has_session=True) is False
    # HITL off entirely.
    assert hitl.should_gate_pr(None, interactive=True, has_session=True) is False


def test_make_pr_gate_request_shape():
    r = hitl.make_pr_gate_request(branch="agent/x", base="main", summary="[commits]\nabc")
    assert r.kind == it.KIND_APPROVE
    assert "agent/x" in r.prompt and "main" in r.prompt
    assert r.context == "[commits]\nabc"          # summary is the /show context
    assert r.meta["gate"] == "pr" and r.default is None


# --- resolve_value -----------------------------------------------------------

def test_resolve_interactive_uses_channel():
    ch = hitl.ReplChannel(read_line=lambda p: "3", emit=lambda s: None)
    r = it.new_request(it.KIND_CHOOSE, "pick", options=("a", "b", "c"))
    value, by = hitl.resolve_value(r, channel=ch, headless=False, config=_cfg())
    assert value == "c" and by == "human"


def test_resolve_headless_default():
    r = it.new_request(it.KIND_INPUT, "q", default="fallback")
    value, by = hitl.resolve_value(r, channel=None, headless=True, config=_cfg())
    assert value == "fallback" and by == "headless-default"


def test_resolve_headless_deny_approve():
    r = it.new_request(it.KIND_APPROVE, "run rm?")
    value, by = hitl.resolve_value(r, channel=None, headless=True, config=_cfg("guided"))
    assert value is False and by == "denied"


def test_resolve_headless_strict_aborts():
    r = it.new_request(it.KIND_APPROVE, "run rm?")
    with pytest.raises(hitl.InterruptAborted) as ei:
        hitl.resolve_value(r, channel=None, headless=True, config=_cfg("strict", "blocking"))
    assert ei.value.exit_code == it.EXIT_INTERRUPT_ABORT


# --- run_interrupt_loop ------------------------------------------------------

def test_loop_single_interrupt_resolves_and_resumes(tmp_path):
    r = it.new_request(it.KIND_APPROVE, "ok?", source=it.SOURCE_DETERMINISTIC)
    resumed = {}

    def resume_fn(value):
        resumed["value"] = value
        return {"messages": ["done"]}  # clean, no __interrupt__

    ch = hitl.ReplChannel(read_line=lambda p: "yes", emit=lambda s: None)
    out = hitl.run_interrupt_loop(
        _result_with(r), resume_fn,
        channel=ch, headless=False, config=_cfg(), workspace=tmp_path,
    )
    assert out == {"messages": ["done"]}
    assert resumed["value"] is True
    # audited (S7)
    recs = audit.read_records(tmp_path)
    assert len(recs) == 1 and recs[0]["resolved_value"] == "True"


def test_loop_no_interrupt_is_passthrough(tmp_path):
    clean = {"messages": ["hi"]}
    out = hitl.run_interrupt_loop(
        clean, lambda v: pytest.fail("should not resume"),
        channel=None, headless=True, config=_cfg(), workspace=tmp_path,
    )
    assert out is clean
    assert audit.read_records(tmp_path) == []


def test_loop_chains_multiple_interrupts(tmp_path):
    r1 = it.new_request(it.KIND_INPUT, "first", default="a")
    r2 = it.new_request(it.KIND_INPUT, "second", default="b")
    seq = iter([_result_with(r2), {"messages": ["fin"]}])

    def resume_fn(value):
        return next(seq)

    out = hitl.run_interrupt_loop(
        _result_with(r1), resume_fn,
        channel=None, headless=True, config=_cfg(), workspace=tmp_path,
    )
    assert out == {"messages": ["fin"]}
    assert len(audit.read_records(tmp_path)) == 2  # both audited


def test_loop_headless_abort_propagates(tmp_path):
    r = it.new_request(it.KIND_APPROVE, "danger")
    with pytest.raises(hitl.InterruptAborted):
        hitl.run_interrupt_loop(
            _result_with(r), lambda v: pytest.fail("no resume"),
            channel=None, headless=True, config=_cfg("strict", "blocking"), workspace=tmp_path,
        )


# --- PauseMiddleware field extraction / gate (S2) ----------------------------
# Regression: the live langchain request carries the call under `.tool_call`
# ({"name","args","id"}), not top-level `.tool_name`/`.args`. Reading the wrong
# place made `command`/`name` always None, so under a non-strict preset the
# review_triggers gate matched nothing and `rm -rf` ran ungated.


class _FakeToolCallRequest:
    """Mirror langchain's ToolCallRequest.tool_call shape (name/args/id dict)."""

    def __init__(self, name, args, id="call-1"):
        self.tool_call = {"name": name, "args": args, "id": id}


def _cfg_with_triggers(*items, level="guided", on_deny="continue"):
    lines = [f"autonomy_level: {level}", f"on_deny: {on_deny}", "review_triggers:"]
    lines += [f"  - {{ on: {on}, pattern: {pat!r} }}" for on, pat in items]
    return cfg.parse_config("\n".join(lines) + "\n")


def test_tool_call_fields_reads_tool_call_dict():
    req = _FakeToolCallRequest("execute", {"command": "rm -rf /project/workspace"})
    name, values, command = hitl._tool_call_fields(req)
    assert name == "execute"
    assert command == "rm -rf /project/workspace"
    assert "rm -rf /project/workspace" in values


def test_command_trigger_gates_rm_rf_under_guided():
    # The shipped .harness-config.yaml trigger: { on: command, pattern: "rm -rf*" }.
    config = _cfg_with_triggers(("command", "rm -rf*"))
    mw = hitl.PauseMiddleware(config)
    req = _FakeToolCallRequest("execute", {"command": "rm -rf /project/workspace"})
    name, values, command = hitl._tool_call_fields(req)
    gated, hit = mw._should_gate(name, values, command)
    assert gated is True and hit is not None


def test_benign_command_not_gated_under_guided():
    config = _cfg_with_triggers(("command", "rm -rf*"))
    mw = hitl.PauseMiddleware(config)
    req = _FakeToolCallRequest("execute", {"command": "ls -la"})
    name, values, command = hitl._tool_call_fields(req)
    gated, _ = mw._should_gate(name, values, command)
    assert gated is False


def test_wrap_tool_call_denies_gated_call(monkeypatch, capsys):
    # A denied gate must NOT run the handler (the rm -rf must not execute).
    config = _cfg_with_triggers(("command", "rm -rf*"))
    mw = hitl.PauseMiddleware(config)
    monkeypatch.setattr(hitl.interrupt, "raise_interrupt", lambda req: False)
    monkeypatch.setattr(hitl, "_blocked_result", lambda request, name: ("BLOCKED", name))
    ran = {"handler": False}

    def handler(_req):
        ran["handler"] = True
        return "TOOL RAN"

    req = _FakeToolCallRequest("execute", {"command": "rm -rf /project/workspace"})
    out = mw.wrap_tool_call(req, handler)
    assert out == ("BLOCKED", "execute")
    assert ran["handler"] is False
    # ground-truth stderr line so a lying model can't mask the block
    err = capsys.readouterr().err
    assert "DENIED" in err and "rm -rf /project/workspace" in err


def test_wrap_tool_call_halt_on_deny_raises(monkeypatch):
    # Default on_deny=halt: a deny raises HaltTurn (turn ends) instead of returning
    # a blocked result that the ReAct loop would continue from.
    config = _cfg_with_triggers(("command", "rm -rf*"), on_deny="halt")
    mw = hitl.PauseMiddleware(config)
    monkeypatch.setattr(hitl.interrupt, "raise_interrupt", lambda req: False)
    ran = {"handler": False}

    req = _FakeToolCallRequest("execute", {"command": "rm -rf x"}, id="c7")
    with pytest.raises(hitl.HaltTurn) as ei:
        mw.wrap_tool_call(req, lambda _r: ran.__setitem__("handler", True))
    assert ran["handler"] is False
    assert ei.value.tool_name == "execute"
    assert ei.value.tool_message.tool_call_id == "c7"  # carries repair message


def test_on_deny_defaults_to_halt():
    assert cfg.parse_config("autonomy_level: guided\n").on_deny == "halt"


def test_wrap_tool_call_approves_gated_call(monkeypatch):
    config = _cfg_with_triggers(("command", "rm -rf*"))
    mw = hitl.PauseMiddleware(config)
    monkeypatch.setattr(hitl.interrupt, "raise_interrupt", lambda req: True)

    req = _FakeToolCallRequest("execute", {"command": "rm -rf /tmp/scratch"})
    out = mw.wrap_tool_call(req, lambda _req: "TOOL RAN")
    assert out == "TOOL RAN"


def test_blocked_result_is_stop_and_report():
    # The deny message must NOT invite a workaround (that drove the rmdir bypass).
    req = _FakeToolCallRequest("execute", {"command": "rm -rf x"}, id="c9")
    msg = hitl._blocked_result(req, "execute")
    text = msg.content.lower()
    assert "denied" in text
    assert "choose another approach" not in text
    assert "workaround" in text and "stop" in text  # stop-and-report intent
    assert msg.tool_call_id == "c9"


def test_approval_prompt_includes_params(monkeypatch):
    # The prompt + context must surface the tool's PARAMETERS, not just its name.
    config = _cfg_with_triggers(("tool_name", "write_file"))
    mw = hitl.PauseMiddleware(config)
    captured = {}
    monkeypatch.setattr(hitl.interrupt, "raise_interrupt",
                        lambda req: captured.update(req=req) or True)

    req = _FakeToolCallRequest("write_file", {"file_path": "a.py", "content": "print(1)"})
    mw.wrap_tool_call(req, lambda _req: "ok")
    r = captured["req"]
    assert "file_path=a.py" in r.prompt          # params on the prompt line
    assert "write_file" in r.prompt
    assert "content" in (r.context or "")        # full args in expandable context


def test_format_call_params_caps_long_values():
    long = "x" * 500
    line = hitl._format_call_params({"content": long}, None)
    assert line.startswith("content=") and "…" in line and len(line) < 300


# --- error-detail rendering (full error surfacing) ---------------------------


def test_err_detail_walks_cause_chain():
    import importlib.util
    if importlib.util.find_spec("langchain") is None:
        pytest.skip("cli import needs langchain")
    _cli = _load("harness.cli")
    try:
        raise ValueError("real 429 quota exceeded")
    except ValueError as root:
        wrapper = RuntimeError("ChatGoogleGenerativeAIError")
        wrapper.__cause__ = root
        detail = _cli._err_detail(wrapper)
    assert "ChatGoogleGenerativeAIError" in detail
    assert "429 quota exceeded" in detail          # underlying cause surfaced
    assert "<-" in detail


# --- S4: permission_denied path-guard handler (M4 slice D) -------------------


@pytest.fixture(autouse=True)
def _no_ambient_state_dir(monkeypatch):
    """The denial sink resolves through ``archive.state_dir``, which honors
    DEEPAGENTS_STATE_DIR. Clear it so these tests read the tmp_path layout and
    never write to a real run's state dir."""
    monkeypatch.delenv(archive.STATE_DIR_ENV, raising=False)


def _denials(workspace):
    return audit.read_records(
        workspace, sink=audit.denials_path(archive.state_dir(workspace))
    )


def test_path_denied_handler_always_denies(tmp_path):
    # A workspace escape is never approvable -- the handler must return a
    # non-True value regardless, so the backend re-raises PathGuardDenied.
    handler = hitl.make_path_denied_handler(tmp_path)
    result = handler("/outside/etc/passwd", "/workspace")
    assert result is not True


def test_path_denied_handler_audits_the_denial(tmp_path):
    handler = hitl.make_path_denied_handler(tmp_path)
    handler("/workspace-evil/secret", "/workspace")

    records = _denials(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["source"] == it.SOURCE_SYSTEM
    assert rec["kind"] == it.KIND_APPROVE
    assert rec["resolved_by"] == "system"
    assert rec["resolved_value"] == "False"
    assert rec["meta"]["reason"] == "workspace escape"
    assert rec["meta"]["op"] == "file"
    # path/context never leak file contents -- only the relpath is recorded
    assert "path" in rec["meta"]


def test_path_denied_record_is_marked_audit_only(tmp_path):
    # kind=approve + resolved_value=False otherwise reads, on replay, as "a human
    # was asked and declined". Nobody was asked -- this denial type has no approve
    # branch at all, and the record must say so.
    handler = hitl.make_path_denied_handler(tmp_path)
    handler("/outside/x", "/workspace")

    assert _denials(tmp_path)[0]["meta"]["audit_only"] is True


def test_path_denied_record_lands_outside_the_workspace(tmp_path):
    # The record of an escape ATTEMPT must not sit where the escaping agent's own
    # file tools can truncate it. It goes to the state dir (M4 invariant 20), not
    # <workspace>/.agent_telemetry/ (which is in-bounds for the path guard).
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    handler = hitl.make_path_denied_handler(workspace)
    handler(str(tmp_path / "evil" / "secret"), str(workspace))

    assert len(_denials(workspace)) == 1
    # nothing was written to the agent-reachable in-workspace sink
    assert audit.read_records(workspace) == []
    assert not audit.interrupts_path(workspace).exists()


def test_path_denied_handler_survives_relpath_failure(tmp_path, monkeypatch):
    # os.path.relpath raises on inputs a real denial can produce (another Windows
    # drive; an empty path on posix). The handler runs INSIDE the backend's
    # `except PathGuardDenied` block, so a second exception here would replace the
    # PermissionError the tool layer expects -- and drop the record.
    import os as _os

    def _boom(*a, **k):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(_os.path, "relpath", _boom)
    handler = hitl.make_path_denied_handler(tmp_path)

    assert handler("/outside/x", "/workspace") is not True
    records = _denials(tmp_path)
    assert len(records) == 1                       # still audited
    assert records[0]["meta"]["path"] == "/outside/x"  # degraded to the abs path


def test_path_denied_handler_never_raises_on_audit_failure(tmp_path, monkeypatch, capsys):
    # An audit write must never fail the turn -- a denial still resolves even if
    # the disk write fails. But it must not be SILENT either: this is the record
    # of a boundary violation, so a failed write is itself reported to stderr
    # (matching the other three record_interrupt call sites).
    handler = hitl.make_path_denied_handler(tmp_path)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "record_interrupt", _boom)
    assert handler("/outside/x", "/workspace") is not True

    err = capsys.readouterr().err
    assert "failed to record path denial" in err
    assert "disk full" in err
