# Milestone 8 — Benchmark Ladder, Tier 1 (Gold Set)

> **Status:** 🚧 In progress — **B1–B5 all built** on `feat/milestone8-bench-ladder`. §0.1 records
> what the build changed about the plan; `milestone8_baseline.md` beside this file is the B5
> evidence record (4/5 resolved, and the three defects the first sweeps found).
> `design_doc.md` §11 "Automated Benchmarking Suite" is the parent entry; this doc is the concrete
> tier-1 slice and wins over it for *what we build next*.
>
> §12 records the four design forks as **resolved**, and §13 the six assumptions checked against the
> code rather than inferred — one of which (`.git` surviving the ephemeral copy) the whole B2/B3
> design would have been unbuildable without. **All six are closed**, the last by measurement rather
> than reading (§13 item 1: PowerShell binds `--`-prefixed flags into `$TaskParts` cleanly, and the
> `--%` fallback an earlier draft named would itself have been the bug).
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

## 0.1 Build status — what shipped, and what the build changed

| Slice | State |
|---|---|
| **B1** hard stops (`--max-steps` / `--max-seconds` / `--max-turns`, `OUTCOME_STOPPED` + `stop_reason`) | ✅ **built** |
| **B2** `--emit-patch` + `harness/bench/patch.py` | ✅ **built** |
| **B3** `harness/bench/` driver, `predictions.jsonl` + `runs.jsonl`, resume | ✅ **built** |
| **B4** the gold set (5 instances, self-checking) | ✅ **built** |
| **B5** baseline record | ✅ **built** — `milestone8_baseline.md` |

Suite after B5: **1159 passed / 14 skips**, live-model tier included. The gold set is exercised
end to end by a real sweep, not only by the suite — see `milestone8_baseline.md`.

### What B1 changed about the plan

1. **A fourth artefact the plan did not name: `resilience.retry_call` gained an
   injectable `retryable=` predicate, because the step bound was silently
   retryable.** `is_retryable` falls back to scanning an error's message for an
   embedded status code (`Error code: 503`), which is right for a provider error
   and wrong for an exception that merely *contains* a 4xx/5xx-shaped number. A
   perfectly ordinary `--max-steps 500` makes LangGraph say *"Recursion limit of
   500 reached"*, the scan reads the 500 as a server error, and the harness
   retries a graph guaranteed to hit the same wall — one stop becomes four, each
   burning a full loop. `_invoke_resilient` now passes a predicate that refuses
   to retry a bound stop. The knowledge stays at the seam that has it;
   `resilience.py` still imports no sibling. Reachable by any operator who picks
   a round number between 400 and 599 for a bound, which is most of them.

2. **`--max-steps` bounds a *turn*; the other two bound the *session*.** §5.1
   treated the three as one family. They are not: a clock and a turn counter are
   properties of the whole run, so crossing one ends the session (the same
   deterministic close `BudgetExceeded` already gets), while a step bound is per
   invoke and an interactive REPL should drop back to the prompt and let the
   operator decide. All three are still `stopped` in the ledger — only the
   session's fate differs.

3. **A stopped headless run needed its own exit code**, which §5.1 did not call
   for. The plan gave a sweep `outcome`/`stop_reason` in the ledger and left
   `exit_code` at 1, i.e. exactly the "truncated looks like crashed" conflation
   §3 exists to remove, moved from the ledger to the process status.
   `limits.EXIT_STOPPED = 43`, next to `interrupt.EXIT_INTERRUPT_ABORT = 42`.

4. **`DeadlineMiddleware` must be appended *before* telemetry, not after.**
   langchain composes first-is-outermost, so an inner deadline raises after
   `TelemetryMiddleware.before_model` has already opened a model span — and
   `build_record` closes any open span, so every deadline stop would report a
   phantom `model_calls: 1` that never reached a provider. The same
   position-is-load-bearing lesson M7 §5 learned one layer out.

5. **Both bound checks live inside `run_turn`'s `try`**, so a stop is classified
   by `_turn_outcome` and written by the same `finally` as every other turn.
   §5.1's "checked the same place `BudgetExceeded` already ends a session" read
   naturally as the callers' loops, which would have produced a stop that ends a
   session and leaves nothing in the ledger. Consequence worth stating: with
   `--max-turns K` the ledger holds `K` real turns plus one zero-work `stopped`
   record — the refusal of the `K+1`th — because the refusal is the event.

