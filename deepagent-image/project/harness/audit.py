"""Interrupt audit trail — Milestone 3 slice S7 (a sliver of design_doc.md §12.7).

Every interrupt and its human response is appended, secret-scrubbed, to
``<workspace>/.agent_telemetry/interrupts.jsonl`` for reproducibility/replay. That
directory is git-ignored and excluded by the git-pr workflow, so audit records
never reach a PR.

Records carry only the stable fields the spec names — interrupt ``id``, ``kind``,
``prompt``, resolved value, ``source``, ``meta``, timestamps — and **not** the
``context`` payload (a diff/command that could be large or secret-laden); dropping
it is the cheap structural guard against leaking it here. Every string is still
scrubbed (§10) as a backstop, **recursively** through ``meta`` (which is a free
dict, so a nested value must not be a scrub blind spot).

**Two sinks, deliberately split (M4 slice D).** The default sink above is
*in-workspace*, so the agent's own file/shell tools can rewrite it — fine for HITL
UX records, not fine for a record of the agent trying to escape the workspace.
Boundary denials therefore go to ``denials_path(state_dir)`` —
``<state-dir>/denials.jsonl``, outside the workspace mount (``archive.state_dir``,
the same isolation M2 gave ``past.sqlite``). The caller picks the sink; this module
just writes where it is told. Note the state dir defeats the *file*-tool tamper
path, not the shell one — a container shell can still reach it by absolute path
until the bwrap jail (M4 slice H) lands.

Off unless HITL is active (the caller only records when an interrupt actually
fired), preserving the removable-seam / byte-for-byte-MVP contract.

Stdlib only; imports ``harness.interrupt`` for the request type but nothing heavy,
so it stays host-testable.

The secret scrub itself now lives in ``harness.scrub`` (Milestone 6 T1) so
``telemetry.py`` can share the one implementation without pulling
``harness.interrupt`` in behind it. It is re-exported here under its original
names, so ``audit.scrub(...)`` / ``audit.scrub_deep(...)`` still resolve and the
existing scrub tests in ``test_audit.py`` pass unedited — which is what makes
them the oracle for the move.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harness.interrupt import InterruptRequest
from harness.scrub import (  # noqa: F401 - re-exported for existing call sites
    _REDACTED,
    _SECRET_KEY_MARKERS,
    _SECRET_PATTERN,
    _secret_values,
    scrub,
    scrub_deep,
)

# Lives under the workspace like the rest of the agent telemetry, but in its own
# dir the git-pr workflow already excludes (deepagent-image/CLAUDE.md).
TELEMETRY_DIRNAME = ".agent_telemetry"
INTERRUPTS_FILE = "interrupts.jsonl"
# Boundary-denial sink. Lives in the *state dir*, not the workspace — see the
# module docstring ("two sinks").
DENIALS_FILE = "denials.jsonl"


def interrupts_path(workspace: Path) -> Path:
    """Path to the interrupts audit log for `workspace` (in-workspace sink)."""
    return Path(workspace) / TELEMETRY_DIRNAME / INTERRUPTS_FILE


def denials_path(state_dir: Path) -> Path:
    """Path to the boundary-denial log under `state_dir` (agent-unreachable sink).

    Takes the resolved state dir rather than the workspace so this module keeps
    its zero-sibling-import profile — the caller (``hitl``/``cli``) already knows
    the state dir via ``archive.state_dir``."""
    return Path(state_dir) / DENIALS_FILE


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
    sink: Path | None = None,
) -> dict:
    """Append one scrubbed audit record for `request` + its resolution. Returns
    the record dict (also useful for tests). Best-effort on I/O errors is the
    caller's job — this raises so a wiring bug is visible in tests, but the cli
    wrapper swallows it (an audit write must never fail a turn).

    `sink` overrides the destination file; it defaults to the in-workspace
    ``interrupts_path(workspace)``. Boundary denials pass ``denials_path(...)``
    so the record lands outside the agent's reach (see the module docstring)."""
    path = Path(sink) if sink is not None else interrupts_path(workspace)
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
        "meta": scrub_deep(dict(request.meta), env),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_records(workspace: Path, *, sink: Path | None = None) -> list[dict]:
    """Parse an audit log back into records (replay/inspection). Empty when the
    log does not exist yet. `sink` selects a non-default log (e.g.
    ``denials_path(state_dir)``), mirroring `record_interrupt`."""
    path = Path(sink) if sink is not None else interrupts_path(workspace)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
