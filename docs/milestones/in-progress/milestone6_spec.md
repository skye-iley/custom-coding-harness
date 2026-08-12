# Milestone 6 — Implementation Spec

> Implementation-level companion to `milestone6.md` (the plan) and `milestone6_invariants.md` (the
> checkable properties). Same relationship `milestone5_spec.md` has to `milestone5.md`.
>
> **Purpose: be sufficient to build from cold.** The plan says *what and why*; this says *exactly
> where, exactly what shape*. Where a detail could reasonably go two ways, it is decided here rather
> than left to the implementer — a decision made at the keyboard is a decision nobody reviewed.
>
> Everything here was read off the tree at `6e0c104`. Line numbers drift; the anchors are function
> and symbol names.

## 1. Module layout

| File | Role | May import |
|---|---|---|
| `harness/telemetry.py` **(new)** | record types, sink append, session derivation, the PR-block renderer | stdlib + `harness.scrub` |
| `harness/scrub.py` **(new)** | `scrub_value()` / `scrub_mapping()`, moved verbatim out of `audit.py` | stdlib only |
| `harness/audit.py` | imports the scrub from `harness.scrub`; behaviour unchanged | + `harness.scrub` |
| `harness/cli.py` | owns `TelemetryMiddleware`, feeds the sink, writes the summary | (already imports everything) |
| `harness/ratelimit.py` | instrumented limiter (§5) | unchanged deps |
| `harness/config.py` | one new `FieldSpec` (§7) | unchanged |

**Why `scrub.py` rather than importing `audit`:** invariant 21 allows `telemetry → audit`, but
`audit.py` imports `harness.interrupt`, which drags the M3 request model into a module that has
nothing to do with interrupts. A third leaf module keeps both dependency-light and is the smaller
diff. *(This supersedes the plan's "telemetry may import `harness.audit`" — update invariant 21 to
name `harness.scrub` when this lands.)*

`TelemetryMiddleware` lives in `cli.py`, not `telemetry.py`, for the same reason
`cli._LIVE_APPLIERS` does not live in `config.py`: it touches the agent runtime (`AgentMiddleware`
base ⇒ langchain), and `telemetry.py` must stay host-testable with no runtime stack.

## 2. Record schema — `<state-dir>/usage.jsonl`

One JSON object per line, UTF-8, `\n`-terminated, written with a single `write()` on a file opened
`"a"` (O_APPEND) so concurrent runs interleave at line granularity, never mid-line (invariant 25).

```json
{
  "schema": 1,
  "run_id": "run-20260812-143013-a1b2c3",
  "thread_id": "session-20260812-143013",
  "topic": "django__django-11099",
  "turn": 3,
  "ts": "2026-08-12T14:31:07.412Z",
  "provider": "ollama",
  "model": "gemma4",
  "input": 4821, "output": 512, "cache_read": 0, "cache_write": 0,
  "cost_usd": null, "cost_provenance": null,
  "energy_wh": 0.0142,
  "unpriced_calls": 0, "estimated_calls": 0,
  "duration_ms": 18432,
  "model_ms": 12010, "tool_ms": 4120, "retry_sleep_ms": 0,
  "paced_sleep_ms": 0, "hitl_wait_ms": 0,
  "model_calls": 3,
  "tool_calls": {"read_file": 2, "execute": 1},
  "tool_errors": 0,
  "failed": false,
  "retry_count": 0,
  "context_trimmed": false
}
```

Rules:

- **`schema: 1` is mandatory and first.** A reader that finds an unknown schema version must be able
  to skip the line rather than mis-parse it. Bump on any field removal or meaning change; adding an
  optional field does not bump.
