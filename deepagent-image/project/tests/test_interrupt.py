"""Tests for harness/interrupt.py (Milestone 3 slice S1, the interrupt spine core).

Pure stdlib module — no langgraph — so the request model, keying, serialization,
render/expand, reply interpretation, and headless fail-closed resolution are all
host-testable. The graph-side ``raise_interrupt`` (lazy langgraph import) is not
exercised here.
"""

from __future__ import annotations

import json

import pytest

from _bootstrap import _load

it = _load("harness.interrupt")


# --- construction + validation -----------------------------------------------

def test_new_request_has_stable_uuid():
    r = it.new_request(it.KIND_INPUT, "what next?")
    assert r.id and isinstance(r.id, str)
    # Two requests get distinct ids (positional-resume bug avoidance, §6).
    assert r.id != it.new_request(it.KIND_INPUT, "what next?").id


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        it.new_request("banana", "nope")


def test_choose_requires_options():
    with pytest.raises(ValueError):
        it.new_request(it.KIND_CHOOSE, "pick", options=())
    # with options it is fine
    it.new_request(it.KIND_CHOOSE, "pick", options=("a", "b"))


# --- serialization round-trip (the checkpoint-restart bar) -------------------

def test_to_from_dict_roundtrip():
    r = it.new_request(
        it.KIND_CHOOSE,
        "which provider?",
        options=("anthropic", "openai"),
        context="line1\nline2",
        default="anthropic",
        timeout_policy=it.TIMEOUT_ABORT,
        source=it.SOURCE_SYSTEM,
        meta={"tool_call_id": "abc"},
    )
    d = r.to_dict()
    # Must be JSON-serializable (it is the value persisted to checkpoints.sqlite).
    d2 = json.loads(json.dumps(d))
    back = it.InterruptRequest.from_dict(d2)
    assert back == r
    assert back.id == r.id  # same keyed prompt re-surfaces after a restart


def test_meta_defaults_isolated():
    a = it.new_request(it.KIND_INPUT, "a")
    b = it.new_request(it.KIND_INPUT, "b")
    a.meta["x"] = 1
    assert b.meta == {}  # no shared mutable default


# --- render (cap + expand, §6) -----------------------------------------------

def test_render_includes_prompt_and_options():
    r = it.new_request(it.KIND_CHOOSE, "pick one", options=("alpha", "beta"))
    out = it.render(r)
    assert "pick one" in out
    assert "1) alpha" in out
    assert "2) beta" in out


def test_render_caps_long_context_with_footer():
    ctx = "\n".join(f"line{i}" for i in range(50))
    r = it.new_request(it.KIND_APPROVE, "ok?", context=ctx)
    out = it.render(r, max_context_lines=10)
    assert "line0" in out
    assert "line9" in out
    assert "line10" not in out.split("/show")[0].replace("+40", "")  # line10 hidden
    assert "+40 lines" in out
    assert "/show to expand" in out


def test_render_short_context_not_truncated():
    r = it.new_request(it.KIND_APPROVE, "ok?", context="a\nb\nc")
    out = it.render(r, max_context_lines=10)
    assert "/show" not in out
    assert "a" in out and "c" in out


def test_expand_returns_full_context():
    ctx = "\n".join(f"line{i}" for i in range(50))
    r = it.new_request(it.KIND_APPROVE, "ok?", context=ctx)
    assert it.expand(r) == ctx


def test_render_shows_default():
    r = it.new_request(it.KIND_INPUT, "name?", default="worker")
    assert "worker" in it.render(r)


# --- reply interpretation ----------------------------------------------------

@pytest.mark.parametrize("reply", ["y", "Yes", "approve", "ok"])
def test_approve_affirmative(reply):
    r = it.new_request(it.KIND_APPROVE, "ok?")
    assert it.interpret_reply(r, reply) is True


@pytest.mark.parametrize("reply", ["n", "No", "deny", "block"])
def test_approve_negative(reply):
    r = it.new_request(it.KIND_APPROVE, "ok?")
    assert it.interpret_reply(r, reply) is False


def test_approve_empty_uses_default():
    r = it.new_request(it.KIND_APPROVE, "ok?", default=True)
    assert it.interpret_reply(r, "") is True


def test_approve_empty_no_default_raises():
    r = it.new_request(it.KIND_APPROVE, "ok?")
    with pytest.raises(it.ReplyError):
        it.interpret_reply(r, "")


def test_approve_garbage_raises():
    r = it.new_request(it.KIND_APPROVE, "ok?")
    with pytest.raises(it.ReplyError):
        it.interpret_reply(r, "maybe")


def test_choose_by_index_and_name():
    r = it.new_request(it.KIND_CHOOSE, "pick", options=("alpha", "beta", "gamma"))
    assert it.interpret_reply(r, "2") == "beta"
    assert it.interpret_reply(r, "GAMMA") == "gamma"


def test_choose_out_of_range_raises():
    r = it.new_request(it.KIND_CHOOSE, "pick", options=("a", "b"))
    with pytest.raises(it.ReplyError):
        it.interpret_reply(r, "9")


def test_input_returns_raw_text():
    r = it.new_request(it.KIND_INPUT, "path?")
    assert it.interpret_reply(r, "  /tmp/x  ") == "  /tmp/x  "


def test_resolve_returns_edited_value():
    r = it.new_request(it.KIND_RESOLVE, "edit the command", default="rm x")
    assert it.interpret_reply(r, "rm -i x") == "rm -i x"
    assert it.interpret_reply(r, "") == "rm x"  # empty falls back to default


# --- headless fail-closed resolution (§6) ------------------------------------

def test_headless_falls_through_to_default():
    r = it.new_request(it.KIND_CHOOSE, "pick", options=("a", "b"), default="a")
    d = it.headless_decision(r, autonomy_level="strict", interruption_policy="blocking")
    assert not d.abort and d.value == "a"


def test_headless_timeout_abort_policy():
    r = it.new_request(it.KIND_INPUT, "x", default="y", timeout_policy=it.TIMEOUT_ABORT)
    d = it.headless_decision(r)
    assert d.abort


def test_headless_approve_no_default_denies_in_guided():
    r = it.new_request(it.KIND_APPROVE, "run rm -rf?")
    d = it.headless_decision(r, autonomy_level="guided", interruption_policy="blocking")
    assert not d.abort and d.value is False


def test_headless_strict_blocking_no_default_aborts():
    r = it.new_request(it.KIND_APPROVE, "run rm -rf?")
    d = it.headless_decision(r, autonomy_level="strict", interruption_policy="blocking")
    assert d.abort


def test_headless_input_no_default_aborts():
    r = it.new_request(it.KIND_INPUT, "what path?")
    d = it.headless_decision(r, autonomy_level="guided", interruption_policy="blocking")
    assert d.abort


def test_exit_code_constant_distinct():
    assert it.EXIT_INTERRUPT_ABORT not in (0, 1, 2)
