"""Tests for harness/ratelimit.py — pure limit math + tier/env resolution.

No langchain needed: everything here is stdlib. build_rate_limiter (the one impure
piece) is exercised by smoke, not asserted here.
"""

from __future__ import annotations

from _bootstrap import _load

rl = _load("harness.ratelimit")


# --- effective_rps -----------------------------------------------------------

def test_rps_none_when_unset():
    assert rl.effective_rps(rl.Limits()) is None


def test_rps_from_rpm_exact():
    assert rl.effective_rps(rl.Limits(rpm=60)) == 1.0
    assert rl.effective_rps(rl.Limits(rpm=30)) == 0.5


def test_rps_from_tpm_best_effort():
    # 15000 tpm / 60 = 250 tok/s; /10000 tok per req = 0.025 req/s
    got = rl.effective_rps(rl.Limits(tpm=15000, tokens_per_request=10000))
    assert abs(got - 0.025) < 1e-9


def test_rps_takes_stricter_of_rpm_and_tpm():
    # rpm=30 -> 0.5 rps; tpm=15000/12000 -> 0.0208 rps. TPM is stricter.
    lim = rl.Limits(rpm=30, tpm=15000, tokens_per_request=12000)
    assert rl.effective_rps(lim) < 0.5
    # a fat token budget makes RPM the binding constraint instead
    lim2 = rl.Limits(rpm=30, tpm=10_000_000, tokens_per_request=12000)
    assert rl.effective_rps(lim2) == 0.5


# --- resolve_limits: tier + env ----------------------------------------------

TABLE = {
    "tokens_per_request": 12000,
    "free": {"rpm": 30, "tpm": 15000},
    "tier1": {"rpm": 1000, "tpm": 1000000},
}


def test_no_tier_no_toplevel_is_inert():
    lim = rl.resolve_limits(TABLE, env={})
    assert lim.rpm is None and lim.tpm is None
    assert not lim.is_active()


def test_tier_from_env_selects_block():
    lim = rl.resolve_limits(TABLE, env={"DEEPAGENTS_PROVIDER_TIER": "free"})
    assert lim.rpm == 30 and lim.tpm == 15000
    assert lim.tokens_per_request == 12000
    assert lim.is_active()


def test_tier_from_table_key():
    lim = rl.resolve_limits({**TABLE, "tier": "tier1"}, env={})
    assert lim.rpm == 1000 and lim.tpm == 1000000


def test_env_overrides_win():
    lim = rl.resolve_limits(
        TABLE,
        env={"DEEPAGENTS_PROVIDER_TIER": "free", "DEEPAGENTS_RPM": "5"},
    )
    assert lim.rpm == 5.0 and lim.tpm == 15000  # tpm still from tier


def test_env_only_paces_provider_without_limits():
    # A provider that ships no [limits] can still be paced purely via env.
    lim = rl.resolve_limits(None, env={"DEEPAGENTS_TPM": "6000", "DEEPAGENTS_TOKENS_PER_REQUEST": "3000"})
    assert lim.tpm == 6000 and lim.tokens_per_request == 3000
    assert rl.effective_rps(lim) == (6000 / 60) / 3000


def test_zero_and_garbage_are_ignored():
    lim = rl.resolve_limits({"free": {"rpm": 0, "tpm": "nope"}}, env={"DEEPAGENTS_PROVIDER_TIER": "free"})
    assert lim.rpm is None and lim.tpm is None


# --- Milestone 6 §5: pacing accounting ----------------------------------------
#
# The limiter blocks INSIDE the model call, so without this the pacing wait is
# indistinguishable from model latency — and on a throttled free tier that is
# most of the run (invariant 4b).

import pytest  # noqa: E402


def test_counter_starts_at_zero_after_reset():
    rl.reset_blocked()
    assert rl.blocked_ms() == 0


def test_counter_accumulates_nanoseconds_and_reports_milliseconds():
    rl.reset_blocked()
    rl._add_blocked_ns(1_500_000)   # 1.5ms
    rl._add_blocked_ns(1_500_000)   # 1.5ms
    # Stored in ns and floored only at the seam, so two sub-millisecond waits do
    # not each round to zero -- which is how a paced run would report 0ms.
    assert rl.blocked_ms() == 3
    rl.reset_blocked()


def test_negative_deltas_are_ignored():
    rl.reset_blocked()
    rl._add_blocked_ns(-5_000_000)
    assert rl.blocked_ms() == 0


def test_counter_is_monotonic_so_callers_take_a_delta():
    rl.reset_blocked()
    rl._add_blocked_ns(2_000_000)
    first = rl.blocked_ms()
    rl._add_blocked_ns(3_000_000)
    assert rl.blocked_ms() > first
    rl.reset_blocked()


def test_instrumented_limiter_times_acquire():
    """The counter has to be fed by the real acquire() path, not only by the
    helper above. Needs langchain, so it is image-tier."""
    pytest.importorskip("langchain_core.rate_limiters")
    rl.reset_blocked()
    limiter = rl.build_rate_limiter(1000.0)  # fast: this must not slow the suite
    limiter.acquire()
    limiter.acquire()
    # Monotonic and non-negative is all a wall-clock assertion can honestly claim
    # at this rate; that the seam is wired at all is the property under test.
    assert rl.blocked_ms() >= 0
    assert type(limiter).__name__ == "_InstrumentedLimiter"
    rl.reset_blocked()


def test_limiter_is_still_an_inmemoryratelimiter():
    """Instrumenting must not change what providers.resolve_chat_model hands the
    chat model as `rate_limiter=`, or the soft "could not build a limiter, carry
    on unpaced" degradation becomes a hard failure."""
    pytest.importorskip("langchain_core.rate_limiters")
    from langchain_core.rate_limiters import InMemoryRateLimiter

    limiter = rl.build_rate_limiter(10.0)
    assert isinstance(limiter, InMemoryRateLimiter)
    assert limiter.requests_per_second == 10.0
    assert limiter.max_bucket_size == 1