6. **§3's re-read-the-constant instruction paid off, in the boring direction.** Measured on the pinned version: `DEFAULT_RECURSION_LIMIT` is
   `10007`, as §3 says, and `GraphRecursionError` subclasses
   `RecursionError`/`RuntimeError` (so it reaches the generic `except Exception`
   handlers rather than needing a `BaseException` clause). The live-model tier
   pins the class *name*, since `limits.py` is stdlib-only and matches by name.

7. **`stop_reasons` was added to the derived session summary**, unplanned but
   free: it is a fold over the records like every other summary field
   (M6 invariant 6), and without it `harness telemetry show` could say a run was
   `stopped` without saying by what.

### What B2 changed about the plan

8. **`--emit-patch` refuses to start on a workspace with no base commit**, which
   §5.2 did not specify. Reporting `model_patch: null` instead would have made
   "impossible here" indistinguishable from "you did not ask for one" — the same
   point-of-entry principle M5.1 applied to enum knobs, and the same
   null-means-two-things trap M6 avoided by making the join keys present-and-null
   rather than absent.

9. **The scratch index has to live outside the workspace, and that was measured
   rather than reasoned.** A first pass put `GIT_INDEX_FILE` inside the tree;
   `git add -A -N -- .` promptly swept the index file itself into the diff it was
   being used to compute. It is now a `tempfile.TemporaryDirectory`, and a test
   pins it.

10. **The scratch index is seeded with `read-tree <base>`, not copied from the
    real index.** Copying would have been the obvious way to preserve tracked
    state, but it makes the patch depend on whatever the *agent* staged — an
    agent that ran `git add` or `git rm --cached` mid-run could move the result.
    Seeding from the base tree makes the patch describe base → worktree and
    nothing else, and it is also what keeps extraction read-only with respect to
    the operator's repo.

11. **A non-UTF-8 diff raises rather than being decoded lossily.**
    `errors="replace"` would hand a scorer a patch that looks fine and does not
    apply — an instance that scores zero and reads as a weak model, which is the
    exact confusion this milestone exists to remove.

12. **`tests/_bootstrap._load` had to learn sub-packages.** It resolved
    `harness.<name>` to `harness/<name>.py` by taking the last dotted component,
    so `harness.bench.patch` would have loaded `harness/patch.py`. It now walks
    intermediate directories, registering each as a bare package object for the
    same reason it fakes `harness` itself: running a real `__init__.py` is what
    would pull whatever it imports. Bare names (`_load("seccomp")`) still work.


### What B3–B5 changed about the plan

13. **Two defects came out of *running* it, neither visible to review.** They are
    the milestone doing its job on its first day, and both are recorded in
    `milestone8_baseline.md` §4:

    * **`run-docker.ps1` swallowed every container exit code.** It ended with
      `& docker @dockerArgs` inside a `try/finally` and never re-raised
      `$LASTEXITCODE`, so the script always exited 0. On the first sweep, four
      instances the step bound had stopped — harness exit 43 — reached the driver
      as a clean 0. `run-docker.sh` has always ended `exit $?`; the pair had
      drifted and nothing caught it, because until B3 no consumer had ever read
      the exit code. `runs.jsonl` now records **both** `exit_code` (the process)
      and `harness_exit_code` (the payload), which is what made the drift visible
      and is what keeps it visible.
    * **Three harness files were in every prediction.** `run-docker`'s
      `seed_workspace` writes `environment.yml`, `.gitignore` and
      `scripts/run-in-env.sh` into any workspace missing them — correct
      interactively, wrong for a benchmark instance. Closed with a
      `SEED_WORKSPACE=0` knob in both launchers, parity-guarded.

14. **`bench show` needs TWO clocks, which §5.3 did not anticipate.** The driver
    times the whole container lifetime; the harness measures only what happens
    inside it. On the baseline sweep that is 345.5s vs. 257.1s — 88s of container
    start-up the harness structurally cannot see. Folding it into the residual
    would have made M6's one inferred number look broken; it is reported as its
    own column instead.

15. **The driver pins `STATE_HOST_DIR` rather than re-deriving it.** The launcher
    keys the host state dir on `sha256(workspace)[:12]`, and a Python copy of that
    would have been a *third* mirror of launcher logic. Both launchers gained an
    override instead (parity-guarded), so the derivation stays in one place and
    each instance's telemetry is its own.

