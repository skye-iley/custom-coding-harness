# Milestone 8 — Benchmark Ladder, Tier 1 (Gold Set)

> **Status:** 📋 Planned — docs only, no code. `design_doc.md` §11 "Automated Benchmarking Suite"
> is the parent entry; this doc is the concrete tier-1 slice and wins over it for *what we build
> next*.
>
> **Scope in one line:** make the harness runnable over a pinned set of coding tasks, unattended,
> on a free local model, and make each run emit a scorable patch plus a joinable telemetry row —
> so "did a harness change break the loop" becomes a measurement instead of an anecdote.

---

## 0. Why this, why now

The three commits before this milestone were all model-routing defects, each of which silently
degraded **every run**, and none of which any of ~1000 passing tests caught:

| Defect | Effect |
|---|---|
| `num_ctx` defaulted to 2048 | prompt truncated from the left; the model received 3 of 11 tool schemas |
| `message.thinking` discarded | blank turns — tokens billed, nothing rendered |
| `reasoning = false` | agentic tool calling collapsed into prose |

Each was found by accident. One was found by an operator saying *"before, it could handle this
fine."* That sentence is the bug report this milestone replaces with a number.

The harness has no standing measurement of whether it still works end to end. It has a large
hermetic suite that proves the harness is internally consistent, and a `live_model` tier that
proves a single turn round-trips. Neither answers *can this thing still fix a bug*.

**It is also the validation M6 was built for and has never received.** `milestone6.md` §5b names
benchmark-grade per-run attribution as telemetry's *primary purpose* — the wall-clock
decomposition, the per-tool call counts, `run_id`/`topic`/`usage_log` in the headless JSON — and
that decomposition has never once been joined to an actual sweep. Telemetry nobody has consumed is
telemetry nobody has validated: a field that is always `null`, a residual that is always 80% of
wall clock, a `run_id` that does not actually join — all read green today.

Chosen now because it is **free** (local Ollama is already the default provider, `pricing = "free"`,
no key, no quota), **host-runnable**, and **independent of the trust-boundary chain**, so it cannot
stall on an unmeasured LSM the way anything touching the jail can.

---

## 1. Goal & Definition of Done

**Goal.** One command runs the harness over a pinned dataset of N bug-fix tasks and produces two
files: a predictions jsonl an *official* scorer can consume, and a per-instance run ledger that
joins to M6 telemetry.

**Done when all of the following hold:**

1. `harness bench run <dataset.jsonl>` iterates every instance, runs the harness headless against a
   prepared workspace, and writes `predictions.jsonl` + `runs.jsonl`. Interrupting it and re-running
   resumes rather than restarting.
2. Every prediction line is `{"instance_id", "model_name_or_path", "model_patch"}` and nothing else;
   `model_patch` is a `git diff` that applies cleanly to a fresh checkout of the instance's base and
   contains **no** harness artifact (`.deepagents/`, `.agent_telemetry/`, `.conda/`, `.harness-*`).
3. A newly created file the agent never `git add`ed appears in the patch. (This is not automatic —
   see §5.2.)
4. No instance can run unbounded: a stuck agent on a free model is stopped by a **step** bound and a
   **wall-clock** bound, and the stop is recorded as its own outcome, distinguishable in the ledger
   from a genuine error and from a cost cap.
5. Each `runs.jsonl` row carries `instance_id`, `run_id`, `usage_log`, `exit_code`, `outcome`, and
   the wall-clock decomposition, joined from the headless JSON and `<state-dir>/usage.jsonl` — i.e.
   the M6 §5b join is **exercised**, not asserted.
6. `harness bench` is keyless in the strong sense: no API key, no network, no model, **no runtime
   stack** (`tests/test_import_isolation.py` covers it alongside `entry`/`doctor`/`telemetry`).
7. A gold set of ≥5 instances is committed, each with a deterministic pass/fail check, and a
   documented "here is what a green sweep looks like on this machine" baseline exists.
8. The removable contract in §10 holds.

**Explicitly not done-when:** a score. See §9.

---

## 2. What exists today — the honest inventory

Not a from-scratch build. Four of the five pieces are already shipped:

