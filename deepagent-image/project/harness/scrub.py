"""Secret redaction — one implementation, shared by every sink that writes to disk.

Moved **verbatim** out of ``audit.py`` (Milestone 6 T1, ``milestone6_spec.md`` §1)
so ``telemetry.py`` can reuse it without inheriting ``audit``'s import of
``harness.interrupt`` — the M3 request model has nothing to do with a usage
record, and dragging it in would make the telemetry module's dependency profile
larger than the invariant (21) allows.

The names are deliberately unchanged. ``test_audit.py``'s scrub cases are the
oracle for this move and must keep passing **unedited**, so ``audit`` re-exports
``scrub`` / ``scrub_deep`` and every call site continues to resolve.

Stdlib only. This is a leaf module: it imports nothing from ``harness``.
"""

from __future__ import annotations

import os
import re

# Env-key name fragments that mark a value as a credential worth redacting.
_SECRET_KEY_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "_KEY")
# Backstop pattern for common bearer/api-key shapes even if not in the env.
_SECRET_PATTERN = re.compile(r"\b(sk|pk|ghp|gho|xoxb|xoxp)-[A-Za-z0-9_\-]{12,}\b")
_REDACTED = "***REDACTED***"


def _secret_values(env: dict) -> list[str]:
    """Values of credential-looking env vars, longest first so a longer secret is
    redacted before a shorter one it may contain."""
    vals = [
        v
        for k, v in env.items()
        if v and len(v) >= 8 and any(m in k.upper() for m in _SECRET_KEY_MARKERS)
    ]
    return sorted(set(vals), key=len, reverse=True)


def scrub(text: str | None, env: dict | None = None) -> str | None:
    """Redact credential env values and common key shapes from `text` (§10).

    Redacts any live secret env *value* found verbatim in the string (the strong
    check — it catches a leaked key regardless of surrounding text), then a
    pattern backstop for key shapes not sourced from the current env."""
    if not text:
        return text
    env = os.environ if env is None else env
    for val in _secret_values(env):
        text = text.replace(val, _REDACTED)
    return _SECRET_PATTERN.sub(_REDACTED, text)


def scrub_deep(value, env: dict | None = None):
    """`scrub` applied through nested containers, leaving non-strings untouched.

    ``meta`` is a free-form dict (``interrupt.InterruptRequest``), so a producer
    can put a dict/list in it. A top-level-strings-only scrub would make that a
    silent leak path around the §10 backstop — the very thing dropping ``context``
    exists to prevent. Recurse instead."""
    if isinstance(value, str):
        return scrub(value, env)
    if isinstance(value, dict):
        return {k: scrub_deep(v, env) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_deep(v, env) for v in value]
    return value