- **Nullability is meaning, not convenience.** `cost_usd`/`cost_provenance` are `null` when no cost
  tracker exists (unpriced model, M1's null=MVP contract) — never `0.0`, which reads as "free" and
  is a different claim (invariant 5). `energy_wh` is `null` when the model carries no energy table.
- **`tool_calls` is an object**, name → count. Empty object, never `null`, when no tools ran.
- **`topic` is `null` when unset**, and is copied from the resolved `Settings.topic` at turn start
  so a mid-session `/config set topic` applies from the next turn onward.
- All `*_ms` are integers, floor-rounded, `≥ 0`.

## 3. Where each number is captured

`TelemetryMiddleware` (in `cli.py`) holds a `_TurnAccumulator` reset in `before_agent`.

| Field | Hook | Detail |
|---|---|---|
| `model_ms`, `model_calls` | `before_model` / `after_model` | `perf_counter` delta per call, summed. Mirrors how `WorkflowMiddleware` already uses these hooks |
| `tool_ms`, `tool_calls`, `tool_errors` | `wrap_tool_call(request, handler)` | name from `request.tool_call["name"]` — **the same access `PauseMiddleware` uses**; reading top-level `tool_name` is the M3 bug that let every call run ungated. `tool_errors` increments when `handler` raises or returns an error ToolMessage |
| `duration_ms` | `cli.run_turn`, around the whole turn | wall clock incl. everything below |
| `retry_sleep_ms`, `retry_count` | `cli._invoke_resilient` | §4 |
| `paced_sleep_ms` | `ratelimit` counter, sampled at turn boundaries | §5 |
| `hitl_wait_ms` | `hitl.run_interrupt_loop` | §6 |
| `context_trimmed` | `_invoke_resilient`'s overflow branch | set `True` before the single retry |
| tokens, cost, energy | `after_model`, same `usage_metadata` path `cost.py` parses | §8 — telemetry parses independently of the tracker |

### 3.1 Middleware order — decide it, then verify it

Current assembly order in `cli.main` is: `build_workflow_middleware(...)` → `tracker` (if any) →
`ArchiveMiddleware` (if any) → `PauseMiddleware` (if HITL).

**Append `TelemetryMiddleware` immediately after the workflow middleware and before the tracker.**
Rationale: telemetry should observe the *whole* turn including anything the later middlewares add.

**But this is the one composition fact not verified in this repo**: whether an earlier entry's
`wrap_tool_call` is the *outer* or *inner* wrapper is deepagents/langchain behaviour, and it decides
whether `tool_ms` includes `PauseMiddleware`'s HITL approval wait. Do not guess.

**Resolution step, first thing in T2:** add a throwaway two-middleware probe (both logging enter/exit
around `wrap_tool_call`), run one stubbed tool call, and record the observed nesting in this section
as a fact. Then:

- If telemetry ends up **outer**: subtract `hitl_wait_ms` from `tool_ms` before recording, so
  `tool_ms` stays "time the tool actually ran."
- If telemetry ends up **inner**: `tool_ms` already excludes the gate; `hitl_wait_ms` is then
  measured only in the interrupt loop (§6) and the decomposition still balances.

Either way the invariant is the same: **`tool_ms` never contains human wait time.**

## 4. Retry accounting (`cli._invoke_resilient`)

`resilience.retry_call` already takes `sleep=` as a parameter and an `on_retry(attempt, exc, delay)`
observer; today `cli` passes bare `time.sleep` and uses `on_retry` only for a stage marker. Change:

```python
slept_ms = 0
def _sleep(seconds: float) -> None:
    nonlocal slept_ms
    time.sleep(seconds)
    slept_ms += int(seconds * 1000)     # measured after, not the requested value
```

Accumulate the actual elapsed sleep, not the requested delay — they differ under load, and the
decomposition residual is where that difference would otherwise hide.

**Retries re-run the whole agent invoke**, so a retried turn's `model_ms`/`tool_ms` include the
abandoned attempt's work. That is correct for "what did this turn cost in wall clock", and
`retry_count > 0` is the flag that explains an outlier. State it in the field docs; do not try to
discount it.

## 5. Rate-limit pacing (`harness/ratelimit.py`)

`build_rate_limiter` returns a langchain `InMemoryRateLimiter` handed to the model as
`rate_limiter=`, so it blocks *inside* the model call. Wrap it:

```python
class _InstrumentedLimiter(InMemoryRateLimiter):
    """Same limiter, plus a monotonic counter of time spent blocked in acquire().

    Global (one limiter per process, shared across turns and threads), so callers
    take deltas across a turn boundary rather than reading an absolute.
    """
    total_blocked_ms = 0   # instance attribute, monotonically increasing
```

`acquire()` (and `aacquire()` if the async path is ever used) times itself and adds to
`total_blocked_ms`. `TelemetryMiddleware` samples the counter in `before_agent` and `after_agent`;
`paced_sleep_ms` is the delta.

**Two consequences to write down rather than discover:**
- Construction failure must keep degrading to the unpaced string path (`providers.resolve_chat_model`
  already does this) — instrumenting must not turn a soft failure into a hard one.
- With no tier selected there is **no limiter at all** (M1's inert-by-default contract), so
  `paced_sleep_ms` is `0`, not `null`. Zero here means "not paced", which is true.

## 6. HITL wait (`hitl.run_interrupt_loop`)

Time spent blocked on a human is wall clock inside the turn but is not the harness's or the model's
cost. Measure it in the interrupt loop (the one place that blocks on the channel) and expose it as
`hitl_wait_ms`, then include it in the decomposition:

```
residual = duration_ms - (model_ms + tool_ms + retry_sleep_ms + paced_sleep_ms + hitl_wait_ms)
```

Without this, invariant 4a's "residual is small" fails the moment anyone runs with HITL on — the
kind of invariant that gets weakened rather than fixed. In a benchmark sweep HITL is off and this is
`0`.

## 7. The on/off knob is a registry field, not a bare env read

M5.1's rule: **a knob is one `FieldSpec` entry, and nothing else.** Add to `FIELD_SPECS` in
`config.py`, positioned next to `mask_enabled`:

```python
FieldSpec(
    name="telemetry", tier="prespinup", env_var="DEEPAGENTS_TELEMETRY",
    profile_key="telemetry", cast=_to_bool, default=True, label="Telemetry",
),
```

- **`tier="prespinup"`** — read once when the middleware list is built; toggling mid-session would
  leave a half-recorded run, which is worse than either state.
- **No `choices`** — it is a bool; `choices` on a bool would reject `DEEPAGENTS_TELEMETRY=1`
  (M5.1's `test_registry_entries_are_internally_coherent` pins this).
- **`profile_key` set** ⇒ it persists, appears in `/config`'s read-only half, in
  `harness config show`, and gets a wizard question for free.

Then `Settings` gains the `telemetry: bool` field **in the same position** — M5.1 invariant 1
asserts `dataclasses.fields(Settings)` equals the registry names *in order*, so a mismatch fails
`test_settings_dataclass_exactly_matches_the_registry` immediately. That test is the guard; do not
work around it.

Deliberately **not** a bare `os.environ` read like `DEEPAGENTS_ARCHIVE`/`DEEPAGENTS_MASK`: those
predate M5, and `mask_enabled` is the post-M5 precedent for a feature toggle that belongs in the
registry.

## 8. Independence from the cost tracker

`build_cost_tracker` returns `None` when there is nothing to price — which is the shipped default
(`ollama:gemma4`, `pricing = "free"`) and therefore the local-benchmark case. So:

- `TelemetryMiddleware` parses `usage_metadata` from the model response itself, via the **same**
  `cost.py` helper the tracker uses (import the parsing function; do not re-implement token
  extraction — two parsers is how the numbers drift).
- When a tracker *is* present, telemetry must not double-count: it reads the response, the tracker
  reads the response, and they are independent observers of the same event. The session-level check
  (invariant 7, `session.json` vs the `past.sqlite` row) is what catches a divergence.
- `cost_usd` comes from the tracker when present, else `null`.

## 9. `session.json` — schema

Written by `cli.main` after `_finalize_session`, before `_pr_approval`.

```json
{
  "schema": 1,
  "run_id": "...", "thread_id": "...", "topic": "...",
  "started": "...", "ended": "...", "duration_ms": 412330,
  "turns": 7, "turns_failed": 1,
  "tokens": {"input": 41022, "output": 5120, "cache_read": 0, "cache_write": 0, "total": 46142},
  "cost_usd": null, "cost_provenance": null, "energy_wh": 0.121,
  "time": {"model_ms": 210400, "tool_ms": 88300, "retry_sleep_ms": 61000,
           "paced_sleep_ms": 0, "hitl_wait_ms": 0, "residual_ms": 52630},
  "models": {"ollama:gemma4": 7},
  "tools": {"read_file": 18, "execute": 9, "write_file": 4},
  "tool_errors": 2,
  "retries": 3, "context_trims": 1, "interrupts": 0,
  "usage_log": "/project/state/usage.jsonl"
}
```

`models` is turn counts per `provider:model` (a mid-session `/config set model` is why this is a
map, not a scalar). `residual_ms` is stored, not recomputed by readers — an explicit residual is
auditable; an implicit one is invisible.

## 10. PR body block (T4)

`open-pr.sh` builds the body with a heredoc into a temp file and passes `--body-file`. The block is
appended only when the summary parses:

```markdown
Automated PR generated by Deep Agents. **Manual review required. Auto-merge disabled.**

<!-- deepagents:telemetry -->
### Run telemetry
| | |
|---|---|
| Turns | 7 (1 failed) |
| Tokens | 46,142 (41,022 in / 5,120 out) |
| Cost | not priced (local model) |
| Wall clock | 6m 52s — model 3m 30s · tools 1m 28s · retry 1m 01s |
| Models | ollama:gemma4 |
```

- The HTML comment marker makes the block findable for a future updater without re-parsing prose.
- **Rendering happens in Python** (`telemetry.render_pr_block`, host-testable), invoked as
  `python3 -m harness telemetry pr-block`; the shell step only redirects it. Formatting money and
  durations in `sh` is how you get `0.00000001` and `NaN` in a PR body.
- Any failure — missing file, bad JSON, non-zero exit — leaves the current hardcoded body and exits
  0 (invariant 13). The step is `|| true` around the block, not around the whole PR creation.

## 11. `harness telemetry` subcommand (T5)

Routed in `dispatch` next to `doctor`/`config`, importing `harness.telemetry` lazily so the keyless
path stays keyless (invariant 22):

```
harness telemetry show [--run <run_id>] [--state-dir <path>]   # default: most recent run
harness telemetry list [--topic <label>] [--limit N]           # one line per run
harness telemetry pr-block [--run <run_id>]                    # stdout, used by open-pr.sh
```

Output shape follows `harness past show` (label-aligned key/value lines), so the two read alike.

## 12. Headless join (T5)

`cli._batch_payload` gains three keys — `run_id`, `topic`, `usage_log` — alongside the existing
`thread_id`. `run_id` already exists unconditionally in `cli.main`
(`run-{ts}-{uuid4[:6]}`, created whether or not the archive is enabled), so this is plumbing, not a
new identifier. Existing keys keep their names and meanings; this is additive.

## 13. Failure paths

| Case | Behaviour |
|---|---|
| Turn raises | record written with `failed: true` and whatever partial numbers exist, from `run_turn`'s existing `except` — before the error is re-raised/reported |
| Sink unwritable | one stderr `[harness] telemetry: <reason>` per run (not per turn), then silently skip. Never raises into the turn (invariant 3) |
| Session summary write fails | stderr note; the run still ends normally and git-pr still runs with the plain body |
| Archive disabled (`DEEPAGENTS_ARCHIVE=0`) | telemetry still records — `run_id` is independent of the archive. Invariant 7's cross-check is skipped, not failed, when there is no row to compare against |
| `--headless` | identical behaviour; the JSON gains the join keys |

## 14. Tests

| File | Tier | Covers |
|---|---|---|
| `tests/test_telemetry.py` **(new)** | host | record shape + schema key, nullability, derivation arithmetic, decomposition (4a–4f) with injected clocks, `render_pr_block` formatting, scrub |
| `tests/test_scrub.py` **(new)** | host | the moved scrub, unchanged behaviour (the existing `test_audit.py` scrub cases must still pass **unedited** — they are the oracle for the move) |
| `tests/test_cli.py` | image | middleware appended iff enabled, `_batch_payload` join keys, failed-turn record |
| `tests/test_config.py` | host | the new `FieldSpec` derives everywhere (M5.1's existing coverage tests do this for free once the entry exists) |
| `tests/test_workflows.py` | host | `open-pr.sh` no-ops on missing/malformed summary |
| `tests/test_live_model.py` | live | one real turn ⇒ non-zero tokens and `model_ms` a real fraction of `duration_ms` |

All writes go to `tmp_path` (or the `artifact_dir` fixture); the session-scoped `_clean_repo_artifacts`
guard is a backstop, not a licence.

## 15. Build order

T1 (`scrub.py` move + `telemetry.py` + record/summary types, all host-tested) → T7-in-T2 (the
middleware-order probe, §3.1) → T2 (capture) → T3 (summary) → T5 (subcommand + headless keys) →
T4 (PR block) → T6 (removability + live case). The config field (§7) lands with T2, since that is
where the toggle first has something to gate.

**Ship the probe result into §3.1 before writing capture code.** It decides one subtraction, and
getting it wrong produces numbers that look plausible and are wrong — the failure mode this whole
milestone exists to prevent.
