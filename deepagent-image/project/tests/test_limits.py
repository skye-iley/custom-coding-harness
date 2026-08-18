"""Tests for harness/limits.py — Milestone 8 B1 (the three hard stops).

Host tier: stdlib only, no langchain, no model, no network. The clock is
injected, so nothing here sleeps. Everything that needs the agent runtime (the
middleware seam, the `stopped` outcome reaching a real telemetry record, the
graph config key) lives in ``test_cli.py``.

The property this file exists to protect is the one `milestone8.md` §10 states
and M7 invariant 18 paid for: **an unset bound is absent, not infinite.** A test
that sets a very large number and watches it not fire proves nothing — the
assertions below are structural (``None`` means no comparison happens at all).
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

limits = _load("harness.limits")


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Deadline -----------------------------------------------------------------


def test_deadline_not_yet_reached_does_not_raise():
    clock = FakeClock()
    d = limits.Deadline(10.0, clock=clock)
    clock.advance(9.999)
    assert d.expired() is False
    d.check()  # must not raise
    assert d.remaining() == pytest.approx(0.001)


def test_deadline_just_crossed_raises_with_both_numbers():
    clock = FakeClock()
    d = limits.Deadline(10.0, clock=clock)
    clock.advance(10.0)  # exactly at the bound: >= , so it fires
    assert d.expired() is True
    with pytest.raises(limits.DeadlineExceeded) as exc:
        d.check()
    # The message has to carry the bound AND the elapsed time: "a deadline was
    # exceeded" without either number tells an operator nothing about whether to
    # raise the bound or fix the loop.
    assert exc.value.limit_seconds == 10.0
    assert exc.value.elapsed_seconds == pytest.approx(10.0)
    assert "10" in str(exc.value)


def test_deadline_carries_its_stop_reason():
    with pytest.raises(limits.DeadlineExceeded) as exc:
        limits.Deadline(0.0, clock=FakeClock()).check()
    assert exc.value.stop_reason == limits.STOP_SECONDS


def test_deadline_with_no_limit_never_expires_however_long_it_runs():
    # The pass-through. `None` is not "a very large number" -- `expired()` short
    # circuits before any arithmetic, and `remaining()` reports None rather than
    # a figure a caller could accidentally compare against.
    clock = FakeClock()
    d = limits.Deadline(None, clock=clock)
    clock.advance(10_000_000)
    assert d.expired() is False
    assert d.remaining() is None
    d.check()


def test_deadline_remaining_goes_negative_rather_than_clamping():
    # An operator reading a blown bound wants to know BY HOW MUCH; clamping at
    # zero would throw that away.
    clock = FakeClock()
    d = limits.Deadline(5.0, clock=clock)
    clock.advance(8.0)
    assert d.remaining() == pytest.approx(-3.0)


# --- TurnCounter --------------------------------------------------------------


def test_turn_counter_allows_exactly_max_turns_then_refuses():
    # The off-by-one, asserted at the boundary: `--max-turns 2` must let the 2nd
    # turn RUN and refuse the 3rd. Off by one in either direction is a silent
    # behaviour change nobody would notice on a long sweep.
    c = limits.TurnCounter(2)
    assert c.begin() == 1
    assert c.begin() == 2
    assert c.exhausted() is True
    with pytest.raises(limits.TurnLimitExceeded) as exc:
        c.begin()
    assert exc.value.limit_turns == 2
    assert exc.value.stop_reason == limits.STOP_TURNS
    # And the refused turn is NOT counted: the count is turns that ran.
    assert c.count == 2


def test_turn_counter_with_no_limit_never_refuses():
    c = limits.TurnCounter(None)
    for _ in range(50):
        c.begin()
    assert c.exhausted() is False
    c.check()
    assert c.count == 50


def test_turn_counter_limit_of_one_refuses_the_second_turn():
    c = limits.TurnCounter(1)
    c.begin()
    with pytest.raises(limits.TurnLimitExceeded):
        c.begin()


def test_turn_counter_limit_of_zero_refuses_immediately():
    # Degenerate but reachable (`--max-turns 0`), and it must refuse rather than
    # run one turn "because zero looks unset". None is the unset spelling.
    c = limits.TurnCounter(0)
    with pytest.raises(limits.TurnLimitExceeded):
        c.begin()


def test_raising_a_live_limit_lets_the_session_continue():
    # What `/config set max_turns` does: the counter is mutated in place, and the
    # turns already taken still count against the new bound.
    c = limits.TurnCounter(1)
    c.begin()
    assert c.exhausted() is True
    c.limit = 3
    assert c.exhausted() is False
    assert c.begin() == 2


# --- the classifier -----------------------------------------------------------


class FakeGraphRecursionError(RuntimeError):
    """Stands in for langgraph's exception, which this tier cannot import."""


FakeGraphRecursionError.__name__ = "GraphRecursionError"


def test_stop_reason_for_maps_each_bound_to_its_own_reason():
    assert limits.stop_reason_for(limits.DeadlineExceeded(1.0, 2.0)) == limits.STOP_SECONDS
    assert limits.stop_reason_for(limits.TurnLimitExceeded(3)) == limits.STOP_TURNS
    assert limits.stop_reason_for(FakeGraphRecursionError("boom")) == limits.STOP_STEPS


def test_stop_reason_for_returns_none_on_a_real_failure():
    # The whole point of the split: a provider 500 is an `error`, not a `stopped`.
    # If this ever returns a reason, a crashed instance starts reading as one that
    # merely ran out of rope -- `milestone8.md` §3's defect, inverted.
    for exc in (RuntimeError("provider 500"), ValueError("bad json"), KeyboardInterrupt()):
        assert limits.stop_reason_for(exc) is None
        assert limits.is_stop(exc) is False


def test_is_stop_is_true_for_every_bound():
    for exc in (
        limits.DeadlineExceeded(1.0, 2.0),
        limits.TurnLimitExceeded(3),
        FakeGraphRecursionError("boom"),
    ):
        assert limits.is_stop(exc) is True


def test_stop_reason_for_ignores_a_bogus_stop_reason_attribute():
    # `stop_reason` is read off the exception, so an unrelated object carrying an
    # attribute of that name must not be able to forge a stop.
    class Impostor(Exception):
        stop_reason = "vibes"

    assert limits.stop_reason_for(Impostor()) is None


def test_every_declared_stop_reason_is_produced_by_something():
    # Guards the enum against a value nothing can emit -- a `stop_reason` a sweep
    # could filter on and never match is worse than one that does not exist.
    produced = {
        limits.stop_reason_for(limits.DeadlineExceeded(1.0, 2.0)),
        limits.stop_reason_for(limits.TurnLimitExceeded(1)),
        limits.stop_reason_for(FakeGraphRecursionError("x")),
    }
    assert produced == set(limits.STOP_REASONS)


def test_exit_stopped_is_distinct_from_success_and_from_failure():
    # A driver reading only the process status must be able to tell "did not
    # converge" from "crashed" (exit 1) and from "finished" (exit 0).
    assert limits.EXIT_STOPPED not in (0, 1, 2)
