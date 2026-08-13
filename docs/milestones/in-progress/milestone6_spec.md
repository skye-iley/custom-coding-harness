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
>
> **Amended before build, still docs-only.** A pre-build pass re-checked every seam this doc names
> against the tree and found five claims that did not hold, plus five decisions the doc had left to
> the keyboard. Both are settled in place, each at the section that owns it, with the superseded
> version written out rather than deleted:
>
> | § | Was | Now |
> |---|---|---|
> | 1 | scrub is `scrub_value`/`scrub_mapping` | it is `scrub`/`scrub_deep`, and the names must not change (§14's oracle) |
> | 3.2 **(new)** | — | how the middleware's numbers reach the record; the write is `run_turn`'s `finally` |
> | 5 | counter on the limiter instance | module-level — `cli` never gets a reference to the instance |
> | 6 | "measure it in the interrupt loop" | via an `on_wait` callback; the return value does not change |
> | 7 | `profile_key="telemetry"` | `profile_key=None` — the cited `mask_enabled` precedent says the opposite |
> | 8 | "the same `cost.py` helper" | named: `_latest_usage` + `_split_tokens`, and why invariants 6/7 depend on it |
> | 9 | "after `_finalize_session`" | *outside* the `archive_conn` guard that call sits inside |
> | 10 | open-pr.sh "builds the body with a heredoc" | it does not; that conversion is T4 work, and the interpreter needs `/project` on the path |
> | 11 | "the keyless path stays keyless" | narrowed — `harness/__init__.py` already imports `cli` unconditionally |
> | 13 | record from `run_turn`'s existing `except` | there is no such `except`; see §3.2 |
> | 15.1 **(new)** | — | the full list of signatures this milestone changes |
>
> **Two of those rows were themselves superseded at merge time**, by the pre-merge review and by M5
> §0.1 F6 landing on `main` first. Row 10's `/project` is now derived from the script's own location
> (it has to be right on a host too — that is where the `open-pr.sh` tests run), and row 11's
> narrowing is undone: `harness telemetry` really is stdlib-only now, because `harness/entry.py`
> routes it without importing `cli`. Both are written out where they live, §10 and §11.

## 1. Module layout

| File | Role | May import |
|---|---|---|
| `harness/telemetry.py` **(new)** | record types, sink append, session derivation, the PR-block renderer | stdlib + `harness.scrub` |
| `harness/scrub.py` **(new)** | `scrub()` / `scrub_deep()` + `_SECRET_KEY_MARKERS` / `_SECRET_PATTERN` / `_REDACTED`, moved verbatim out of `audit.py` | stdlib only |
| `harness/audit.py` | `from harness.scrub import scrub, scrub_deep`; behaviour unchanged | + `harness.scrub` |
| `harness/cli.py` | owns `TelemetryMiddleware`, feeds the sink, writes the summary | (already imports everything) |
| `harness/ratelimit.py` | instrumented limiter (§5) | unchanged deps |
| `harness/config.py` | one new `FieldSpec` (§7) | unchanged |

**Why `scrub.py` rather than importing `audit`:** invariant 21 allows `telemetry → audit`, but
`audit.py` imports `harness.interrupt`, which drags the M3 request model into a module that has
nothing to do with interrupts. A third leaf module keeps both dependency-light and is the smaller
diff. *(This supersedes the plan's "telemetry may import `harness.audit`" — update invariant 21 to
name `harness.scrub` when this lands.)*

**The function names do not change.** They are `scrub(text, env=None)` and `scrub_deep(value,
env=None)` today (`audit.py`), and they keep those names in `scrub.py`; `audit.py` re-exports both
so `audit.scrub(...)` still resolves. This is not cosmetic — §14 makes the existing `test_audit.py`
scrub cases the **oracle** for the move, and they must pass *unedited*. A rename would force an edit
to the very test that proves the move was behaviour-preserving, which is the one thing that must not
happen. The module-private constants (`_SECRET_KEY_MARKERS`, `_SECRET_PATTERN`, `_REDACTED`,
`_secret_values`) move with them; only `TELEMETRY_DIRNAME` / `INTERRUPTS_FILE` / `DENIALS_FILE` and
the record functions stay in `audit.py`.

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
  "outcome": "ok",
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
| `duration_ms` | `cli.run_turn`, around the whole turn | wall clock incl. everything below. **`run_turn`, not `run_repl`** — §3.2. Covers the `--stream` branch too: it bypasses the resilience layer and the HITL loop but is still a completed turn, and a mode that silently produced no record would be a hole shaped exactly like a bug |
| `retry_sleep_ms`, `retry_count` | `cli._invoke_resilient` | §4 |
| `paced_sleep_ms` | `ratelimit` module counter, sampled at turn boundaries | §5 |
| `hitl_wait_ms` | `hitl.run_interrupt_loop` | §6 |
| `context_trimmed` | `_invoke_resilient`'s overflow branch | set `True` before the single retry |
| tokens, cost, energy | `after_model`, same `usage_metadata` path `cost.py` parses | §8 — telemetry parses independently of the tracker |

### 3.1 Middleware order — decide it, then verify it

Current assembly order in `cli.main` is: `build_workflow_middleware(...)` → `tracker` (if any) →
`ArchiveMiddleware` (if any) → `PauseMiddleware` (if HITL).

**Append `TelemetryMiddleware` immediately after the workflow middleware and before the tracker.**
Rationale: telemetry should observe the *whole* turn including anything the later middlewares add.

**RESOLVED — telemetry is the OUTER wrapper, and no subtraction is needed.** The composition fact
and the conclusion drawn from it are two separate findings; both are recorded because the second one
reverses what this section originally planned to do about the first.

**Finding 1 — first in the list is outermost.** `langchain.agents.factory._chain_tool_call_wrappers`
(langchain 1.3.15) composes in middleware order with the docstring *"Compose wrappers into middleware
stack (first = outermost)"*, and `compose_two(outer, inner)` calls `outer(request, call_inner)`.
Read off the resolved dependency rather than inferred from a timing probe, so the answer is the
mechanism, not a measurement of it. Telemetry is appended before `PauseMiddleware`, so telemetry
wraps the gate.

**Finding 2 — the HITL wait is not inside `wrap_tool_call` at all**, so being outer costs nothing.
`PauseMiddleware.wrap_tool_call` calls `interrupt.raise_interrupt(...)`, i.e. langgraph's
`interrupt()`, which **raises `GraphInterrupt` and suspends the graph**. The human is asked in
`hitl.run_interrupt_loop`, which runs in `run_turn` *after* `agent.invoke` has already returned.
Nothing blocks on a human inside the wrapper, so the planned subtraction would have removed time
that was never counted — it would have made `tool_ms` wrong in the other direction. Do not
implement it.

The invariant is unchanged and now holds structurally: **`tool_ms` never contains human wait time.**

**But finding 2 has a consequence this section originally missed**, and it is the real reason the
probe was worth doing. Because the gate raises *through* telemetry's wrapper:

- **`GraphInterrupt` and `HaltTurn` must not count as tool errors.** They are control flow, not a
  failed tool. Counting them would put every gated call in the reliability column — invariant 2a's
  mistake one level down.
- **A gated tool call enters `wrap_tool_call` twice** — once for the suspend, once on resume. Only
  the entry that actually reaches the tool may increment `tool_calls`, or a HITL run reports double
  the tool work it did.
- **Neither entry contributes to `tool_ms` unless the tool ran.** Time spent building the approval
  prompt is harness overhead and belongs in the residual.

### 3.2 How the middleware's numbers reach the record — and where the record is written

Two halves measure one turn: the **middleware** owns `model_ms` / `tool_ms` / `tool_calls` /
`model_calls` / `tool_errors` (it is the only thing on the hook seams), and **`run_turn`** owns
`duration_ms`, `failed`, `retry_*`, `context_trimmed` and the write (it is the only thing that
brackets the whole turn including a raise). They have to meet somewhere; an earlier draft did not
say where.

**CORRECTION — `before_agent` is not the turn boundary, and the reset must not live there.** This
section (and §3's table) assumed `before_agent` fires once per turn. It fires once per **invoke**,
and a turn invokes several times: the resilience layer re-invokes on a retry, and every HITL resume
is another invoke. A reset there wipes `retry_sleep_ms`/`retry_count` accumulated *between* invokes
and every pre-suspend tool and model count on a gated turn. `run_turn` is the only thing that
brackets a whole turn, so `TelemetryMiddleware.begin_turn()` is called from there and nowhere else;
the middleware defines no `before_agent` at all.

**Same defect, same fix, for cost.** §8 says to read `tracker.turn.cost` because
"`CostTrackerMiddleware.before_agent` resets `turn`" — true, and that is precisely the problem:
`tracker.turn` holds only the *last invoke's* cost on a retried or gated turn. The per-turn cost is
therefore a **delta against `tracker.session`**, which is never reset: snapshot at `begin_turn`,
subtract at write. Correct for any number of invokes, and it makes the per-turn costs sum to the
session total by construction — which is what invariant 7 actually needs, rather than something the
test has to hope for.

**The accumulator is a constructor argument, threaded like `tracker` already is.** `cli.main` builds
one `TelemetryMiddleware` and passes it into `run_repl` / `run_batch`, which pass it into `run_turn`
— the same shape `tracker: CostTrackerMiddleware | None` takes through those three signatures today.
Not a module global: at most one `run_turn` runs per process today, but a global would make the host
tests order-dependent and a future parallel driver silently wrong.

**The write happens in a `try`/`finally` inside `run_turn`.** Not in `after_agent`, and not in the
callers' `except` blocks. Three reasons, each of which alone decides it:

- **`run_turn` has no general `except` to hook.** It catches only `hitl.HaltTurn` (`cli.py:552`).
  The general handlers live in **`run_repl`** *and* **`run_batch`** — two of them, so "the existing
  except" is ambiguous and hooking either one misses the other path. *(This supersedes the §13 row
  that named `run_turn`'s existing `except`; that except does not exist.)*
- **`after_agent` cannot see a failure.** A turn that raises mid-invoke never reaches it, which is
  exactly invariant 2's case — the record an operator most wants.
- **`duration_ms` must bracket the HITL resume loop**, which runs in `run_turn` *after*
  `_invoke_resilient` returns, i.e. after the middleware's `after_agent` has already fired.

So: `run_turn` starts the clock, resets the accumulator (belt-and-braces — `before_agent` also
resets it, but a turn that fails before the first model call never reaches that hook), and in
`finally` assembles the record from the accumulator plus its own numbers and appends it.

The in-flight exception decides the record's **`outcome`** (`cli._turn_outcome`), and `failed` is
derived from that as `outcome == "error"` — never set independently. `HaltTurn` is `denied`,
`BudgetExceeded` is `budget`, `KeyboardInterrupt` is `cancelled`, `InterruptAborted` is `aborted`,
and anything else is `error`. *(As first built only `HaltTurn` was excluded from `failed`; the other
three still counted as failures, which invariant 2a's own reasoning forbids. See `milestone6.md`
§0.1 finding 3.)*

`_run_turn_hitl` (the `run_repl` wrapper) stays a pass-through. It must not become a second write
site, or a HITL run records every turn twice.

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
# Module-level, monotonically increasing. Read via blocked_ms(); callers take a
# delta across a turn boundary rather than an absolute.
_TOTAL_BLOCKED_NS = 0

class _InstrumentedLimiter(InMemoryRateLimiter):
    """Same limiter, plus accounting of time spent blocked in acquire()."""
```

`acquire()` (and `aacquire()` if the async path is ever used) times itself and adds to the module
counter. `TelemetryMiddleware` samples `ratelimit.blocked_ms()` in `before_agent` and `after_agent`;
`paced_sleep_ms` is the delta.

**The counter is module-level, not an instance attribute — this is forced, not stylistic.** The
limiter is constructed deep inside `providers.resolve_chat_model` and handed straight to the chat
model as `rate_limiter=`; **nothing gives `cli.py` a reference to that object**, so a per-instance
attribute would be unreachable from the middleware that has to read it. One limiter exists per
process (M1's design), so a module counter and an instance counter carry identical information here;
only one of them is addressable. Store nanoseconds internally and expose milliseconds at the seam,
so repeated small `acquire()` waits don't each floor to zero.

**Three consequences to write down rather than discover:**
- Construction failure must keep degrading to the unpaced string path (`providers.resolve_chat_model`
  already does this) — instrumenting must not turn a soft failure into a hard one.
- With no tier selected there is **no limiter at all** (M1's inert-by-default contract), so
  `paced_sleep_ms` is `0`, not `null`. Zero here means "not paced", which is true.
- The counter is process-global, so a host test must reset it between cases. Expose a
  `reset_blocked()` for tests rather than letting them poke the private name.

## 6. HITL wait (`hitl.run_interrupt_loop`)

Time spent blocked on a human is wall clock inside the turn but is not the harness's or the model's
cost. Measure it in the interrupt loop (the one place that blocks on the channel) and expose it as
`hitl_wait_ms`, then include it in the decomposition:

**Plumbing — an optional observer callback, not a return-value change.** `run_interrupt_loop`
returns the resumed *result*, and `run_turn` uses that return value; widening it to a tuple would
change a signature three call sites and the HITL tests depend on. Instead it takes a new keyword-only
`on_wait: Callable[[int], None] | None = None`, called with the elapsed milliseconds each time the
channel returns an answer. `run_turn` passes `accumulator.add_hitl_wait`; every existing caller
passes nothing and behaves exactly as before. Time the **channel `ask` only** — not the surrounding
audit write or the resume invoke, which are harness cost and belong in the residual.

Same reasoning applies to `_pr_approval`'s session-end gate, which also blocks on a human: it is
**out of scope for `hitl_wait_ms`**, because it happens after the last turn record is written and
inside no turn at all. It lands in `session.json`'s wall clock as unattributed time, which is
correct — it is not part of any turn.

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
    # profile_key=None: an audit surface's off switch is an operator escape hatch,
    # not a standing preference — and persisting it would oblige BOTH launchers to
    # resolve and forward it (see below). Env/.env is the off switch.
    name="telemetry", tier="prespinup", env_var="DEEPAGENTS_TELEMETRY",
    profile_key=None, cast=_to_bool, default=True, label="Telemetry",
),
```

- **`tier="prespinup"`** — read once when the middleware list is built; toggling mid-session would
  leave a half-recorded run, which is worse than either state.
- **No `choices`** — it is a bool; `choices` on a bool would reject `DEEPAGENTS_TELEMETRY=1`
  (M5.1's `test_registry_entries_are_internally_coherent` pins this).
- **No CLI flag.** `parse_args` flags are hand-written, and neither `mask_enabled` nor the other
  removable-contract toggles carry one. `DEEPAGENTS_TELEMETRY=0` in `.env` or the shell is the
  documented off switch, and `.env` already reaches the container via `--env-file`.

**`profile_key=None` — decided, and the reversal from the earlier draft is written out.** That draft
set `profile_key="telemetry"` and justified it as "the `mask_enabled` precedent." `mask_enabled` is
`profile_key=None` (`config.py`), so the precedent says the opposite of what was claimed. Setting it
would also cost two things the milestone gets nothing for:

- `test_prespinup_profile_keys_are_consumed_by_both_launchers` requires **every** persisted
  pre-spinup key to be read by `run-docker.ps1` *and* `run-docker.sh` — so a profile key obliges a
  matching resolve-and-forward block in both launchers, plus a `check-parity` sync point, for a knob
  whose entire job is to be left alone.
- `test_wizard_prespinup_specs_are_the_persisted_prespinup_half` pins the persisted roster by name,
  and a persisted field gets a wizard question, putting "do you want telemetry?" in the security
  wizard's flow. Defaulting on (§7 of the plan) and asking every operator to reconfirm it are
  contradictory postures.

With `profile_key=None` the field still resolves through the same CLI > env > default chain, still
renders in `/config`'s read-only half and `harness config show`, and still validates at every point
of entry. Only the profile-file tier is absent — the same shape `headless` and `mask_enabled` have.

Then `Settings` gains the `telemetry: bool` field **in the same position** — M5.1 invariant 1
asserts `dataclasses.fields(Settings)` equals the registry names *in order*, so a mismatch fails
`test_settings_dataclass_exactly_matches_the_registry` immediately. That test is the guard; do not
work around it. Position it next to `mask_enabled`, i.e. inside the pre-spinup block, and the
persisted-roster test above stays untouched because the new field is not persisted.

Deliberately **not** a bare `os.environ` read like `DEEPAGENTS_ARCHIVE`/`DEEPAGENTS_MASK`: those
predate M5. The registry entry is what buys validation-at-entry and one declaration; the
`profile_key` is a separate question, answered `None` above.

## 8. Independence from the cost tracker

`build_cost_tracker` returns `None` when there is nothing to price — which is the shipped default
(`ollama:gemma4`, `pricing = "free"`) and therefore the local-benchmark case. So:

- `TelemetryMiddleware` parses `usage_metadata` from the model response itself, via the **same**
  `cost.py` helpers the tracker uses — `cost._latest_usage(state)` then `cost._split_tokens(usage)`.
  Import them; do not re-implement token extraction, because two parsers is how the numbers drift.
  *(Both are underscore-private. They are private to discourage reimplementation, not to forbid
  reuse, and `cli.py` already imports from `cost`; importing them is the point of this bullet.)*
- When a tracker *is* present, telemetry must not double-count: it reads the response, the tracker
  reads the response, and they are independent observers of the same event. The session-level check
  (invariant 7, `session.json` vs the `past.sqlite` row) is what catches a divergence.
- `cost_usd` comes from the tracker when present, else `null`. Read it off `tracker.turn.cost` at
  the end of the turn — `CostTrackerMiddleware.before_agent` resets `turn`, so it holds exactly this
  turn's cost by the time `run_turn`'s `finally` runs.

**`input` means fresh input, and this is what makes invariants 6 and 7 checkable.** `_split_tokens`
returns `(fresh_input, output, cache_read, cache_write)` — cache-read tokens are split *out of*
`input`, not left in it. `UsageAccumulator` stores the same split, and `cli._cost_totals_for_row`
writes those accumulator fields to the `past.sqlite` row. So telemetry's `input` equals the archive's
`input_tokens` only if telemetry uses that same split. Use `_split_tokens` verbatim and the
agreement is structural; hand-roll `usage["input_tokens"]` and invariant 7 fails on arithmetic that
looks like a telemetry bug and is really two definitions of one word.

## 9. `session.json` — schema

Written by `cli.main` after `_finalize_session`, before `_pr_approval`.

**Outside the `if archive_conn is not None:` block, not inside it.** In `cli.main` the
`_finalize_session` call is guarded by that condition, so "after `_finalize_session`" read literally
puts the summary write inside the guard — and §13 requires telemetry to keep working under
`DEEPAGENTS_ARCHIVE=0`, where the guard is false and the write would silently never happen. The
ordering constraint (invariant 8) is *after finalize, before `_pr_approval`*; the placement is at
that point in `main`'s body, unguarded, reading `tracker`/`run_id` which both exist regardless of the
archive.

**It is written on the normal-completion path only.** That point in `main` is reached when
`run_repl`/`run_batch` return; a crash out of the REPL unwinds to the `finally`, which runs
`session.end` and closes the archive. So a hard crash leaves the turn records (each already
appended, invariant 1) and **no** `session.json`. That is the deliberate trade: per-turn durability
is what bounds loss, and a summary written from a half-finalized session would disagree with the
`past.sqlite` row, which fork 2 forbids. `harness telemetry show` derives from the records when the
summary is absent, so the run is still readable.

```json
{
  "schema": 1,
  "run_id": "...", "thread_id": "...", "topic": "...",
  "started": "...", "ended": "...", "duration_ms": 412330,
  "turns": 7, "turns_failed": 1,
  "outcomes": {"ok": 5, "budget": 1, "error": 1},
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

**Today `open-pr.sh` passes `--body "<one literal string>"`.** It does *not* use a heredoc or
`--body-file` — converting it is part of this slice, not a fact about the current script. T4 is
therefore two changes: build the body into a temp file via heredoc and switch to `--body-file`
(behaviour-identical when no summary exists), then append the block when one does. The block is
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
- **The invocation needs a working directory, and the script has already left it.** `open-pr.sh`
  starts with `cd "${DEEPAGENTS_WORKSPACE:-.}"`, and the harness package is not installed — it is a
  directory at `/project/harness`. So `python3 -m harness …` from the workspace fails with
  `No module named harness`. Run it as `(cd /project && python3 -m harness telemetry pr-block …)` in
  a subshell, or with `PYTHONPATH=/project`; do not `cd` in the parent shell, since the following
  `gh` call needs the workspace CWD. Prefer the container's harness interpreter explicitly
  (`/opt/venv/bin/python3`) over bare `python3` — the workspace conda env must never be the one that
  imports harness code (the two-stack rule).
- Any failure — missing file, bad JSON, non-zero exit, missing interpreter — leaves the body at the
  hardcoded text and exits 0 (invariant 13). The `|| true` goes around the block generation, not
  around the whole PR creation.

## 11. `harness telemetry` subcommand (T5)

Routed in `dispatch` next to `doctor`/`config`, importing `harness.telemetry` lazily so the route
adds no dependency of its own (invariant 22):

```
harness telemetry show [--run <run_id>] [--state-dir <path>]   # default: most recent run
harness telemetry list [--topic <label>] [--limit N]           # one line per run
harness telemetry pr-block [--run <run_id>]                    # stdout, used by open-pr.sh
```

Output shape follows `harness past show` (label-aligned key/value lines), so the two read alike.

**`show` falls back to the records when `session.json` is absent**, deriving the same summary in
memory (§9: a crashed run has records and no summary). It says which source it used, because
"derived from 6 turn records, session not finalized" and "read from session.json" are different
claims about the same numbers.

**What "keyless" does and does not mean here — state it, do not inherit the claim.**
When this spec was written, `harness/__init__.py` did `from harness.cli import main`
**unconditionally**, so *any* `python3 -m harness <subcommand>` already imported `cli.py` and with it
langchain/langgraph/deepagents — `config` and `doctor` included. So the property the milestone could
actually hold was the narrower one: **`telemetry` needs no API key, no network, and no model, and its
route adds nothing to the import cost that `config`/`doctor` do not already pay.**

> **Superseded during the merge to `main`.** M5 §0.1 F6 landed first (PR #44): a lazy
> `__init__.__getattr__` plus the stdlib-only `harness/entry.py`. The `telemetry` route therefore
> moved from `cli.dispatch` (which no longer exists — `cli.dispatch` is now a re-export of
> `entry.dispatch`) into `entry.py`, and the **strong** claim now holds: `harness telemetry` loads no
> runtime stack at all. Invariant 22 is restated to the strong form and
> `tests/test_import_isolation.py` pins it. The paragraph above is kept rather than rewritten because
> the reasoning it records — do not write "stays keyless" in a way that implies a stdlib-only import
> the code does not deliver — is what made the claim safe to strengthen only once the code caught up.

## 12. Headless join (T5)

`cli._batch_payload` gains three keys — `run_id`, `topic`, `usage_log` — alongside the existing
`thread_id`. `run_id` already exists unconditionally in `cli.main`
(`run-{ts}-{uuid4[:6]}`, created whether or not the archive is enabled), so this is plumbing, not a
new identifier. Existing keys keep their names and meanings; this is additive.

## 13. Failure paths

| Case | Behaviour |
|---|---|
| Turn raises | record written with `failed: true` and whatever partial numbers exist, from `run_turn`'s **`finally`** (§3.2 — it has no general `except`, and its two callers each have their own) — before the error propagates to the caller that reports it |
| Turn denied by the HITL gate (`HaltTurn`) | record written, `failed: false`. An operator deny is an outcome, not a failure; `tool_errors` and the tool counts still show what ran |
| Sink unwritable | one stderr `[harness] telemetry: <reason>` per run (not per turn), then silently skip. Never raises into the turn (invariant 3) |
| Session summary write fails | stderr note; the run still ends normally and git-pr still runs with the plain body |
| Run crashes before the summary | turn records survive (each appended at its own turn); no `session.json`, and `telemetry show` derives from the records instead (§9, §11) |
| Archive disabled (`DEEPAGENTS_ARCHIVE=0`) | telemetry still records — `run_id` is independent of the archive, and the summary write sits outside the `archive_conn` guard (§9). Invariant 7's cross-check is skipped, not failed, when there is no row to compare against |
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
milestone exists to prevent. The probe cannot run on the dev host: langchain is not installed there,
so it runs in the `deepagent-harness-test` image (the bind-mount dev loop in
`deepagent-image/CLAUDE.md` is enough — no rebuild).

### 15.1 Signatures this milestone changes

Listed up front because they are the milestone's whole blast radius outside the two new modules, and
because a reviewer should be able to check the list rather than diff for it:

| Symbol | Change |
|---|---|
| `cli.run_turn` | + `telemetry=None` param; body gains `try`/`finally` (§3.2) |
| `cli.run_repl`, `cli.run_batch` | + `telemetry=None` param, passed through |
| `cli._invoke_resilient` | + returns/records retry sleep and trim flag (§4) |
| `cli._batch_payload` | + `run_id`, `topic`, `usage_log` keys (§12) |
| `cli.dispatch` | + `telemetry` route (§11) |
| `hitl.run_interrupt_loop` | + keyword-only `on_wait=None` (§6) |
| `ratelimit` | + module counter, `blocked_ms()`, `reset_blocked()`, `_InstrumentedLimiter` (§5) |
| `audit` | scrub functions move out to `harness.scrub`, re-exported (§1) |
| `config.FIELD_SPECS`, `config.Settings` | + one `telemetry` field each, same position (§7) |
| `workflows/git-pr/open-pr.sh` | `--body` → heredoc + `--body-file`, then the block (§10) |

Every added parameter defaults to the inert value, so the removable contract (invariant 20) is
structural rather than something the tests have to police.