16. **`Instance.workspace` resolves relative to the dataset file**, not the
    process CWD, so a dataset is relocatable as a unit. Unspecified in §5.3 and
    load-bearing the moment anyone runs a sweep from somewhere else.

17. **A malformed dataset line is fatal, and so is an `--only` id that matches
    nothing.** Both could plausibly have been skips. A sweep that quietly ran 4 of
    5 instances — or 0 of 5 — would report a pass rate over a set nobody chose,
    which is the same silent partiality invariant 18 forbids one level up.

18. **The gold set's instance 004 had to be redesigned to be a real trap.** The
    first draft (a slug truncated onto a trailing hyphen) had no
    tempting-but-wrong fix — the obvious repair was simply correct, so the
    instance measured nothing the others did not. The shipped version is a bounded
    cache where "clear the cache when over capacity" makes the target test pass
    and breaks another. Verified by *applying the naive fix and watching it
    break*, not by inspection.

19. **Two more came from the tests rather than review**: `tests/_bootstrap._load`
    had to learn sub-packages (it resolved `harness.bench.patch` to
    `harness/patch.py`), and `shutil.rmtree` needed a read-only handler — git
    stores loose objects read-only, so on Windows every scratch tree survived
    deletion and a 50-instance sweep would have filled the disk in silence.

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

**Where the code lives.** New stdlib-only `harness/limits.py`: `DeadlineExceeded`,
`TurnLimitExceeded`, and the pure clock/counter logic (host tier, no langchain). The
`DeadlineMiddleware` that calls it sits with the other middleware classes, since it needs the
langchain base — the same split `telemetry.py`/`TelemetryMiddleware` and
`rawtrace.py`/`RawTraceMiddleware` already use, for the same reason. §12 fork 2 records why this is
not folded into `TelemetryMiddleware` and why `cost.py` is the wrong home despite owning
`BudgetExceeded`.

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

**Where it lives — one implementation, called from two places.** The extraction logic (intent-to-add,
pathspec exclusions, diff-against-base) lives in **`harness/bench/patch.py`**, stdlib + `git`
subprocess only. `--emit-patch` calls it from inside the container and puts the result on the
headless JSON; **the driver calls the same function** against the workspace it prepared.

That is not redundancy, it is the seam that makes §9's cross-harness runners possible later. A
foreign harness (Aider, SWE-agent, Claude Code) has no `--emit-patch` flag and never will, so the
driver has to be able to produce the patch itself from a workspace it owns. Two implementations of
`git add -A -N` + pathspec exclusion is two chances to get invariant 8 wrong, and the one that gets
it wrong is the one nobody is looking at.

Consequence for our own runs: `--emit-patch` becomes a **convenience, not the mechanism**. It is
still worth having — the in-container path is the one an operator can use by hand, without the
driver — but a sweep does not depend on it.

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

0. **Refuse to start** unless a step bound and a time bound both resolved (§12 fork 1). A sweep with
   no bound is the failure mode this milestone exists to remove; it must not be reachable by
   forgetting a flag.
1. Skip if `instance_id` is already in `predictions.jsonl` (resume; the file is append-only).
2. **Copy the instance to a scratch workspace the driver owns**, `.git` included, `.conda`
   excluded. The driver — not `EPHEMERAL=1` — owns preparation and disposal.
3. Launch `run-docker` — `.ps1` on `win32`, `.sh` elsewhere (§12 fork 4) — with `--headless`,
   `--topic <instance_id>`, the §5.1 bounds, and `DEEPAGENTS_WORKFLOWS_DIR` pointed at a
   **nonexistent** path — the loader returns early on a missing directory (§13 item 3), so no empty
   directory has to be created or managed.
4. Read the single JSON line from stdout. Anything on stderr is stage markers and stays there.
5. **Extract the patch from the scratch workspace** via `bench/patch.py` (§5.2), then delete it.
6. Append one line to `predictions.jsonl` and one to `runs.jsonl`. **Flush both after every
   instance** — a sweep that dies at instance 40 of 50 must not lose 39.
7. Continue on failure. An instance that crashes gets a prediction with an empty `model_patch` and a
   ledger row carrying the outcome; it never aborts the sweep.

