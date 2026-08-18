"""Telemetry — the per-turn sink, the derived session summary, and the readers.

Milestone 6 (``docs/milestones/complete/milestone6_spec.md``). The harness
already computes almost everything ``design_doc.md`` §8 asks for and throws most
of it away; this module is the missing **sink** and the missing **surface**.

Three things live here and nothing else:

* ``TurnRecord`` + ``record_turn`` — one scrubbed JSON object per turn, appended
  to ``<state-dir>/usage.jsonl``.
* ``derive_session`` — the session summary, *derived* from those records rather
  than accumulated in parallel. ``past.sqlite`` stays the authoritative ledger
  for session totals (fork 2); a derived file that disagrees with the row is a
  bug in the file.
* ``render_pr_block`` + ``telemetry_main`` — the read surfaces (PR body block,
  ``harness telemetry show|list|pr-block``).

**Placement is load-bearing.** The sink lives in the *state dir*, beside
``past.sqlite`` and ``denials.jsonl`` and outside the workspace mount, because
telemetry is an audit surface and the audited party must not be able to rewrite
the record (``milestone6.md`` §5a; the same reasoning M4 slice D applied to
``denials.jsonl``). Stated precisely rather than aspirationally: that is
**file-tool-proof always** (pathguard + the workspace-rooted backend cannot
address it) and **shell-proof only under ``DEEPAGENTS_JAIL=1``**. With the jail
off a container shell can still reach it by absolute path.

**Import profile:** stdlib plus ``harness.scrub`` — nothing else from the
package, at module level, ever (invariants 21/22). ``TelemetryMiddleware`` lives
in ``cli.py``, not here, for the same reason ``cli._LIVE_APPLIERS`` does not live
in ``config.py``: it needs the langchain ``AgentMiddleware`` base, and this module
must stay host-testable on a bare interpreter.

No prompt text, no reply text, no tool arguments — the record type has no field
for them (invariant 10). The scrub is a backstop, not the primary defence.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harness.scrub import scrub, scrub_deep

# Bump on any field REMOVAL or meaning change. Adding an optional field does not
# bump: a reader that finds an unknown key ignores it, but a reader that finds an
# unknown *schema* must be able to skip the line rather than mis-parse it.
SCHEMA_VERSION = 1

USAGE_FILE = "usage.jsonl"
SESSION_FILE = "session.json"

# How a turn ended. `failed` is derived from this (`outcome == OUTCOME_ERROR`) and
# not measured separately, so the reliability column cannot drift from the reason.
#
# The split exists because four different events used to arrive as one `failed`
# flag, and only the last of them is the harness breaking:
#
#   ok        the turn returned an answer
#   denied    the HITL gate denied a tool call under `on_deny: halt`
#   budget    a --max-cost / --max-tokens cap stopped the turn
#   cancelled the operator pressed Ctrl-C mid-turn
#   aborted   the headless interrupt policy fail-closed (EXIT_INTERRUPT_ABORT)
#   stopped   a --max-steps / --max-seconds / --max-turns bound fired (M8 B1)
#   error     a provider error outlasted the retries, or the harness raised
#
# The first six are *outcomes* — the harness did what it was configured to do.
# Counting them as failures puts a governance signal in the reliability column,
# which a benchmark sweep then reads as harness unreliability (invariant 2a). The
# original code excluded `denied` on exactly that reasoning and then included the
# other three, which is the inconsistency this field removes.
#
# `stopped` is Milestone 8's addition and is deliberately NOT folded into either
# neighbour. Folding a clock or step stop into `budget` says "the operator's cap
# fired" about an event closer to "the agent did not converge", and the two lead
# to opposite actions (raise the cap vs. fix the loop). Folding it into `error` is
# the defect `milestone8.md` §3 documents: `GraphRecursionError` falls through to
# OUTCOME_ERROR today, so a truncated instance is recorded identically to a
# crashed one, and a sweep's failure count mixes the two. Which of the three
# bounds fired is `stop_reason`, not a third outcome.
OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"
OUTCOME_BUDGET = "budget"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_ABORTED = "aborted"
OUTCOME_STOPPED = "stopped"
OUTCOME_ERROR = "error"
OUTCOMES = (
    OUTCOME_OK,
    OUTCOME_DENIED,
    OUTCOME_BUDGET,
    OUTCOME_CANCELLED,
    OUTCOME_ABORTED,
    OUTCOME_STOPPED,
    OUTCOME_ERROR,
)

# Marks the generated block in a PR body so a future updater can find it without
# re-parsing prose.
PR_BLOCK_MARKER = "<!-- deepagents:telemetry -->"


def usage_path(state_dir: Path | str) -> Path:
    """``<state-dir>/usage.jsonl`` — the per-turn sink (§5a: agent-unreachable)."""
    return Path(state_dir) / USAGE_FILE


def session_path(state_dir: Path | str) -> Path:
    """``<state-dir>/session.json`` — the derived per-run summary."""
    return Path(state_dir) / SESSION_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- the per-turn record ------------------------------------------------------


@dataclass
class TurnRecord:
    """One turn's measurements. Field order here IS the on-disk key order.

    Nullability is meaning, not convenience:

    * ``cost_usd`` / ``cost_provenance`` are ``None`` when no cost tracker exists
      (an unpriced model — M1's null=MVP contract), never ``0.0``. Zero reads as
      "free", which is a different claim (invariant 5).
    * ``energy_wh`` is ``None`` when the model carries no energy table.
    * ``topic`` is ``None`` when unset.
    * ``paced_sleep_ms`` is ``0`` and never ``None``: with no tier selected there
      is no limiter at all, and zero here means "not paced", which is true.

    ``tool_calls`` is a name -> count mapping (an empty dict, never ``None``), so
    a per-tool mix survives into the summary. All ``*_ms`` are non-negative
    floor-rounded integers.

    ``interrupts`` is an addition to ``milestone6_spec.md`` §2's list: the summary
    schema (§9) carries an interrupt count, and every summary field must be
    *derivable from the records* (invariant 6). Counting it at the same seam that
    measures ``hitl_wait_ms`` is the only way to get that without a second
    accumulator. Additive, so no schema bump (§2's own rule).
    """

    run_id: str
    thread_id: str | None = None
    topic: str | None = None
    turn: int = 0
    ts: str = field(default_factory=_now_iso)

    provider: str | None = None
    model: str | None = None

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    cost_usd: float | None = None
    cost_provenance: str | None = None
    energy_wh: float | None = None
    unpriced_calls: int = 0
    estimated_calls: int = 0

    duration_ms: int = 0
    model_ms: int = 0
    tool_ms: int = 0
    retry_sleep_ms: int = 0
    paced_sleep_ms: int = 0
    hitl_wait_ms: int = 0

    model_calls: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0

    outcome: str = OUTCOME_OK
    stop_reason: str | None = None
    retry_count: int = 0
    context_trimmed: bool = False
    interrupts: int = 0

    @property
    def failed(self) -> bool:
        """Derived, never stored separately: only ``error`` is a failure.

        A property rather than a field so the two can't disagree. `failed` is
        still written to disk (readers and the summary use it, and dropping a
        field would need a schema bump) — but it is a *view* of `outcome`."""
        return self.outcome == OUTCOME_ERROR

    def to_dict(self) -> dict:
        """The on-disk object, ``schema`` first and mandatory."""
        return {
            "schema": SCHEMA_VERSION,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "topic": self.topic,
            "turn": self.turn,
            "ts": self.ts,
            "provider": self.provider,
            "model": self.model,
            "input": int(self.input),
            "output": int(self.output),
            "cache_read": int(self.cache_read),
            "cache_write": int(self.cache_write),
            "cost_usd": self.cost_usd,
            "cost_provenance": self.cost_provenance,
            "energy_wh": self.energy_wh,
            "unpriced_calls": int(self.unpriced_calls),
            "estimated_calls": int(self.estimated_calls),
            "duration_ms": _ms(self.duration_ms),
            "model_ms": _ms(self.model_ms),
            "tool_ms": _ms(self.tool_ms),
            "retry_sleep_ms": _ms(self.retry_sleep_ms),
            "paced_sleep_ms": _ms(self.paced_sleep_ms),
            "hitl_wait_ms": _ms(self.hitl_wait_ms),
            "model_calls": int(self.model_calls),
            "tool_calls": {str(k): int(v) for k, v in (self.tool_calls or {}).items()},
            "tool_errors": int(self.tool_errors),
            "outcome": (self.outcome if self.outcome in OUTCOMES else OUTCOME_ERROR),
            # Null on every turn that was not stopped, so a reader can tell "no
            # bound fired" from "a bound fired and we lost which one". Additive
            # and nullable, so no schema bump: `outcome` was always declared as an
            # enum that could grow (§2's own rule).
            "stop_reason": self.stop_reason,
            "failed": bool(self.failed),
            "retry_count": int(self.retry_count),
            "context_trimmed": bool(self.context_trimmed),
            "interrupts": int(self.interrupts),
        }


def _ms(value) -> int:
    """Floor-round to a non-negative integer millisecond count."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _scrub_all(value, env: dict | None = None):
    """``scrub_deep`` extended to dict **keys** as well as values.

    ``audit``'s ``scrub_deep`` deliberately leaves keys alone: ``meta``'s keys are
    harness-chosen labels (``path``, ``op``, ``reason``), so scrubbing them would
    be work with no leak to prevent. Telemetry is different — ``tool_calls`` is
    keyed by the *tool name off the model's tool call*, which is not a harness
    constant. One redaction implementation still (``scrub``); only the traversal
    is wider, and the wider one belongs here rather than in the shared module
    whose oracle test pins the narrower behaviour.
    """
    if isinstance(value, dict):
        return {scrub(str(k), env): _scrub_all(v, env) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_all(v, env) for v in value]
    return scrub_deep(value, env)


def record_turn(path: Path | str, record: TurnRecord | dict, env: dict | None = None) -> dict:
    """Append one scrubbed record to `path`; return the dict that was written.

    Written with a **single** ``write()`` on a file opened ``"a"`` (O_APPEND), so
    two concurrent runs against one state dir interleave at line granularity and
    never mid-line (invariant 25) — separation is by the ``run_id`` field, never
    by file position.

    This **raises** on I/O failure, exactly as ``audit.record_interrupt`` does, so
    a wiring bug is visible in tests. The turn path's caller (``cli.run_turn``)
    wraps it and degrades to one stderr warning per run — a telemetry write must
    never break a turn (invariant 3).
    """
    payload = record.to_dict() if isinstance(record, TurnRecord) else dict(record)
    payload = _scrub_all(payload, env)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def read_records(path: Path | str, run_id: str | None = None) -> list[dict]:
    """Parse the sink back into records, optionally filtered to one ``run_id``.

    Tolerant by design: a truncated or unknown-schema line is skipped rather than
    fatal. The sink is append-only and concurrently written, so a reader that dies
    on one bad line is a reader that cannot be trusted mid-run.
    """
    target = Path(path)
    if not target.is_file():
        return []
    out: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_VERSION:
            continue
        if run_id is not None and obj.get("run_id") != run_id:
            continue
        out.append(obj)
    return out


def latest_run_id(path: Path | str) -> str | None:
    """The ``run_id`` of the last record in the sink, or ``None`` when empty."""
    records = read_records(path)
    return records[-1].get("run_id") if records else None


# --- the derived session summary ---------------------------------------------


def _sum(records: list[dict], key: str) -> int:
    return sum(int(r.get(key) or 0) for r in records)


def _sum_optional(records: list[dict], key: str) -> float | None:
    """Sum the non-null values, or ``None`` when every record is null.

    The distinction is the point: "no record carried a cost" and "the cost was
    zero" are different claims, and collapsing them is exactly the fabrication
    invariant 5 forbids.
    """
    vals = [r.get(key) for r in records if r.get(key) is not None]
    if not vals:
        return None
    return sum(float(v) for v in vals)


def _outcome_counts(records: list[dict]) -> dict[str, int]:
    """``{outcome: n}`` over the records, in ``OUTCOMES`` order, zeroes omitted.

    Sparse on purpose: a run where every turn succeeded reports ``{"ok": 7}``, not
    five zeros the reader has to skim past. Ordered by ``OUTCOMES`` rather than by
    count so two runs' summaries line up column-for-column.

    An unrecognized value (a record from a newer schema, or a hand-edited file) is
    counted under its own key rather than dropped — losing a turn from the tally
    would make the outcomes stop summing to ``turns``, and that identity is what
    makes the block auditable.
    """
    counts: dict[str, int] = {}
    for r in records:
        name = r.get("outcome")
        if not name:
            # Pre-`outcome` record: reconstruct the only distinction it carried.
            name = OUTCOME_ERROR if r.get("failed") else OUTCOME_OK
        counts[str(name)] = counts.get(str(name), 0) + 1
    known = {k: counts[k] for k in OUTCOMES if k in counts}
    extra = {k: v for k, v in counts.items() if k not in OUTCOMES}
    return {**known, **extra}


def _stop_reason_counts(records: list[dict]) -> dict[str, int]:
    """``{stop_reason: n}`` over the stopped turns, zeroes omitted.

    Which bound fired is the actionable half of a ``stopped`` outcome: a sweep
    that is mostly ``stopped``/``steps`` is reporting the bound the operator set,
    not the harness (``milestone8.md`` §8). Unrecognised values are counted under
    their own key for the same reason ``_outcome_counts`` does it — a tally that
    silently drops a turn is worse than one with an odd key in it.
    """
    counts: dict[str, int] = {}
    for r in records:
        reason = r.get("stop_reason")
        if not reason:
            continue
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    return counts


def derive_session(
    records: list[dict],
    *,
    run_id: str | None = None,
    thread_id: str | None = None,
    topic: str | None = None,
    started: str | None = None,
    ended: str | None = None,
    duration_ms: int | None = None,
    usage_log: str | None = None,
) -> dict:
    """The session summary, derived field-by-field from `records`.

    Derived, never independently accumulated (invariant 6): every number here is
    a fold over the turn records, so the summary cannot drift from them. The
    tokens/cost it produces must also equal the ``past.sqlite`` row (invariant 7),
    which holds because ``cli`` writes both off ``cost._split_tokens``' fresh-input
    split — cache-read tokens split *out of* ``input``, the same definition
    ``UsageAccumulator`` and ``_cost_totals_for_row`` use.

    A zero-turn run still produces a valid summary (zeros, empty maps) — "the
    operator opened a session and typed /exit" must not yield a file the reader
    has to special-case (invariant 9).

    ``duration_ms`` is the *session* wall clock when the caller supplies it; with
    nothing supplied it falls back to the sum of the turns, which is a lower bound
    (it excludes idle time at the prompt). ``residual_ms`` is stored rather than
    left for readers to recompute — an explicit residual is auditable, an implicit
    one is invisible.
    """
    run_id = run_id or (records[0].get("run_id") if records else None)
    thread_id = thread_id or (records[0].get("thread_id") if records else None)
    if topic is None and records:
        topic = records[-1].get("topic")

    turn_ms = _sum(records, "duration_ms")
    total_ms = _ms(duration_ms) if duration_ms is not None else turn_ms

    time_block = {
        "model_ms": _sum(records, "model_ms"),
        "tool_ms": _sum(records, "tool_ms"),
        "retry_sleep_ms": _sum(records, "retry_sleep_ms"),
        "paced_sleep_ms": _sum(records, "paced_sleep_ms"),
        "hitl_wait_ms": _sum(records, "hitl_wait_ms"),
    }
    # The one inferred number in the whole schema. Bounded, not assumed zero —
    # this is what catches a future blocking call (a streaming path, a second
    # limiter) silently disappearing into "overhead".
    time_block["residual_ms"] = total_ms - sum(time_block.values())

    tokens = {
        "input": _sum(records, "input"),
        "output": _sum(records, "output"),
        "cache_read": _sum(records, "cache_read"),
        "cache_write": _sum(records, "cache_write"),
    }
    tokens["total"] = sum(tokens.values())

    models: dict[str, int] = {}
    for r in records:
        provider, model = r.get("provider"), r.get("model")
        if not model:
            continue
        key = f"{provider}:{model}" if provider else str(model)
        models[key] = models.get(key, 0) + 1

    tools: dict[str, int] = {}
    for r in records:
        for name, n in (r.get("tool_calls") or {}).items():
            tools[name] = tools.get(name, 0) + int(n or 0)

    cost = _sum_optional(records, "cost_usd")
    # Provenance of the run as a whole: the last turn that had one. A mid-session
    # `/config set model` can change it, and the end-of-run posture is the one the
    # ledger row also records.
    provenance = None
    for r in records:
        if r.get("cost_provenance") is not None:
            provenance = r["cost_provenance"]

    return {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "thread_id": thread_id,
        "topic": topic,
        "started": started or (records[0].get("ts") if records else None),
        "ended": ended or (records[-1].get("ts") if records else None),
        "duration_ms": total_ms,
        "turns": len(records),
        # Only `error` counts. A record written before `outcome` existed carries
        # `failed` alone, so fall back to it — a reader of an older sink must not
        # silently report zero failures.
        "turns_failed": sum(
            1
            for r in records
            if (r["outcome"] == OUTCOME_ERROR if r.get("outcome") else r.get("failed"))
        ),
        "outcomes": _outcome_counts(records),
        # Sparse, same convention as `outcomes`: `{}` on a run where nothing was
        # stopped. Derived from the records like everything else here (invariant
        # 6), so `outcomes["stopped"]` and the sum of this map agree by
        # construction rather than by a second accumulator.
        "stop_reasons": _stop_reason_counts(records),
        "tokens": tokens,
        "cost_usd": (round(cost, 6) if cost is not None else None),
        "cost_provenance": provenance,
        "energy_wh": _sum_optional(records, "energy_wh"),
        "time": time_block,
        "models": models,
        "tools": tools,
        "tool_errors": _sum(records, "tool_errors"),
        "retries": _sum(records, "retry_count"),
        "context_trims": sum(1 for r in records if r.get("context_trimmed")),
        "interrupts": _sum(records, "interrupts"),
        "usage_log": usage_log,
    }


def write_session(path: Path | str, summary: dict, env: dict | None = None) -> dict:
    """Write the scrubbed summary to `path` (whole-file, not append). Raises on
    I/O failure; ``cli`` wraps it so a summary failure never fails the run."""
    payload = _scrub_all(dict(summary), env)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def read_session(path: Path | str) -> dict | None:
    """Parse ``session.json``, or ``None`` when absent/unreadable/wrong schema.

    Never raises: every caller (the PR block, ``telemetry show``) has a defined
    behaviour for "no summary", and a malformed file must take that path rather
    than the traceback one (invariant 13).
    """
    target = Path(path)
    if not target.is_file():
        return None
    try:
        obj = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_VERSION:
        return None
    return obj


# --- formatting ---------------------------------------------------------------


def format_duration(ms) -> str:
    """``6m 52s`` / ``52s`` / ``1h 04m 03s``. Human units, computed in Python —
    formatting durations in ``sh`` is how a PR body ends up with ``NaN``."""
    total = _ms(ms) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def format_cost(cost_usd, provenance: str | None = None) -> str:
    """A dollar figure, or the reason there isn't one.

    ``None`` is not ``$0.00``: it means nothing priced this run (an unpriced model
    — the shipped local-Ollama default), and printing zero would state a
    measurement the harness never made.
    """
    if cost_usd is None:
        return "not priced (no cost tracker for this model)"
    text = f"${float(cost_usd):.4f}"
    if provenance == "estimate":
        text = "~" + text + " (estimated)"
    return text


def _stop_reason_suffix(summary: dict) -> str:
    """`` [steps=1, seconds=2]`` when a bound fired, ``""`` otherwise."""
    reasons = summary.get("stop_reasons") or {}
    if not reasons:
        return ""
    return " [" + ", ".join(f"{k}={v}" for k, v in reasons.items()) + "]"


def render_pr_block(summary: dict | None) -> str:
    """The markdown block appended to the PR body (§10). ``""`` when there is no
    summary, so the caller's fallback is "append nothing" rather than a branch.

    Aggregates only, by construction: every value here comes from ``session.json``,
    which has no free-text field (invariant 12).
    """
    if not summary:
        return ""
    tokens = summary.get("tokens") or {}
    time_block = summary.get("time") or {}
    turns = summary.get("turns", 0)
    # Everything that did not end `ok`, named. A reviewer reading the PR body
    # needs to know a run stopped on its budget rather than crashed, and
    # "(2 failed)" cannot say that. Plain-`ok` runs get no parenthetical at all.
    other = {
        k: v for k, v in (summary.get("outcomes") or {}).items()
        if k != OUTCOME_OK and v
    }
    if other:
        turns_text = f"{turns} ({', '.join(f'{v} {k}' for k, v in other.items())})"
    else:
        failed = summary.get("turns_failed", 0)
        turns_text = f"{turns}" + (f" ({failed} failed)" if failed else "")
    tokens_text = (
        f"{tokens.get('total', 0):,} "
        f"({tokens.get('input', 0):,} in / {tokens.get('output', 0):,} out)"
    )
    clock = (
        f"{format_duration(summary.get('duration_ms'))} — "
        f"model {format_duration(time_block.get('model_ms'))} · "
        f"tools {format_duration(time_block.get('tool_ms'))} · "
        f"retry {format_duration(time_block.get('retry_sleep_ms'))}"
    )
    models = ", ".join(summary.get("models") or {}) or "—"
    lines = [
        PR_BLOCK_MARKER,
        "### Run telemetry",
        "| | |",
        "|---|---|",
        f"| Turns | {turns_text} |",
        f"| Tokens | {tokens_text} |",
        f"| Cost | {format_cost(summary.get('cost_usd'), summary.get('cost_provenance'))} |",
        f"| Wall clock | {clock} |",
        f"| Models | {models} |",
    ]
    energy = summary.get("energy_wh")
    if energy:
        lines.append(f"| Energy | {float(energy):.3f} Wh |")
    return "\n".join(lines) + "\n"


def format_show(summary: dict, source: str) -> str:
    """Label-aligned key/value lines, following ``harness past show``'s shape so
    the two read alike.

    `source` names where the numbers came from — "read from session.json" and
    "derived from 6 turn records (session not finalized)" are different claims
    about the same figures, and a reader deciding whether to trust a crashed run's
    totals needs to know which one they have.
    """
    tokens = summary.get("tokens") or {}
    time_block = summary.get("time") or {}
    rows = [
        ("run_id", summary.get("run_id")),
        ("thread_id", summary.get("thread_id")),
        ("topic", summary.get("topic") or "-"),
        ("started", summary.get("started")),
        ("ended", summary.get("ended")),
        ("duration", format_duration(summary.get("duration_ms"))),
        ("turns", f"{summary.get('turns', 0)} ({summary.get('turns_failed', 0)} failed)"),
        (
            "outcomes",
            (", ".join(f"{k}={v}" for k, v in (summary.get("outcomes") or {}).items()) or "-")
            # Which bound fired, inline rather than as its own row: it is
            # meaningless without the `stopped` count beside it, and a row that is
            # "-" on every unstopped run is a row nobody reads.
            + _stop_reason_suffix(summary),
        ),
        (
            "tokens",
            f"{tokens.get('total', 0)} (in={tokens.get('input', 0)} out={tokens.get('output', 0)}"
            f" cache_r={tokens.get('cache_read', 0)} cache_w={tokens.get('cache_write', 0)})",
        ),
        ("cost", format_cost(summary.get("cost_usd"), summary.get("cost_provenance"))),
        (
            "energy",
            f"{float(summary['energy_wh']):.3f} Wh" if summary.get("energy_wh") is not None else "-",
        ),
        (
            "time",
            " ".join(
                f"{k.removesuffix('_ms')}={v}ms"
                for k, v in time_block.items()
            ),
        ),
        ("models", ", ".join(f"{k}={v}" for k, v in (summary.get("models") or {}).items()) or "-"),
        ("tools", ", ".join(f"{k}={v}" for k, v in (summary.get("tools") or {}).items()) or "-"),
        ("tool_errors", summary.get("tool_errors", 0)),
        ("retries", summary.get("retries", 0)),
        ("context_trims", summary.get("context_trims", 0)),
        ("interrupts", summary.get("interrupts", 0)),
        ("usage_log", summary.get("usage_log") or "-"),
        ("source", source),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)


# --- the `harness telemetry` subcommand ---------------------------------------


def _resolve_state_dir(explicit: str | None) -> Path:
    """The state dir to read, honouring ``--state-dir`` then the same env/workspace
    fallback ``archive.state_dir`` implements.

    ``archive`` is imported **inside the function** on purpose: this module must
    import nothing from the package but ``harness.scrub`` at module level
    (invariant 22), and re-deriving the fallback here instead would give the repo
    two definitions of where state lives — the exact drift the one-authority rule
    exists to prevent.
    """
    if explicit:
        return Path(explicit)
    from harness import archive

    workspace = Path.cwd()
    return archive.state_dir(workspace)


def _summary_for(state_dir: Path, run_id: str | None) -> tuple[dict | None, str]:
    """(summary, source) for `run_id`, preferring ``session.json`` and falling back
    to deriving from the records — a crashed run has records and no summary (§9)."""
    sink = usage_path(state_dir)
    stored = read_session(session_path(state_dir))
    wanted = run_id or (stored or {}).get("run_id") or latest_run_id(sink)
    if stored is not None and (run_id is None or stored.get("run_id") == run_id):
        return stored, "read from session.json"
    records = read_records(sink, run_id=wanted)
    if not records:
        return None, "no records"
    return (
        derive_session(records, run_id=wanted, usage_log=str(sink)),
        f"derived from {len(records)} turn record(s) — session not finalized",
    )


def telemetry_main(argv: list[str]) -> int:
    """``harness telemetry show|list|pr-block``.

    Needs no API key, no network and no model — and, since M5 §0.1 F6 landed
    (``harness/entry.py`` + the lazy ``harness/__init__`` ``__getattr__``), no
    runtime stack either. ``entry.dispatch`` routes here without importing
    ``cli``, and this module imports nothing from the package but
    ``harness.scrub`` at module level, so reading a run's numbers costs a bare
    interpreter. ``tests/test_import_isolation.py`` pins that.
    """
    parser = argparse.ArgumentParser(prog="harness telemetry", add_help=True)
    parser.add_argument("action", choices=("show", "list", "pr-block"))
    parser.add_argument("--run", default=None, help="run_id (default: most recent)")
    parser.add_argument("--state-dir", default=None, help="state dir to read")
    parser.add_argument("--topic", default=None, help="list: filter by topic")
    parser.add_argument("--limit", type=int, default=20, help="list: max runs")
    args = parser.parse_args(argv)

    state_dir = _resolve_state_dir(args.state_dir)
    sink = usage_path(state_dir)

    if args.action == "list":
        records = read_records(sink)
        by_run: dict[str, list[dict]] = {}
        for r in records:
            by_run.setdefault(str(r.get("run_id")), []).append(r)
        rows = [
            derive_session(rs, run_id=rid)
            for rid, rs in by_run.items()
        ]
        if args.topic is not None:
            rows = [s for s in rows if s.get("topic") == args.topic]
        if not rows:
            print("[harness] no telemetry records.")
            return 0
        for summary in rows[-max(args.limit, 1):]:
            print(
                f"{summary['run_id']}\ttopic={summary.get('topic') or '-'}"
                f"\tturns={summary['turns']}"
                f"\ttokens={(summary.get('tokens') or {}).get('total', 0)}"
                f"\tcost={format_cost(summary.get('cost_usd'))}"
                f"\ttime={format_duration(summary.get('duration_ms'))}"
            )
        return 0

    summary, source = _summary_for(state_dir, args.run)
    if summary is None:
        if args.action == "pr-block":
            # No summary => no block. The PR body keeps its hardcoded text and the
            # step exits 0 (invariant 13): a telemetry gap must never be the reason
            # a PR does not open.
            return 0
        print(f"[harness] no telemetry for {args.run or 'the most recent run'} in {state_dir}")
        return 1

    if args.action == "pr-block":
        sys.stdout.write(render_pr_block(summary))
        return 0

    print(format_show(summary, source))
    return 0
