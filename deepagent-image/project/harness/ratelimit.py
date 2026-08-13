"""Proactive request pacing from provider/plan rate limits (design_doc.md §12.4).

The reactive half (honor a 429's ``retry_delay`` / exponential backoff) lives in
``resilience.py``. This module is the *proactive* half: given a provider's plan
limits (RPM / TPM) declared in the registry (``provider.toml`` ``[limits]``), it
computes a steady request rate and hands the caller a langchain rate limiter that
paces **every** model call in the ReAct loop — so a free-tier TPM ceiling is
respected up front instead of only after a 429 death-spiral.

Split like the rest of the harness: the math + config resolution are pure stdlib
(host-testable, no langchain), and the one impure piece — building the actual
``InMemoryRateLimiter`` — is a thin lazy wrapper.

**RPM is exact** (a minimum interval between calls). **TPM is best-effort**: we
convert a tokens/min budget into a request rate using an estimated tokens-per-request
(``tokens_per_request``, overridable), then pace at whichever of RPM/TPM is
stricter. The estimate can drift from the provider's real accounting, so TPM here
reduces — not eliminates — 429s; ``resilience``'s reactive backoff catches the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

# Fallback tokens/request when TPM is set but no estimate is given. Deliberately
# on the high side: a coding agent's per-call payload (system prompt + tool
# schemas + growing history) measured ~8k fixed + history, so a low guess would
# under-pace and still trip TPM. Override via provider.toml or DEEPAGENTS_TOKENS_PER_REQUEST.
DEFAULT_TOKENS_PER_REQUEST = 10_000

# Env overrides (apply on top of the registry, read at resolve time).
ENV_RPM = "DEEPAGENTS_RPM"
ENV_TPM = "DEEPAGENTS_TPM"
ENV_TOKENS = "DEEPAGENTS_TOKENS_PER_REQUEST"
ENV_TIER = "DEEPAGENTS_PROVIDER_TIER"


@dataclass(frozen=True)
class Limits:
    """Resolved plan limits for one run. ``None`` fields = that axis is unbounded."""

    rpm: float | None = None
    tpm: float | None = None
    tokens_per_request: float = DEFAULT_TOKENS_PER_REQUEST

    def is_active(self) -> bool:
        return effective_rps(self) is not None


def effective_rps(limits: Limits) -> float | None:
    """The steady requests/second that satisfies *both* RPM and TPM, or ``None``
    when neither is set (no pacing).

    RPM → rpm/60. TPM → (tpm/60)/tokens_per_request (best-effort). The stricter
    (smaller) of the available constraints wins so neither ceiling is breached.
    """
    candidates: list[float] = []
    if limits.rpm and limits.rpm > 0:
        candidates.append(limits.rpm / 60.0)
    if limits.tpm and limits.tpm > 0 and limits.tokens_per_request > 0:
        candidates.append((limits.tpm / 60.0) / limits.tokens_per_request)
    return min(candidates) if candidates else None


def _num(value, cast=float):
    """Best-effort numeric coercion; None/blank/invalid → None."""
    if value is None or value is True or value is False:
        return None
    try:
        n = cast(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def resolve_limits(table: dict | None, env: dict | None = None) -> Limits:
    """Resolve the effective limits from a ``[limits]`` TOML table + env overrides.

    Table shape (all optional)::

        [limits]
        tier = "free"                 # active tier (else use top-level rpm/tpm)
        tokens_per_request = 12000    # TPM→rps estimate
        rpm = 30
        tpm = 15000
        [limits.free]                 # optional per-tier blocks
        rpm = 30
        tpm = 15000
        [limits.paid]
        rpm = 1000
        tpm = 4000000

    Active tier: ``DEEPAGENTS_PROVIDER_TIER`` env, else the table's ``tier`` key.
    When a matching ``[limits.<tier>]`` block exists its rpm/tpm win; otherwise the
    top-level rpm/tpm apply. ``DEEPAGENTS_RPM`` / ``DEEPAGENTS_TPM`` /
    ``DEEPAGENTS_TOKENS_PER_REQUEST`` override the final numbers (so a user can pace
    a provider that ships no ``[limits]`` at all, or tighten one that does).
    """
    import os

    env = os.environ if env is None else env
    table = table or {}

    tier = (env.get(ENV_TIER) or table.get("tier") or "").strip()
    tier_block = table.get(tier) if tier and isinstance(table.get(tier), dict) else None

    def pick(key):
        if tier_block is not None and key in tier_block:
            return tier_block.get(key)
        return table.get(key)

    rpm = _num(pick("rpm"))
    tpm = _num(pick("tpm"))
    tokens = _num(table.get("tokens_per_request")) or DEFAULT_TOKENS_PER_REQUEST

    # env overrides (highest precedence)
    rpm = _num(env.get(ENV_RPM)) if env.get(ENV_RPM) else rpm
    tpm = _num(env.get(ENV_TPM)) if env.get(ENV_TPM) else tpm
    if env.get(ENV_TOKENS):
        tokens = _num(env.get(ENV_TOKENS)) or tokens

    return Limits(rpm=rpm, tpm=tpm, tokens_per_request=tokens)


# --- pacing accounting (Milestone 6, milestone6_spec.md §5) -------------------
#
# The limiter blocks *inside* the model call, so without this the pacing wait is
# indistinguishable from model latency — and on a throttled free tier that is most
# of the run. Measuring it is what keeps `model_ms` a property of the model rather
# than of the plan (invariant 4b).
#
# MODULE-LEVEL, not an instance attribute, and that is forced rather than
# stylistic: the limiter is built deep inside `providers.resolve_chat_model` and
# handed straight to the chat model as `rate_limiter=`, so **nothing gives cli.py a
# reference to the object**. A per-instance counter would be unreachable from the
# middleware that has to read it. One limiter exists per process (M1's design), so
# the two carry identical information here; only one of them is addressable.
#
# Nanoseconds internally, milliseconds at the seam, so repeated small acquire()
# waits don't each floor to zero. Monotonically increasing — callers take a delta
# across a turn boundary rather than an absolute.
_TOTAL_BLOCKED_NS = 0


def blocked_ms() -> int:
    """Total milliseconds spent blocked in ``acquire()`` since process start."""
    return _TOTAL_BLOCKED_NS // 1_000_000


def reset_blocked() -> None:
    """Zero the counter. For tests only — it is process-global, so a case that
    asserts on it must reset rather than poke the private name."""
    global _TOTAL_BLOCKED_NS
    _TOTAL_BLOCKED_NS = 0


def _add_blocked_ns(elapsed_ns: int) -> None:
    global _TOTAL_BLOCKED_NS
    _TOTAL_BLOCKED_NS += max(int(elapsed_ns), 0)


def build_rate_limiter(rps: float):
    """A langchain ``InMemoryRateLimiter`` pacing at ``rps`` requests/second.

    ``max_bucket_size=1`` disables bursting (a hard steady cap, not a token bucket
    that lets N calls through at once then stalls). Lazy import so this module stays
    host-importable for the pure-math tests.

    The returned limiter is a subclass that also accounts for the time it spends
    blocked (see the module counter above). Behaviour is otherwise the base class's,
    unchanged: instrumenting must not turn the soft "limiter could not be built,
    carry on unpaced" degradation in ``providers.resolve_chat_model`` into a hard
    failure, so nothing here may raise that the base would not.
    """
    import time

    from langchain_core.rate_limiters import InMemoryRateLimiter

    class _InstrumentedLimiter(InMemoryRateLimiter):
        """Same limiter, plus accounting of time spent blocked in acquire().

        Defined inside the factory because the base class is a lazy import — the
        module must stay host-importable for the pure-math tests, which is also why
        `harness doctor` and the resolver can load it with no langchain present.
        """

        def acquire(self, *args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return super().acquire(*args, **kwargs)
            finally:
                _add_blocked_ns(time.perf_counter_ns() - start)

        async def aacquire(self, *args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return await super().aacquire(*args, **kwargs)
            finally:
                _add_blocked_ns(time.perf_counter_ns() - start)

    # Check often enough to be responsive at high rps, but not busy-spin at low rps.
    check = min(0.1, max(rps / 10.0, 0.001)) if rps > 0 else 0.1
    return _InstrumentedLimiter(
        requests_per_second=rps,
        check_every_n_seconds=check,
        max_bucket_size=1,
    )