| Need | State today | Gap |
|---|---|---|
| Run one task unattended, one JSON on stdout | **`cli.run_batch`** (`--headless`, M3 P2) | none |
| Per-instance workspace isolation | **`EPHEMERAL=1`** — run-docker mounts a throwaway copy, reverts on close | none; §5.3 uses it as-is |
| Join a run to its measurements | **`_batch_payload`** carries `run_id` / `topic` / `usage_log` (M6) | never exercised by a real sweep |
| Per-instance resource ceiling | `--cpus` / `--memory` / `--pids-limit` (M1) | bounds the *host*, not the *loop* |
| Per-instance **loop** ceiling | `--max-cost` / `--max-tokens` (M1) | **cost never accrues on a free model** — the caps are inert exactly where the benchmark runs |
| Scorable patch out | **nothing** — the git lifecycle produces a branch→commit→PR | §5.2 |
| Batch driver | **nothing** | §5.3 |

So the milestone is three slices, not seven.

---

## 3. The bound that does not exist — and why `--max-turns` alone is the wrong one

`design_doc.md` §11 names the gap as *"`--max-turns` + session wall-clock"*. **Half of that is
wrong, and getting it wrong would ship a bound that bounds nothing.**

In headless mode a benchmark instance is **exactly one turn**. `run_batch` iterates `tasks`, and a
sweep passes one task per instance. A runaway is not many turns — it is one turn whose ReAct loop
does not terminate: model → tool → model → tool, forever, inside a single `agent.invoke`. A
turn-count cap is checked between turns and is therefore never reached.

The only thing that bounds that loop today is **LangGraph's `recursion_limit`**, and three things
are true about it, all bad:

- The harness **never sets it**. `grep -rn recursion_limit project/ --include='*.py'` returns
  nothing; every run takes the installed LangGraph's default.
- **That default is not a bound.** It is
  `int(getenv("LANGGRAPH_DEFAULT_RECURSION_LIMIT", "10007"))` — `langgraph/_internal/_config.py:32`
  on the pinned version. Ten thousand super-steps against a free local model is unbounded in every
  sense that matters: no cost accrues, nothing else is watching, and the instance runs until an
  operator kills it or the disk fills with trace. (Do not carry the widely-quoted **25** into this
  design — that was LangGraph's older default and it is not what this harness runs on. Re-read the
  constant at build time rather than trusting either number, including this one.)
