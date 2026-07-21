"""Interrupt audit trail — Milestone 3 slice S7 (a sliver of design_doc.md §12.7).

Every interrupt and its human response is appended, secret-scrubbed, to
``<workspace>/.agent_telemetry/interrupts.jsonl`` for reproducibility/replay. That
directory is git-ignored and excluded by the git-pr workflow, so audit records
never reach a PR.

Records carry only the stable fields the spec names — interrupt ``id``, ``kind``,
``prompt``, resolved value, ``source``, timestamps — and **not** the ``context``
payload (a diff/command that could be large or secret-laden); dropping it is the
cheap structural guard against leaking it here. Every string is still scrubbed
(§10) as a backstop.

Off unless HITL is active (the caller only records when an interrupt actually
fired), preserving the removable-seam / byte-for-byte-MVP contract.

Stdlib only; imports ``harness.interrupt`` for the request type but nothing heavy,
so it stays host-testable.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from harness.interrupt import InterruptRequest

# Lives under the workspace like the rest of the agent telemetry, but in its own
# dir the git-pr workflow already excludes (deepagent-image/CLAUDE.md).
TELEMETRY_DIRNAME = ".agent_telemetry"
INTERRUPTS_FILE = "interrupts.jsonl"

# Env-key name fragments that mark a value as a credential worth redacting.
_SECRET_KEY_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "_KEY")
# Backstop pattern for common bearer/api-key shapes even if not in the env.
_SECRET_PATTERN = re.compile(r"\b(sk|pk|ghp|gho|xoxb|xoxp)-[A-Za-z0-9_\-]{12,}\b")
_REDACTED = "***REDACTED***"


def interrupts_path(workspace: Path) -> Path:
    """Path to the interrupts audit log for `workspace`."""
    return Path(workspace) / TELEMETRY_DIRNAME / INTERRUPTS_FILE


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_interrupt(
    workspace: Path,
    request: InterruptRequest,
    resolved_value: object,
    *,
    resolved_by: str = "human",
    raised_at: str | None = None,
    resolved_at: str | None = None,
    env: dict | None = None,
) -> dict:
    """Append one scrubbed audit record for `request` + its resolution. Returns
    the record dict (also useful for tests). Best-effort on I/O errors is the
    caller's job — this raises so a wiring bug is visible in tests, but the cli
    wrapper swallows it (an audit write must never fail a turn)."""
    path = interrupts_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": request.id,
        "kind": request.kind,
        "source": request.source,
        "prompt": scrub(request.prompt, env),
        "options": [scrub(o, env) for o in request.options],
        "resolved_value": scrub(str(resolved_value), env) if resolved_value is not None else None,
        "resolved_by": resolved_by,
        "raised_at": raised_at or _now(),
        "resolved_at": resolved_at or _now(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_records(workspace: Path) -> list[dict]:
    """Parse the audit log back into records (replay/inspection). Empty when the
    log does not exist yet."""
    path = interrupts_path(workspace)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
