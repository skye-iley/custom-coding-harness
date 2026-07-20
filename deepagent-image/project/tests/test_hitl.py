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
