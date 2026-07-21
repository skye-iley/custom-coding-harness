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
