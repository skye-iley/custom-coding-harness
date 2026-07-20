"""Provider resilience — retry/backoff + context-overflow classification.

Promotes design_doc.md §12.4, pulled into Milestone 3 as prerequisite **slice P1**
because the source-3 *provider-error* interrupt (S4) is defined to fire only
*after* this backoff layer is exhausted — without a backoff to escalate from,
there is nothing to escalate.

Two narrow, safe behaviours wrap the per-turn model invoke (`cli.run_turn`):

  * **Transient-error retry** — bounded exponential backoff with full jitter for
    retryable statuses (429 / 5xx / connection reset). Caps come from env
    (``DEEPAGENTS_MAX_RETRIES``, ``DEEPAGENTS_RETRY_BASE``).
  * **Context-overflow stopgap** — classify the context-length error so the caller
    can trim the oldest turns and retry once. Explicitly the pre-§7 (Headroom)
    placeholder; ``trim_messages`` is a blunt drop-oldest, not summarization.

Pure stdlib, no langchain import, so the classification + backoff math are
host-testable on a bare interpreter (suite convention). The impure pieces (real
``time.sleep``, the actual model call) are injected by the caller so the retry
loop itself is deterministic under test.

S4 depends on this: the provider-error interrupt is raised from the *same* except
clauses, once ``retry_call`` has re-raised the exhausted error.
"""

from __future__ import annotations

import os
import random
import re
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# HTTP statuses worth retrying: rate-limit + the transient 5xx family. A 4xx
# other than 429 is a caller error (bad key, bad request) — retrying just burns
# the same failure, so it is NOT retryable.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Substrings that mark a transient transport failure when no numeric status is
# exposed on the exception (connection resets, read timeouts, temporary DNS).
_RETRYABLE_SUBSTRINGS = (
    "connection reset",
    "connection aborted",
    "connection error",
    "read timed out",
    "read timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "overloaded",
    "rate limit",
    "too many requests",
    "econnreset",
)

# Substrings that mark a context-length / token-window overflow. Providers word
# this differently, so match on the stable fragments each uses.
_CONTEXT_OVERFLOW_SUBSTRINGS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "context window",
    "too many tokens",
    "reduce the length",
    "input is too long",
    "prompt is too long",
    "string too long",
    "exceeds the maximum",
)

# Defaults when the env knobs are unset/invalid. Three tries total (one initial
# + retries) with a 0.5s base doubles to ~0.5/1/2s of backoff before the jitter.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BASE = 0.5
# Hard ceiling on any single backoff sleep, so a large attempt count or base
# can't wedge the session for minutes.
_DELAY_CAP_SECONDS = 20.0


def _status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status pulled off an exception, across the attribute
    names the common SDK/transport layers use. ``None`` when none is exposed."""
    for attr in ("status_code", "http_status", "status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, bool):  # bool is an int subclass; never a status
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    # Some SDKs nest the status on a `.response` object.
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


def _first_status_in_text(text: str) -> int | None:
    """A 3-digit status embedded in an error string (``Error code: 503``)."""
    m = re.search(r"\b([45]\d{2})\b", text)
    return int(m.group(1)) if m else None


def is_retryable(exc: BaseException) -> bool:
    """True when ``exc`` is a transient provider/transport failure worth retrying.

    Retryable = an explicit retryable status (429/5xx), or — absent a status —
    a transport-failure fingerprint in the message. A context-overflow error is
    deliberately NOT retryable here: it needs trimming, not a blind resend, and
    is routed through ``is_context_overflow`` instead.
    """
    if is_context_overflow(exc):
        return False
    status = _status_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS
    text = str(exc).lower()
    if any(sub in text for sub in _RETRYABLE_SUBSTRINGS):
        return True
    embedded = _first_status_in_text(text)
    return embedded in RETRYABLE_STATUS if embedded is not None else False


def is_context_overflow(exc: BaseException) -> bool:
    """True when ``exc`` signals the prompt exceeded the model's context window."""
    text = str(exc).lower()
    return any(sub in text for sub in _CONTEXT_OVERFLOW_SUBSTRINGS)


def max_retries_from_env(env: dict | None = None) -> int:
    """Retry budget from ``DEEPAGENTS_MAX_RETRIES`` (default 3, clamped >= 0)."""
    env = os.environ if env is None else env
    raw = env.get("DEEPAGENTS_MAX_RETRIES")
    if not raw:
        return _DEFAULT_MAX_RETRIES
    try:
        return max(int(raw), 0)
    except ValueError:
        return _DEFAULT_MAX_RETRIES


def retry_base_from_env(env: dict | None = None) -> float:
    """Backoff base seconds from ``DEEPAGENTS_RETRY_BASE`` (default 0.5, > 0)."""
    env = os.environ if env is None else env
    raw = env.get("DEEPAGENTS_RETRY_BASE")
    if not raw:
        return _DEFAULT_RETRY_BASE
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_RETRY_BASE
    return val if val > 0 else _DEFAULT_RETRY_BASE


def compute_delay(attempt: int, base: float, rand: Callable[[], float] = random.random) -> float:
    """Full-jitter exponential backoff for a 0-indexed ``attempt``.

    Delay is uniform in ``[0, min(cap, base * 2**attempt)]`` (AWS "full jitter"),
    so concurrent retriers don't resynchronize into a thundering herd. ``rand``
    is injectable (0..1) so tests pin the delay deterministically.
    """
    ceiling = min(base * (2 ** max(attempt, 0)), _DELAY_CAP_SECONDS)
    return rand() * ceiling


def retry_call(
    fn: Callable[[], T],
    *,
    max_retries: int,
    base: float,
    sleep: Callable[[float], None],
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``fn`` with bounded exponential backoff on retryable failures.

    Up to ``max_retries`` extra attempts after the first (so ``max_retries + 1``
    calls at most). A non-retryable exception (bad key, context overflow, a bug)
    propagates immediately — retrying it would only repeat the same failure. When
    the budget is exhausted the *last* retryable exception is re-raised, which is
    where S4's provider-error interrupt hooks in.

    ``sleep`` and ``rand`` are injected so the loop is deterministic and instant
    under test; ``on_retry(attempt, exc, delay)`` is an optional observer for a
    ``[harness] retrying...`` stage marker.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classification decides re-raise
            if attempt >= max_retries or not is_retryable(exc):
                raise
            delay = compute_delay(attempt, base, rand=rand)
            if on_retry is not None:
                on_retry(attempt + 1, exc, delay)
            sleep(delay)
            attempt += 1


def trim_messages(messages: list[Any], keep_last: int) -> list[Any]:
    """Drop the oldest messages, keeping at most the last ``keep_last``.

    The blunt pre-Headroom (§7) stopgap for a context overflow: on overflow the
    caller retries once with a trimmed message list. This is drop-oldest, not
    summarization — a placeholder that trades old context for a turn that fits,
    flagged as such in the milestone spec (P1). ``keep_last <= 0`` keeps none.
    """
    if keep_last <= 0:
        return []
    return messages[-keep_last:]