**Why the driver owns the copy instead of `EPHEMERAL=1`.** Ephemeral mode reverts the workspace *on
container close*, which means the patch must be taken from inside, before close — i.e. only our
harness can produce one. With the driver owning a scratch tree, the workspace still exists after the
process exits and **anything** can be diffed the same way, which is the §9 seam. Ephemeral mode is
not being deprecated; it is simply not what a sweep is built on. (§13 item 2's finding still
applies, only to a different copier: whatever makes the scratch copy must keep `.git`.)

**The runner seam.** The launch step above is the only harness-specific part of the loop, so it goes
behind a one-method protocol:

```python
class Runner(Protocol):
    def invoke(self, workspace: Path, prompt: str, limits: Limits) -> RunResult: ...
    def capabilities(self) -> frozenset[str]: ...   # {"tokens","cost","tool_calls","run_id"}
```

`HolderRunner` is the only implementation in this milestone. Patch extraction is deliberately **not**
on the protocol — the driver does it uniformly for every runner, which is what would make a
comparison fair rather than a comparison of whose extractor is better. `capabilities()` exists so a
runner reports what it can measure and the ledger writes **`null`** for the rest, never an estimate
(M6's `null` ≠ `0.0` rule; §9).

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

Five instances, committed under `benchmarks/gold/`. Each is a small self-contained Python project
with a seeded bug, a failing test that pins it, and a passing suite around it.

**Hard requirements, each for a reason found in §13:**

- **Each instance is an initialised git repo with at least one commit** (§13 item 6). No base
  commit ⇒ nothing to diff against ⇒ no patch. The seeded bug is *in* that commit; the working tree
  is clean at handoff.
- **Deterministic.** No network, no clock, no randomness, no filesystem outside the instance. The
  check is a `pytest` exit code.
- **Small.** Solvable in well under the step bound, or the instance measures the bound rather than
  the harness.
- **Fixtures, not tests.** `pytest tests/` must not collect them — they live outside `tests/` and
  their own `pytest.ini`/`conftest.py` never reaches the harness suite.

**The five, chosen to vary the *loop shape*, not the domain.** Each column is a distinct way the
agent can fail that a single instance would not distinguish:

| # | id | Seeded bug | What it forces the agent to do | Why this one exists |
|---|---|---|---|---|
| 1 | `gold-001-off-by-one` | Paginator returns one row too many on the last page | Read one file, make a one-line edit | The floor. If this fails, the loop is broken, not the model. Fastest signal, first to run. |
| 2 | `gold-002-hidden-caller` | A helper's contract changed; the bug surfaces in a *different* module that the prompt does not name | Search the repo for callers it was not pointed at | Exercises exploration. A model that only edits what it was handed fails here. |
| 3 | `gold-003-read-the-failure` | Error is a `TypeError` deep in a stack; the prompt gives only "the CLI crashes on empty input" | **Run the tests** and read the traceback before editing | Exercises the shell tool + the edit→run→re-read cycle. The instance most sensitive to a tool-schema regression (this is the `num_ctx` class of defect). |
| 4 | `gold-004-regression-trap` | The obvious fix (widen a guard) makes `fail_to_pass` pass and **breaks two `pass_to_pass` tests** | Run the whole suite, not just the named test | The only instance that catches "passes the target test by breaking the module". Scores 0 if the agent stops at the first green. |
| 5 | `gold-005-new-module` | Fix requires **creating a new file** (extracting a validator the two callers need) | Add a file that git has never seen | **Done-when #3's live case.** Fails silently to an empty patch without `git add -A -N` (§5.2) — the instance that would have caught that bug. |

Instance 5 is load-bearing twice over: it is a real task shape *and* it is the regression test for
the milestone's own most likely silent defect. Build it early, not last.

`fail_to_pass` / `pass_to_pass` are literal pytest node-id commands, so the same fields carry
straight into tiers 2 and 3 without a format change.

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
- **Cross-harness runners — the seam exists, the adapters do not.** `Runner` is declared and
  `HolderRunner` implements it (§5.3); no `AiderRunner` / `SweAgentRunner` / `ClaudeCodeRunner`
  ships here. Deferred to **tier 2**, where the dataset is somebody else's and the comparison means
  something. Three things are already settled so that tier does not have to re-litigate them: patch
  extraction is driver-side and uniform (§5.2); unsupported metrics are `null`, never estimated; and
  only **patch, exit code and wall clock** are universal — tokens/cost/tool-calls depend on what a
  given harness exposes (Aider's analytics, Claude Code's headless JSON, SWE-agent/OpenHands
  trajectory files), and step bounds have no cross-harness equivalent at all, so the universal floor
  is a wall-clock bound plus a process kill.

  Two things that tier must confront and this one does not: **confounds** (a different model,
  context size or tool set makes the number measure the model, not the harness — pin the model
  across runners and record it per row), and the fact that **a self-authored gold set is not a
  leaderboard**. Tier 1 tuned to our own harness is a sanity check across runners at best; a
  publishable cross-harness number needs a set nobody here wrote, which is exactly what tiers 2 and
  3 are.
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

- **`tests/test_limits.py` (host tier, stdlib only)** — the deadline arithmetic and the turn counter
  as pure functions: a deadline not yet reached, one just crossed, one with no deadline set (the
  pass-through), and the counter's off-by-one at exactly `max_turns`.
- **`tests/test_bench.py` (host tier, stdlib only)** — dataset parsing incl. malformed lines,
  resume-skip logic against an existing `predictions.jsonl`, the predictions record carrying exactly
  three keys, the join from a synthetic headless payload + a synthetic `usage.jsonl` to a
  `runs.jsonl` row, and the empty-patch counter. The subprocess launch is injected, not run.
- **`tests/test_bench_patch.py` (host tier, `git` required — skip if absent)** — the extractor
  itself, driven **directly** rather than through the headless JSON: intent-to-add surfaces an
  untracked file, every excluded path stays out on a workspace where all of them are dirty, the
  diff is taken against the recorded base rather than `HEAD` (asserted with a commit sitting on
  top), and the result passes `git apply --check` against a fresh copy of the base. This is
  invariant 12a's file: a sweep must not depend on `--emit-patch`, so the extractor is tested
  where the driver calls it.
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

## 12. Forks (resolved)

1. **`--max-steps` default: unset.** The harness sets `recursion_limit` only when the knob is
   passed — absence is the removable contract (§10), and picking a number for every interactive run
   is a behaviour change this milestone has no business making. **The driver always passes one**,
   and `harness bench run` **refuses to start** without a step bound and a time bound resolved from
   somewhere. A benchmark run is never on an inherited default nobody chose; an interactive run is
   never silently re-bounded.

2. **The deadline is its own middleware, and it splits like M6/M7 do.** A new stdlib-only
   `harness/limits.py` holds `DeadlineExceeded`, `TurnLimitExceeded` and the pure clock/counter
   logic; `DeadlineMiddleware` (which needs the langchain base) lives beside the other middleware
   classes. Two reasons over folding it into `TelemetryMiddleware`: telemetry must stay removable on
   its own (M6 §10), and the split keeps the arithmetic in the host test tier, which is the same
   reason `rawtrace.py`/`RawTraceMiddleware` and `telemetry.py`/`TelemetryMiddleware` are already
   split that way.

   Exception placement follows the existing convention — exceptions live with their subsystem, not
   in a shared errors module (`BudgetExceeded` in `cost.py:283`, `HaltTurn`/`InterruptAborted` in
   `hitl.py:146,163`, `PathGuardDenied` in `pathguard.py:24`). `limits.py` is that subsystem.
   **`cost.py` is the wrong home** despite owning `BudgetExceeded`: a step or clock bound is not
   cost, and `cost.py` is under an acyclic import guard that a new concern should not be pushed
   through.

3. **`harness/bench/`, not `scripts/bench.py`.** It wants the same argv grammar, the same keyless
   guarantee, and the same host test tier as every other admin command — and `scripts/` would oblige
   a `.ps1`/`.sh` pair under the parity rule for something that is pure Python. Routed through
   `entry.dispatch` with a function-local import, like `telemetry` / `doctor` / `config`.

4. **Cross-platform in v1, by launcher selection.** The primary development host for this repo is
   Windows/PowerShell, so a Linux-first driver is one the author cannot run. The driver picks
   `scripts/run-docker.ps1` on `sys.platform == "win32"` and `scripts/run-docker.sh` elsewhere; both
   already accept trailing passthrough, measured on both (§13 item 1). The driver must spell every
   forwarded flag with a **double dash** — a single-dash `-model` binds to the PowerShell launcher's
   own parameter instead of reaching `main.py`.

---

## 13. Verified against the code (2026-08-17)

Six assumptions this plan rests on, checked rather than inferred. Recorded because the M7 lesson
generalises — a plan that reads plausible and a plan that is true are different things, and the
difference only shows up when someone reads the actual seam.

1. **Both launchers pass trailing arguments through to the container command.** ✅
   `run-docker.sh:642` — `AGENT_RUN+=(python3 main.py "$@")`. `run-docker.ps1:15–16,658–660` — a
   `ValueFromRemainingArguments` positional `[string[]]$TaskParts`, appended after `python3 main.py`.
   So `--emit-patch` / `--max-steps N` reach `parse_args` with no launcher change.

   **Measured, not inferred (Windows PowerShell 5.1.26100.9168, Desktop edition).** A faithful
   mini-repro of the `param()` block was driven with `--`-prefixed flags. All five cases bind into
   `$TaskParts` in order, values kept as separate elements:

   | Case | Result |
   |---|---|
   | `--headless --emit-patch --max-steps 40 "fix the bug"` | 5 elements, in order |
   | `-Cpus 4 --headless --max-steps 40 "task"` | 4 elements; the declared param binds, the rest pass through |
   | `--model ollama:gemma4 --headless "task"` | 4 elements — **`--model` does not collide with the declared `-Model`** |
   | `--max-cost 1.5 --workspace /project/workspace "task"` | 5 elements |
   | `--% --headless --emit-patch …` | **2 elements — broken.** `--%` lands as a literal argument and collapses the rest into one string |

   Two conclusions. **No stop-parsing token is needed**, and **`--%` must not be used** — the
   fallback an earlier draft of this section named would have been the bug rather than the fix. A
   token like `--model` is read as the parameter name `-model`, which matches no declared parameter,
   so `ValueFromRemainingArguments` collects it; the declared `-Model` is only reachable with a
   single dash. The one standing rule this leaves for the driver: **always double-dash.** A
   single-dash `-model` *would* bind to the launcher's own parameter and never reach `main.py`.

2. **`.git` survives the ephemeral copy.** ✅ `run-docker.sh:198–208` — the copy uses `dotglob` and
   skips exactly one entry, `.conda`. The comment at line 199 says dotfiles including `.git` are
   copied deliberately. So `EPHEMERAL=1` composes with `--emit-patch`: the throwaway workspace is
   still a git repo with the instance's history, which is what the diff needs.

   **Now a requirement rather than a dependency.** §5.3 moved copy-and-dispose to the driver, so a
   sweep no longer rides on ephemeral mode — but the finding is unchanged and it transfers: whatever
   makes the scratch copy must keep `.git`, or the diff has no base. `run-docker.sh:198–208` is the
   reference implementation to copy the *rule* from, not the code path the driver uses. (The
   original phrasing of this item, "had `.git` been excluded the whole B2/B3 design would have been
   unbuildable", was true of the ephemeral-based design and is what prompted looking at it.)

3. **The workflows loader tolerates a missing directory.** ✅ `workflows.py:127` —
   `if not workflows_dir.is_dir(): return`. So the driver points `DEEPAGENTS_WORKFLOWS_DIR` at a
   **nonexistent** path and no workflow is discovered; it does not have to create and manage an
   empty directory. (Both `git-branch` and `git-pr` are folder-discovered, so this disables the pair
   together, which is what §5.2 requires.)

4. **`headless` and `telemetry` are the precedent for a non-persisted knob.** ✅ `config.py:456,481`
   — both are `FieldSpec(tier="prespinup", env_var=..., profile_key=None)`. So `--emit-patch` is a
   real `FieldSpec` (it gets validation and `harness doctor` display for free) that is deliberately
   **not** written to the profile: it is a per-sweep mode, not a preference. `max_turns` /
   `max_steps` / `max_seconds` are `tier="live"` alongside `max_cost` / `max_tokens`, and persist.

5. **`max_steps` can be live.** ✅ The graph `config` dict is built once in `cli.main` and passed
   into every `run_turn` call (`cli.py:951–954`), so an applier mutating `config["recursion_limit"]`
   takes effect on the next turn with no agent rebuild — the same shape as M7's raw-trace applier.

6. **`git-branch`'s gate is `trigger.sh`, and gates on being a git repo.** ✅ So a gold-set instance
   **must be an initialised git repo with at least one commit** — otherwise there is no base to diff
   against and `git diff <base>` has nothing to say. B4 states this as a requirement rather than
   leaving it implicit.