- When it *does* trip, `GraphRecursionError` falls through `cli._turn_outcome`'s final `return
  OUTCOME_ERROR`. **A truncated instance is recorded identically to a crashed one.** A sweep's
  `turns_failed` would therefore mix "the harness broke" with "the harness ran out of rope", the
  exact conflation M6 invariant 2a exists to prevent — this is simply a case M6 could not have known
  about.

Note the consequence for §12's first open question: because the inherited default is effectively
infinite rather than merely low, "leave it unset by default" is a real choice about the *harness*
(unchanged behaviour, removable contract intact) and **not** a safe choice for a *sweep*. The driver
must always pass one.

So the slice is **three** knobs and one classifier change:

| Knob | Bounds | Seam |
|---|---|---|
| `--max-steps` | the ReAct loop inside one turn | `config["recursion_limit"]`, set in `cli.main` |
| `--max-seconds` | wall clock for the whole session | checked at a step boundary, §5.1 |
| `--max-turns` | number of turns in a session | `run_repl` / `run_batch` loop counter |

`--max-turns` is kept — it is genuinely useful outside benchmarking (an interactive session that
should not run away overnight, a multi-task headless invocation), and it is the cheapest of the
three. It is simply **not** the benchmark bound, and the doc must not imply it is.

---

## 4. Scope (slices, in build order)

| Slice | What | Depends on |
|---|---|---|
| **B1** | Hard stops: `--max-steps`, `--max-seconds`, `--max-turns`, `OUTCOME_STOPPED` + `stop_reason` | — |
| **B2** | `--emit-patch`: `model_patch` in the headless JSON, exclusions, intent-to-add | — |
| **B3** | `harness/bench/`: dataset format, driver, `predictions.jsonl` + `runs.jsonl`, resume | B1, B2 |
| **B4** | The gold set itself: ≥5 pinned instances + their checks | B3 |
| **B5** | Docs + baseline record: what a green sweep looks like, and on what hardware | B4 |

B1 and B2 are independently useful and independently shippable — B1 in particular closes a real
hole (today a runaway free-model turn has no bound the operator chose). B1 is the **smallest useful
slice** if this milestone is ever cut short.

---

## 5. Implementation

### 5.1 B1 — the hard stops

**`--max-steps` (`DEEPAGENTS_MAX_STEPS`, default: unset ⇒ LangGraph's default).**
One line where the graph config is built: `config["recursion_limit"] = settings.max_steps`. That is
the whole mechanism — LangGraph already counts super-steps and raises `GraphRecursionError`. The
work is not the plumbing, it is (a) making the bound an explicit number the operator chose rather
than an inherited five-digit constant, and (b) classifying the exception.

**`--max-seconds` (`DEEPAGENTS_MAX_SECONDS`).** A *session* deadline, not a per-turn one — an
instance is one turn, and a per-turn timer would need a thread. Checked at a **step boundary**, in
`TelemetryMiddleware`-adjacent territory: the cheapest correct seam is `before_model`, which fires
once per model call, is already a hook the harness owns, and cannot leave a tool call half-executed.
Raises `DeadlineExceeded`.

> **Why not a signal/thread timeout.** `SIGALRM` does not exist on Windows and the host tier must
> stay cross-platform; a watchdog thread cannot interrupt a blocking HTTP read without cancelling
> mid-write. A step-boundary check overshoots by at most one model call, which for a benchmark bound
> is exactly the right trade — the number that matters is "this instance did not run for an hour",
> not "this instance stopped at 600.000s".

**`--max-turns` (`DEEPAGENTS_MAX_TURNS`).** A counter in the REPL loop and in `run_batch`, checked
the same place `BudgetExceeded` already ends a session, so it inherits the deterministic-exit
behaviour M1 established.

**The classifier.** `telemetry.OUTCOMES` gains **`OUTCOME_STOPPED = "stopped"`**, and
`cli._turn_outcome` gains three lines mapping `GraphRecursionError` / `DeadlineExceeded` /
`TurnLimitExceeded` to it. The record gains **`stop_reason`** (`"steps"` / `"seconds"` / `"turns"` /
`null`) so the three are distinguishable without three outcomes.

Why a new outcome rather than reusing `OUTCOME_BUDGET`: a sweep must be able to ask *how many
instances did the harness fail to finish, and why*. Folding a clock stop into `budget` says "the
operator's cap fired" about an event that is closer to "the agent did not converge" — and the two
lead to opposite actions (raise the cap vs. fix the loop). Folding it into `error` is the bug
described in §3.

`TelemetryRecord.to_dict` already degrades an unrecognised outcome to `error` (`telemetry.py:210`),
so an old reader meeting a new record fails safe. `schema` stays at `1`: `outcome` was always
declared as an enum that could grow, and `stop_reason` is additive and nullable.

### 5.2 B2 — `--emit-patch`

**Where it lives.** The harness emits the patch into the headless JSON; the *driver* maps that to
the predictions schema. The harness is the only thing that knows the state dir, the exclusions and
the workspace root; the driver is the only thing that knows `instance_id` and the run's identity.
Splitting there keeps one machine contract (the headless JSON) rather than two.

**Payload.** `_batch_payload` gains `model_patch`, **present as `null` when `--emit-patch` is off**
— the same convention M6 used for `run_id`/`topic`/`usage_log`, and for the same reason: a driver
reading `payload["model_patch"]` should get a testable null, not a `KeyError` that looks like a
schema change.

**The diff.**

```
git -C <workspace> add -A -N -- .           # intent-to-add: see below
git -C <workspace> diff --no-color --no-ext-diff --binary <base> -- . \
    ':(exclude).deepagents' ':(exclude).agent_telemetry' ':(exclude).conda' \
    ':(exclude).harness-config.yaml' ':(exclude).harness-profile.yaml'
```

Four things here are load-bearing:

- **`add -A -N` (intent-to-add) is not optional.** `git diff` shows nothing for an untracked file.
  An agent that fixes a bug by adding a new module produces an **empty patch** and scores zero, and
  the failure is completely silent — it looks like a model that did nothing. Done-when #3 exists
  specifically to pin this.
- **Exclusions are pathspec-based, not post-hoc text filtering.** Editing a unified diff after the
  fact to drop a file is how you produce a patch that does not apply.
- **`--no-ext-diff`** because a workspace `.gitconfig` with an external differ would otherwise emit
  something that is not a patch at all.
- **The base is the recorded base**, not `HEAD`. Which matters because of the next paragraph.

**The git lifecycle must be off.** `git-branch` (session.start) and `git-pr` (session.end) create a
branch and *commit* — after which `git diff HEAD` is empty and the patch is silently lost. Bench
runs therefore point `DEEPAGENTS_WORKFLOWS_DIR` at an empty directory. This is a **driver-side**
decision (§5.3), not a new harness flag: the workflows dir override already exists, and adding a
`--no-git-lifecycle` flag would be a second way to express the same thing.

> Consequence worth stating plainly: the patch is computed **before** `session.end`, from
> `run_batch`, while the working tree is still dirty. That ordering is the point.

### 5.3 B3 — the batch driver (`harness/bench/`)

**Where it runs.** On the **host**, driving `scripts/run-docker.{sh,ps1}` once per instance. Not
inside the container. Three reasons: each instance must get a clean container (that *is* the
isolation); the whole security posture — mask, state dir, netjail, caps — comes along for free
rather than being re-implemented; and it keeps the driver keyless and stdlib-only, so it routes
through `entry.dispatch` like every other admin command and is covered by the import-isolation
guard.

**Route.** `entry.dispatch` gains `bench` → `harness.bench.bench_main`, function-local import, same
shape as `telemetry` / `doctor` / `config`.

```
harness bench run <dataset.jsonl> [--out DIR] [--limit N] [--only ID,ID] [--dry-run]
harness bench show [--out DIR]                 # summarize a completed sweep
```

**Dataset format** — jsonl, one object per line:

```json
{
  "instance_id": "gold-001-off-by-one",
  "workspace": "benchmarks/gold/001",
  "base_commit": "HEAD",
  "task_prompt": "The paginator returns one row too many on the last page. Fix it.",
  "fail_to_pass": ["pytest tests/test_paginate.py::test_last_page"],
  "pass_to_pass": ["pytest tests/test_paginate.py"]
}
```

Tier 1 uses a **local directory**, not clone-at-commit — that is what makes it free and offline.
`repo` + a real `base_commit` are reserved for tier 3 and parse as optional today; the field is
present now so the format does not change under tier 2/3.

**Per-instance loop:**

1. Skip if `instance_id` is already in `predictions.jsonl` (resume; the file is append-only).
2. Launch `run-docker` with `EPHEMERAL=1` (throwaway copy of the instance dir — already shipped,
   already excludes `.conda`), `--headless`, `--emit-patch`, `--topic <instance_id>`, the §5.1
   bounds, and `DEEPAGENTS_WORKFLOWS_DIR` pointed at an empty dir.
3. Read the single JSON line from stdout. Anything on stderr is stage markers and stays there.
4. Append one line to `predictions.jsonl` and one to `runs.jsonl`. **Flush both after every
   instance** — a sweep that dies at instance 40 of 50 must not lose 39.
5. Continue on failure. An instance that crashes gets a prediction with an empty `model_patch` and a
   ledger row carrying the outcome; it never aborts the sweep.

**Outputs.** `predictions.jsonl` is exactly the three official keys and nothing else — anything
extra risks a scorer rejecting it. Everything the harness knows goes in `runs.jsonl`:
`instance_id`, `run_id`, `thread_id`, `usage_log`, `exit_code`, `outcome`, `stop_reason`,
`duration_ms` and its decomposition, `tool_calls` by name, `tokens`, `cost_usd`, `model`, plus the
sweep's own `started_at`/`ended_at`.

**Serial, deliberately.** One local Ollama daemon, one GPU: parallel instances contend for the same
weights and make `model_ms` meaningless. `--jobs` is a §9 non-goal, not an oversight — and it is
also why `run_turn` already threads `telemetry` as a parameter rather than a module global
(`cli.py:972–975`), so the seam is not foreclosed.

### 5.4 B4 — the gold set

≥5 instances, committed under `benchmarks/gold/`. Each is a small self-contained project with a
seeded bug, a failing test that pins it, and a passing test suite around it. Criteria:

- **Deterministic.** No network, no clock, no randomness. The check is a `pytest` exit code.
- **Small.** A tier-1 instance should be solvable in well under the step bound, or it is measuring
  the bound rather than the harness.
- **Varied in failure mode**, not in domain — the point is to exercise the *loop*: one that needs
  reading a file it was not pointed at, one that needs running the tests to see the failure, one
  that needs editing two files, one whose obvious fix breaks `pass_to_pass`, one that needs creating
  a new file (which is also done-when #3's live case).

The instances are fixtures, not tests: `pytest tests/` must not run them.

### 5.5 B5 — the baseline record

A committed record of what a green sweep looked like: date, host, model + tag, `num_ctx`, the
resolved bounds, and the per-instance outcome table. Same discipline as
`milestone4_manual_verification.md` — evidence, not guidance. Without it, the first regression has
nothing to be a regression *from*.

---

## 6. The M6 join — the part that is actually a test of telemetry

`runs.jsonl` is built by joining the headless JSON's `run_id` against `<state-dir>/usage.jsonl`.
That join is the first real consumer of M6 §5b, and it is expected to surface problems, because
several of M6's fields have never been read by anything:

- `run_id` must actually be the key. `thread_id` repeats across resumes and is explicitly *not* it
  (`cli.py:2092–2097`) — if the join is written against `thread_id` because it is the older, more
  obvious field, a sweep will silently merge two instances.
- `cost_usd` is `null`, never `0.0`, on the free local model — the benchmark's own default case. A
  driver that sums it must not treat `null` as zero and report a $0 sweep as a priced one.
- The **residual** (`duration_ms` minus the measured components) is the number to watch. M6
  invariant 4a exists to catch a blocking call vanishing into "overhead"; a sweep of 50 instances is
  the first dataset large enough for a systematic residual to be visible rather than noise.
- Under `--max-steps`, the run ends *inside* the graph. The telemetry record is written from
  `run_turn`'s `finally` (`cli.py:977–985`), so it should exist — but "should" is doing work there,
  and this is the first stop path M6 never saw.

**If the join does not work, that is the milestone finding something**, not a blocker. Record it the
way M6 §0.1 recorded the probe that reversed its own planned fix.

---

## 7. Anti-cheat posture (tier 1)

Tier 1 runs against **local directories with no upstream**, so there is no fix to fetch and the
question is mostly moot. It is still worth stating the posture now, because tier 3 inherits it:
the solve runs under **NetJail with a minimal allowlist** (package registry only), so the agent
cannot pull the upstream patch from GitHub. `design_doc.md` §11 already resolved this fork; tier 1
does not need it, and tier 1 must not *contradict* it — so the driver accepts `NET_JAIL=1` and
passes it through, rather than assuming egress.

The local model is also worth naming as a posture, not just a cost decision: a cloud model that has
memorised a public benchmark's fixes is a contamination problem a local Ollama tag mostly does not
have. Mostly. Do not write that down as a guarantee.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| **The gold set measures the model, not the harness.** A weak local model fails everything, and every harness change reads as "still 0/5". | Instances sized to be solvable by the shipped default (`ollama:gemma4`) — verified in B4, not assumed. A set where the baseline is 0 is a broken set, and B5 is what makes that visible on day one. |
| **The baseline drifts because the model changed, and gets read as a harness regression.** | `runs.jsonl` records the model + tag per instance; B5's record pins what the baseline was measured against. A model change is a re-baseline, and must be recorded as one. |
| **The patch is empty and nobody notices.** New-file case, `.conda` noise, a lifecycle commit swallowing the diff. | Done-when #2/#3; the driver counts empty patches and `bench show` reports them prominently. An all-empty sweep must be loud, not a row of zeros. |
| **The bound is set so low every instance stops.** | `stop_reason` is per-instance and `bench show` aggregates it. A sweep that is mostly `stopped/steps` is reporting the bound, not the harness. |
| **`--emit-patch` becomes a second way to get code out of the container.** | It is a diff of the workspace the agent already owns, on the headless JSON channel, under an explicit flag. It reads nothing outside the workspace and adds no new path. Worth re-checking against M4 §19 at build time rather than asserting here. |
| **Scope creep into scoring.** | §9. |

---

## 9. Non-goals

- **Scoring.** No bespoke scorer, ever. The contract this milestone satisfies is the predictions
  jsonl; correctness comes from the benchmark's own evaluation harness (SWE-bench eval, the Aider
  runner). A scorer written here would be a number nobody else can compare against.
- **Tiers 2 and 3.** Aider polyglot and SWE-bench Lite/Verified are follow-on milestones. They need
  only the driver + patch output + bounds this milestone builds; the per-instance Docker images and
  the network posture are *their* open questions (`design_doc.md` §11 — including the unpinned
  detail of whether `/opt/venv` can ride inside a SWE-bench instance image without colliding with
  its interpreter).
- **Parallel instances** (`--jobs`). §5.3.
- **A CI gate on the sweep.** Tier 1 is a regression signal a human runs; wiring it into CI needs a
  runner with a model on it, which is a separate problem. Do not gate CI on something that can be
  red because a daemon was down.
- **Auto-baselining.** No "compare against last run and fail on delta" logic in v1. Two sweeps and a
  human reading them is the whole workflow until there is enough history to know what normal drift
  looks like.

---

## 10. Removable contract

Delete `harness/bench/`, its `entry.dispatch` route, the four new flags + their `FieldSpec` entries,
`OUTCOME_STOPPED` + `stop_reason`, the `_turn_outcome` lines, and the `model_patch` key — and the
harness is byte-for-byte Milestone 7. Specifically:

- No flag passed ⇒ `recursion_limit` is not set in the graph config at all (LangGraph's default), no
  deadline is checked, no turn counter is compared. Not "set to infinity" — **absent**, so the
  pass-through is structural rather than arithmetic (M7 invariant 18's lesson).
- `--emit-patch` off ⇒ no `git` subprocess runs, and `model_patch` is `null`.
- `benchmarks/` is fixture data with no importer; deleting it breaks nothing.

---

## 11. Test plan

- **`tests/test_bench.py` (host tier, stdlib only)** — dataset parsing incl. malformed lines,
  resume-skip logic against an existing `predictions.jsonl`, the predictions record carrying exactly
  three keys, the join from a synthetic headless payload + a synthetic `usage.jsonl` to a
  `runs.jsonl` row, and the empty-patch counter. The subprocess launch is injected, not run.
- **`tests/test_cli.py` (image tier)** — each bound fires and produces `stopped` with the right
  `stop_reason`; the headless JSON carries `model_patch: null` without the flag; `--max-steps`
  reaches `config["recursion_limit"]`.
- **`tests/test_telemetry.py`** — `stopped` round-trips, an unknown outcome still degrades to
  `error`, `outcomes` still sums to `turns`.
- **`tests/test_config.py`** — the four new `FieldSpec` entries resolve through all four tiers and
  pair with `cli._LIVE_APPLIERS` both ways (M5.1 invariant 7). `max_turns`/`max_seconds` are live;
  `max_steps` is live but takes effect on the next turn, not mid-turn.
- **`tests/test_import_isolation.py`** — `harness.bench` imports without pulling `cli`/`agent`/
  deepagents/dotenv.
- **`tests/test_live_model.py` (`live_model` marker)** — the tier that a stub cannot substitute for,
  and the one this milestone most depends on: a real local-model turn against a real seeded-bug
  fixture, asserting the emitted patch is non-empty, applies cleanly with `git apply --check` to a
  fresh copy of the base, and contains no excluded path. A stub returns whatever the test wrote; it
  cannot tell a patch that applies from one that does not.

> **The M7 §0.2 lesson applies directly here.** A substring assertion against a serialised blob
> cannot tell a correct artifact from a plausible-looking one — M7's system prompt passed every stub
> because a prompt is a substring of its own `repr`. The equivalent trap here is asserting
> `"def paginate" in model_patch` and calling it a valid diff. **Assert by applying it**
> (`git apply --check`), not by reading it.

---

## 12. Open questions to pin at build start

1. **`--max-steps` default.** Explicit number, or leave LangGraph's 25 as the default and only set
   it when the flag is passed? Leaning: leave unset by default (removable contract, §10) but have
   the driver always pass one, so a benchmark run is never on an inherited default nobody chose.
2. **Where the deadline check lives.** `before_model` on a new tiny middleware, or folded into
   `TelemetryMiddleware`? Folding is cheaper; a separate middleware keeps M6 removable
   independently. Leaning separate.
3. **Whether `bench` belongs in `harness/` at all**, given it drives `docker` from the host and
   every other `harness` subcommand reads local state. The alternative is `scripts/bench.py`.
   Leaning `harness/bench/`: it wants the same argv grammar, the same keyless guarantee, and the
   same test tier — and `scripts/` would need a `.ps1`/`.sh` pair under the parity rule, for a thing
   that is pure Python.
4. **Windows.** `run-docker.ps1` and `run-docker.sh` both exist; the driver picks one. Whether the
   driver is cross-platform in v1 or Linux/WSL-first is a scope call, not a design one.
