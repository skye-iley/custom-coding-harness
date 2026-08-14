# Milestone 6 — Telemetry

## 0. Build status

**Built — all slices landed (T1–T6) on `feat/milestone6-telemetry-impl`, merged to `main` in
PR #46.** `harness/scrub.py` and
`harness/telemetry.py` are new; `cli.py` owns `TelemetryMiddleware` and the `run_turn` write;
`ratelimit.py`, `hitl.py`, `config.py`, `cost.py` and `workflows/git-pr/open-pr.sh` carry the seam
changes `milestone6_spec.md` §15.1 lists, and nothing outside that list moved.

This doc is the plan, written before the code so the code has something to be wrong against. Every
claim below about *existing* behaviour was read off the tree at `6e0c104`.

**A pre-build pass re-checked every seam the spec names against the tree** and found five claims
that did not hold plus five decisions left unmade. All ten are settled in `milestone6_spec.md`
(header table lists them) and the two that reach this doc are corrected in place: §3 T3's summary
placement, and §3 T1's scrub-extraction naming. The spec is the doc that changed; nothing about the
plan's scope, forks, or §5a placement argument moved.

### 0.1 What the build changed about the plan

Three things, each recorded because the superseded version is the one a reader would otherwise
assume.

1. **The §3.1 middleware-order probe resolved, and its answer reversed the planned fix.** The
   composition fact was settled from `langchain.agents.factory._chain_tool_call_wrappers` (langchain
   1.3.15) rather than from a timing probe — the source says *"first = outermost"*, so telemetry,
   appended before `PauseMiddleware`, is the outer `wrap_tool_call`. The spec planned to subtract
   `hitl_wait_ms` from `tool_ms` in that case. **That subtraction is wrong and was not
   implemented**: `PauseMiddleware` raises langgraph's `interrupt()`, which *suspends* the graph, so
   the human is asked in `run_interrupt_loop` after `invoke` has already returned. The wait is never
   inside the wrapper, and subtracting it would have removed time that was never counted.
   The probe still earned its place, by surfacing what the spec had missed: the gate raises
   **through** telemetry's wrapper, so `GraphInterrupt`/`HaltTurn` must not count as tool errors and
   a gated call must not be counted twice (it enters the wrapper once to suspend, once on resume).
   Full record in `milestone6_spec.md` §3.1.
2. **The scrub traversal is wider in telemetry than in audit.** `scrub_deep` moved verbatim, as
   specified, and scrubs dict *values* only — correct for `audit`, whose `meta` keys are
   harness-chosen labels. Telemetry's `tool_calls` is keyed by the **tool name off the model's tool
   call**, which is not a harness constant, so `telemetry._scrub_all` extends the traversal to keys.
   One redaction implementation still (`scrub`); only the walk is wider, and the wider walk lives in
   the module that needs it rather than in the shared one whose oracle test pins the narrower
   behaviour.
3. **`interrupts` is a turn-record field.** §9's summary schema carries an interrupt count and
   invariant 6 requires every summary field to be *derivable from the records*, which the §2 record
   list could not support. It is counted at the same seam that measures `hitl_wait_ms` (the `on_wait`
   observer). Additive, so no schema bump — §2's own rule.

4. **`before_agent` is not the turn boundary — the spec was wrong about that, and it mattered.**
   §3/§3.2 had `TelemetryMiddleware` reset its accumulator in `before_agent`. That hook fires once
   per **invoke**, and a turn invokes several times: the resilience layer re-invokes on a retry, and
   every HITL resume is another invoke. The reset therefore erased `retry_sleep_ms`/`retry_count`
   (which accumulate *between* invokes) and every tool and model count from before a gate suspended
   the graph — silently, and only on exactly the turns worth investigating. `run_turn` is the only
   thing that brackets a whole turn, so `begin_turn()` is called from there and the middleware
   defines no `before_agent`. The same defect applies to `tracker.turn`, which §8 told the
   implementer to read: per-turn cost is now a **delta against `tracker.session`** (never reset),
   which is right for any invoke count and makes the per-turn costs sum to the session total by
   construction rather than by luck. Regression tests:
   `test_retry_numbers_survive_the_re_invoke`, `test_turn_cost_is_a_session_delta_not_tracker_turn`,
   `test_per_turn_costs_sum_to_the_session_total`.

Two smaller notes: `cost.CostTrackerMiddleware` gained a `has_energy` property (without it,
`turn.energy_wh == 0.0` on a model with no energy table is indistinguishable from a measured zero),
and `cli._cost_totals_for_row` was split so `_cost_and_provenance` can ask the same "is this a real
number or a floor" question of `tracker.turn` that the ledger row asks of `tracker.session`.

**3. `failed` was measuring two different things, and the boolean could not say which.**
Found in the pre-merge review. The build excluded an operator deny from `failed` on invariant 2a's
reasoning — a governance signal does not belong in the reliability column — and then let
`run_turn`'s `except BaseException` flag `BudgetExceeded`, `KeyboardInterrupt` and
`InterruptAborted` as failures anyway. Those are the same kind of event: a `--max-cost` cap firing,
an operator's Ctrl-C, and the headless policy failing closed are all the harness doing what it was
configured to do. A SWE-bench sweep run under a cost cap would have reported every capped instance
as a harness failure, indistinguishable from a crash — in the one number a sweep is most likely to
aggregate.

Two ways to fix it, and the cheap one loses information: excluding the three from `failed` makes a
capped turn indistinguishable from a completed one. So the record now carries **`outcome`**
(`ok`/`denied`/`budget`/`cancelled`/`aborted`/`error`) and `failed` became a *derived property*
(`outcome == "error"`), which is what stops the two from ever disagreeing again. The summary gains
an `outcomes` map that sums to `turns`, and the PR block names the mix (`3 (1 budget)`) instead of
counting failures that were not failures. Additive, so no `SCHEMA_VERSION` bump — §2's own rule —
and `derive_session` falls back to `failed` for a record written before the field existed, since
reconstructing `ok` for those would report zero failures on a run that had them.

**Also found in that review, and unrelated to the above:** `open-pr.sh` hardcoded `cd /project`
before invoking `-m harness telemetry pr-block`. Correct in the image, wrong everywhere else — and
`tests/test_workflows.py`'s `open-pr.sh` cases run on the *host*, where they had been skipping for
an unrelated reason (the probe imported `harness.telemetry`, which pulled `cli` and failed without
langchain). Merging M5 §0.1 F6 removed that reason, the cases started running, and the hardcoded
path failed them. The root is now derived from the script's own location (`workflows/git-pr/` is two
levels under it), which is the same directory in the image and the right one on a host.

Chosen over the `HarnessProfile` chain deliberately: telemetry is one of the two core-identity items
with **no dependency on the trust-boundary chain** (`design_doc.md` → Product Identity → "Core
identity — independent of the chain above"), so it cannot be blocked by the one open question there
— whether the bwrap jail actually starts on an AppArmor host, which is being measured separately.

## 1. Goal & Definition of Done

`design_doc.md` §8 (Observability & Metrics) + §12.7 (cost / telemetry persistence). The harness
**already computes** almost everything §8 asks for and then throws most of it away: M1's tracker
prints a per-turn usage line to stderr and forgets it, and M2 persists only a session-level roll-up
on one `past.sqlite` row. §8 is not a from-scratch build. It is a **sink** and a **surface**.

**The primary purpose is per-run attribution good enough to benchmark with** — to answer "what did
this run cost, and where did its wall-clock actually go" precisely enough that two runs, two models,
or two harness versions can be compared. That target is what sets the field list: a number that
cannot be decomposed cannot be compared, and a number that silently absorbs a 60-second rate-limit
sleep is not a latency measurement.

Done when:

1. Every turn appends a structured record to a durable per-run sink, secret-scrubbed.
2. Every run writes one session summary artifact derived from those records.
3. That summary reaches the **PR body** the `git-pr` workflow opens, when a PR is opened.
4. Keyless read access to the summary exists, and a **headless run's stdout JSON can be joined to
   it** without parsing stderr.
5. **Wall clock decomposes** (§3 T2): `duration_ms` splits into model, tool, retry-sleep,
   rate-limit-sleep and overhead, each measured at a real seam rather than inferred.
6. **Removable contract holds:** with telemetry off, the harness is byte-for-byte Milestone 5.1.
7. Nothing in the sink can leak a credential, a prompt, or a file's contents.
8. **The agent cannot edit its own record.** The sink is outside the workspace, unreachable by the
   agent's file tools and — under `DEEPAGENTS_JAIL=1` — by its shell (§5a).

## 2. What exists today — the honest inventory

| Source | Has | Gap |
|---|---|---|
| `cost.py` `UsageAccumulator` | per-turn `input`/`output`/`cache_read`/`cache_write`/`cost`/`energy_wh`/`unpriced_calls`/`estimated_calls` | printed to stderr, then **discarded**. No per-turn record survives the process |
| `cost.py` `CostTrackerMiddleware` | `before_agent`/`after_model`/`after_agent` hooks, usage already parsed | built **only when there is something to track** (M1's null=MVP contract), so telemetry **cannot ride on it** — it needs its own middleware or it silently vanishes on unpriced models, which is exactly the local-Ollama benchmark case |
| `workflows.py` `WorkflowMiddleware` | `before_model` / `after_model` / `wrap_tool_call(request, handler)` | proves the seams exist and are already used in-repo; nothing times them |
| `hitl.py` `PauseMiddleware` | `wrap_tool_call` with the tool name off `request.tool_call` | same seam telemetry needs for per-tool counts |
| `resilience.retry_call` | `sleep=` **injected** by the caller and `on_retry(attempt, exc, delay)` observer | `cli._invoke_resilient` passes plain `time.sleep` and uses `on_retry` only for a stage marker — retry time is spent and unrecorded |
| `cli._invoke_resilient` | the one-shot context-overflow branch (`trim_messages`, retry once) | a trimmed turn is not comparable to an untrimmed one, and nothing records that it happened |
| `ratelimit.build_rate_limiter` | a langchain `InMemoryRateLimiter` handed to the model as `rate_limiter=` | it blocks **inside** the model call, so pacing time is currently indistinguishable from model latency |
| `archive.py` `sessions` row | `input_tokens`, `output_tokens`, `cost_usd`, `cost_provenance`, per `run_id` | session-level only; no per-turn, no energy, no latency, no model mix |
| `cli._batch_payload` | headless stdout JSON: `final_message`, `thread_id`, `tokens`, `cost_usd`, `branch`, `pr_url`, `exit_code` | carries `thread_id`, **not** `run_id` — and `run_id` is the `past.sqlite` key (`thread_id` repeats across resumes). A batch driver cannot reliably join its own output to the ledger |
| `audit.py` | jsonl append + **recursive** secret scrub + the two-sink split | an approval/boundary record, not telemetry — but the substrate to reuse rather than reinvent |
| `workflows/git-pr/open-pr.sh` | `gh pr create --body "<hardcoded string>"`, already sources `$DEEPAGENTS_STATE_DIR/session.env` | the PR surface seam; nothing reads a summary today |

**Read this table as the reason the milestone is small.** Nearly every row is "the seam exists and
is already used for something else." The work is capture, placement, and not leaking anything.

## 3. Slices (in build order)

### T1 — `harness/telemetry.py`: the per-turn sink
New stdlib-only module. `record_turn(path, record)` appends one scrubbed JSON object per line to
**`<state-dir>/usage.jsonl`** — `archive.state_dir(workspace)`, the same root as `past.sqlite`,
`checkpoints.sqlite`, `session.env` and `denials.jsonl`. **Outside the workspace mount** (§5a — the
load-bearing placement decision). Reuses `audit.py`'s scrub, **extracted to a shared helper, not
copy-pasted** (`tests/_bootstrap.py` exists because two test modules had duplicated a loader) —
`scrub` / `scrub_deep` move to `harness/scrub.py` **under those exact names**, re-exported from
`audit`, because the existing `test_audit.py` scrub cases are the oracle for the move and must pass
unedited (`milestone6_spec.md` §1).

Record fields, v1:

| Group | Fields |
|---|---|
| Identity | `run_id`, `thread_id`, `topic`, `turn`, `ts` |
| Model | `provider`, `model` |
| Tokens | `input`, `output`, `cache_read`, `cache_write` |
| Money/energy | `cost_usd`, `cost_provenance`, `energy_wh`, `unpriced_calls`, `estimated_calls` |
| Time (§3 T2) | `duration_ms`, `model_ms`, `tool_ms`, `retry_sleep_ms`, `paced_sleep_ms` |
| Work | `model_calls`, `tool_calls` (per-tool-name counts), `tool_errors` |
| Anomalies | `failed`, `retry_count`, `context_trimmed` |

No prompt text, no reply text, no tool arguments — §4.

**Import layering:** `telemetry.py` imports no sibling but `harness.audit` (for the scrub). `cost.py`
must **not** import it — the acyclic guard `test_import_isolation` enforces stays intact; `cli.py`
feeds telemetry, exactly as it feeds `archive.py`.

### T2 — capture: make wall clock decompose
The slice this milestone is really about. `duration_ms` is measured around the turn in
`cli.run_turn`; each component is measured at a **real seam**, never inferred by subtraction:

| Component | Seam | Note |
|---|---|---|
| `model_ms` | `before_model` / `after_model` on a new `TelemetryMiddleware` | summed across the ReAct loop's calls, hence `model_calls` |
| `tool_ms`, `tool_calls`, `tool_errors` | `wrap_tool_call(request, handler)` | name off `request.tool_call` — the same field `PauseMiddleware` reads (and the same one an early HITL bug got wrong by reading top-level `tool_name`) |
| `retry_sleep_ms`, `retry_count` | swap `_invoke_resilient`'s `sleep=time.sleep` for an accumulating wrapper; `on_retry` for the count | the injection point already exists precisely so the caller controls sleeping |
| `paced_sleep_ms` | instrument `ratelimit.build_rate_limiter` — time `acquire()` | **without this the pacing hides inside `model_ms`**, and on a throttled free tier that is most of the run |
| `context_trimmed` | `_invoke_resilient`'s overflow branch | a trimmed turn is not comparable to an untrimmed one |
| overhead | `duration_ms` minus the rest | the *residual*, and the only inferred number — which is why the invariant checks it is small and non-negative rather than assuming it is zero |

**`TelemetryMiddleware` is its own middleware, not a hook on the cost tracker.** M1 appends the cost
tracker only when there is something to track, so on `ollama:gemma4` — `pricing = "free"`, the
default provider, the benchmark case — there is no tracker at all. Telemetry that rode on it would
be silently absent exactly where it is most wanted.

Writes must never raise into the turn path — the rule the audit sink and `/config` dispatch follow.

### T3 — session summary artifact
At session end, derive `<state-dir>/session.json` from the turn records: totals, model mix, turn
count, failed-turn count, wall-clock duration and its decomposition, interrupt count, retry/trim
counts. Written in `cli.main`'s end sequence **after** `_finalize_session` (so numbers match stderr
and the `past.sqlite` row) and **before** `_pr_approval` / `run_hook("session.end")` (so git-pr can
read it) — but **outside** the `if archive_conn is not None:` block that `_finalize_session` call
sits inside, or `DEEPAGENTS_ARCHIVE=0` would silently produce no summary (`milestone6_spec.md` §9).

`past.sqlite` stays **authoritative** for session totals; `session.json` is derived. Two files
disagreeing is worse than one file missing, so the invariants pin the derivation, not the values.

### T4 — telemetry-to-PR
`open-pr.sh` gains: if the summary exists, append a fenced summary block to the PR body
(`--body-file`, so a large body isn't shell-quoting-fragile). Absent, unreadable, or malformed file,
or no `gh` ⇒ today's body, unchanged, exit 0. Aggregates only — never a turn record, never a prompt.

No new plumbing for the state-dir read: `open-pr.sh` already sources
`${DEEPAGENTS_STATE_DIR:-.deepagents}/session.env`, and `stage-commit-push.sh` already reads the
frozen `$state_dir/mask-snapshot.txt` with the comment *"can't be tampered with post-launch (state
dir is agent-unreachable)"*. The workspace sink §5a rejects would have been the only git-pr input
the agent could edit.

### T5 — read access + the headless join
- `harness telemetry show [--run <id>]` via the keyless `dispatch` route (stdlib only, like
  `memadmin`), reading the same state dir `harness past` reads.
- **`_batch_payload` gains `run_id` and the summary path.** Today it emits `thread_id`, which
  repeats across resumes and is not the `past.sqlite` key; a batch driver (see §5b) cannot join its
  own stdout to the ledger without guessing. This is three fields and it is the difference between
  telemetry you can aggregate over 300 instances and telemetry you read one file at a time.

### T6 — removability + tests
`DEEPAGENTS_TELEMETRY=0` ⇒ no sink, no summary, no PR block, no middleware, no new stderr line.
Host-tier tests for record shape, scrub, derivation, decomposition arithmetic, and the git-pr
no-ops. **Live-model case** for the one thing a stub cannot check: a real turn against a real model
produces non-zero token counts and a `model_ms` that is a real fraction of `duration_ms` — the
CLAUDE.md guideline (assert against a real model where the behaviour is the model's), and
`usage_metadata` is exactly the field providers have silently omitted before.

## 4. Non-goals

- **No prompt/response trace.** §8's `.agent-trace.jsonl` reads as "model inputs and responses";
  that is `DEEPAGENTS_RAW_TRACE` (`design_doc.md` §11), a separate feature with a different risk
  profile. This milestone records *measurements*, not *content* — which is what makes the scrub a
  backstop rather than the primary defence.
- **No remote export, no OpenTelemetry, no dashboard.** Files on disk + a PR block.
- **No new cost math.** M1 owns pricing/energy; telemetry records what it already computes.
- **No benchmark runner.** §5b describes how an external driver would use this; the harness does not
  grow one.
- **No automatic retention/GC.** Same posture as M2: manual prune, policy deferred.

## 5. Forks (decide before T1)

1. **Which sink dir — workspace `.agent_telemetry/` or the state dir?** → **The state dir.**
   Telemetry is an audit surface; the audited party must not be able to edit the record. Full
   argument in §5a — this fork was initially decided the other way, so the reversal is written out.
2. **Extend `past.sqlite` or write files?** → **Both, with one authority.** Per-turn stream in jsonl
   (append-only, cheap, greppable); session totals stay the sqlite row; `session.json` is derived
   and must equal the row on shared fields.
3. **Which §8 metrics ship in v1?** → Those with a data source **today** (the §3 T1 table).
   **Deferred by dependency, not choice:** routing accuracy (no router — core-identity item 5),
   token-reduction ratio (no compression — §7), session success rate / PR quality (needs post-hoc
   GitHub state), **TTFT** (the harness does not stream, so there is no first-token event to time —
   `model_ms` is full-response latency and the docs must not call it TTFT).
4. **Naming — §8 says `.agent-trace.jsonl` / `.agent-metrics.json`.** → **Deviate**:
   `<state-dir>/usage.jsonl` + `<state-dir>/session.json`, following fork 1 and the flat naming the
   state dir already uses. §8's names also imply a content trace, which §4 rules out. `design_doc.md`
   §8 gets updated when this lands.
5. **What does `duration_ms` mean?** → Wall clock around the turn, tool execution and HITL wait
   included. It is **not** model latency — that is `model_ms`. Both are recorded precisely so the
   ambiguity that makes single-number "latency" useless cannot arise.
6. **Measure pacing, or fold it into model time?** → **Measure it.** Instrumenting the limiter's
   `acquire()` is a handful of lines in `ratelimit.py`; not doing it means a free-tier run reports
   model latency of ~60s/call, which is a property of the *plan*, not the model. This is the fork
   most likely to be skipped for expedience and the one that most corrupts comparisons.

## 5a. Telemetry is an audit surface — why the sink is agent-unreachable

An earlier draft put the sink in the workspace's `.agent_telemetry/`, reasoning that telemetry is
"UX/metrics, not boundary evidence" and that `interrupts.jsonl` already lives there. That was wrong
on three counts, and the reasoning is recorded because the wrong version is the intuitive one.

**1. It is evidence about the agent, produced by the harness, read by a human.** That is the
definition of an audit record whatever the file is called. The fields are: what the run cost, how
many turns it took, how many *failed*, how many tool calls it made, which models it used. Those are
exactly the facts an operator checks when a run behaves badly or bills more than expected. A record
the audited party can rewrite is not evidence — and an agent has motive here that it lacks for, say,
its own recall history: a budget ceiling it is about to hit, a failed-turn count that makes it look
ineffective, tool thrash. Whether today's models would actually do it is not the question placement
has to answer; the question is whether the record *means* anything, and a mutable one does not.

**2. M4 slice D already settled this exact question.** `denials.jsonl` went to the state dir because
*"a denial is evidence the agent tried to escape, and the workspace log is in-bounds for the path
guard, so the agent's own file tools could truncate it."* Telemetry sits on the same side of that
line. The workspace `interrupts.jsonl` is the *counter*-example: a HITL UX trail, deliberately
agent-writable, and M4 split the two sinks precisely because "where the existing log lives" is not a
reason.

**3. The workspace placement also does not survive.** Under `-Ephemeral` the workspace is a
throwaway copy discarded on close, while the state dir is keyed to the *real* workspace path and
persists across ephemeral runs. Telemetry written into the workspace would evaporate exactly in the
mode most likely to be used for risky or exploratory runs — and for a benchmark sweep, which is the
same shape. It would also sit inside the agent's commit tree, needing a git-pr exclusion to stay out
of the diff — a class of mistake (M4 had to stop a masked `.env` being committed) the state dir
makes structurally impossible.

**What the tamper-resistance actually is — stated precisely, not aspirationally.** The state dir
defeats the **file-tool** path unconditionally: `pathguard` + the workspace-rooted backend mean the
agent's `read_file`/`write_file` cannot address it. It defeats the **shell** path *only under
`DEEPAGENTS_JAIL=1`*, where bwrap binds the workspace and nothing else; with the jail off — the
default — a container shell can still reach the state dir by absolute path. Same caveat `audit.py`
already carries for `denials.jsonl`, repeated here rather than rounded up to "tamper-proof." The
repo has been burned once by a boundary claim outrunning the code (M4 slice H / AppArmor). Honest
statement: **file-tool-proof always, shell-proof under the jail, and `past.sqlite` in the same dir
remains the authoritative ledger regardless.**

One consequence to design for rather than discover: `state_dir()` falls back to
`<workspace>/.deepagents` when `DEEPAGENTS_STATE_DIR` is unset, putting the sink *back inside the
mount* on a raw `docker run` that skips `run-docker`. `harness doctor` already errors when an
in-container state dir resolves inside the workspace; telemetry inherits that check rather than
adding its own, and the invariants pin that it is inherited.

## 5b. Using this for a benchmark sweep (e.g. SWE-bench Lite)

Recorded because it is the milestone's primary motivation, and because the boundary is easy to get
wrong in both directions.

**Telemetry does not score the benchmark, and must not try.** SWE-bench resolution is decided by the
benchmark's own evaluation: apply the predicted patch, run `FAIL_TO_PASS` + `PASS_TO_PASS`,
pass/fail. The prediction is `git diff` from the workspace. None of that is telemetry's business,
and a harness that reported its own success rate would be marking its own homework.

What this milestone provides is the **other half of the report** — the `$`, `tokens`, and `time`
columns, per instance, decomposed. Concretely, a driver would:

- run one instance per harness invocation with `--headless` (one-shot, JSON on stdout, meaningful
  exit code — M3 P2);
- pass `--topic <instance_id>`, which is already a free-text per-run label that lands on the
  `past.sqlite` row and in every telemetry record, making it the join key;
- read `run_id` + the summary path from the headless JSON (T5) and collect `session.json`;
- take correctness from the SWE-bench harness, entirely separately.

**Two caveats that will otherwise surprise you.** (1) On the shipped default `ollama:gemma4`,
`pricing = "free"` ⇒ `cost_usd` is structurally `0`/NULL, not a measurement: for local sweeps the
comparable columns are tokens, `model_ms`, and energy. (2) The git session workflows create a branch
per run and try to open a PR; against a benchmark checkout they are safe no-ops without a remote or
`gh`, but a sweep should confirm that rather than assume it.

## 6. Risks

- **Double-counting.** Two writers of the same numbers is how ledgers drift. Mitigation: fork 2's
  single-authority rule + an invariant pinning `session.json` to the sqlite row.
- **Decomposition that silently stops adding up.** The residual is the one inferred number; if a new
  blocking call appears (a future streaming path, a second limiter) it lands in overhead unnoticed.
  Mitigation: the decomposition invariant bounds the residual instead of ignoring it.
- **Leakage into a PR.** T4 publishes outward. Mitigation: aggregates only by construction (built
  from `session.json`, which has no free-text field), plus the scrub, plus a test that a planted
  secret in a turn record cannot reach the body.
- **Per-turn write cost / turn-path fragility.** One append per turn is negligible next to a model
  call, but the write must not be able to raise. Mitigation: the never-raise wrapper the audit sink
  already uses.
- **Removable-contract erosion.** Telemetry touches `run_turn` and adds a middleware — the riskiest
  such seam yet, because it is per-turn rather than per-session. Mitigation: middleware appended
  only when enabled, plus a byte-for-byte-off invariant.

## 7. Operator decisions (settled)

- **Telemetry defaults ON.** `DEEPAGENTS_TELEMETRY=0` is the off switch, and the removable contract
  (invariant 20) is what makes defaulting on safe. A record that exists only when someone remembered
  to enable it is not much of an audit surface — the run you want telemetry for is the one you did
  not expect to go wrong.
- **The off switch is env-only** — an M5.1 registry field with `profile_key=None` and no CLI flag,
  the same shape `mask_enabled` and `headless` have. Persisting it would oblige both launchers to
  forward it and would put "do you want telemetry?" in the security wizard, which contradicts
  defaulting on. Reasoning in full: `milestone6_spec.md` §7.
- **The PR summary goes in the body**, not a comment. One artifact, no second API call, survives
  with the PR.
- **The sink is agent-unreachable** (§5a) — telemetry is an audit surface, not a metrics
  convenience.
- **Benchmark-grade attribution is in scope for v1**, not a follow-up: all of §3 T2's decomposition,
  the per-tool mix, and the headless join land in this milestone. The schema is cheap to get right
  now and expensive to migrate once a sweep's worth of records exists.

## 8. Invariants (folded in from `milestone6_invariants.md` on completion)

The checkable assertions this milestone's build and tests were held to.

> **Status: implemented and covered.** They were written first, on purpose — a telemetry milestone
> whose correctness is "the numbers look about right" is untestable by construction, so the numbers
> got pinned before they existed. Where the tests live: 1–13 and 20–25 across
> `tests/test_telemetry.py` (host), `tests/test_cli.py` (image), `tests/test_workflows.py` (the
> git-pr degradation cases), `tests/test_ratelimit.py` (4b's seam), `tests/test_hitl.py` (the
> `on_wait` observer 4a depends on), `tests/test_agent.py` (14/15/19), and one live case in
> `tests/test_live_model.py`.
>
> **Two were amended by the build, neither weakened:**
>
> * **4g** — the subtraction `milestone6_spec.md` §3.1 planned for the "telemetry is outer" case was
>   **not** implemented, because the probe showed the HITL wait never happens inside
>   `wrap_tool_call` at all (the gate *suspends* the graph; the human is asked afterwards, in
>   `run_interrupt_loop`). The invariant now holds structurally rather than by arithmetic, which is
>   stronger. The probe's actual finding is a new obligation, covered below.
> * **4e** — gains the consequence of that finding: `GraphInterrupt`/`HaltTurn` travel *through*
>   telemetry's wrapper, so they must not count as tool errors and a gated call must not be counted
>   twice.
>
> **16 is still narrow and still correct.** The shell leg is asserted only under the jail; the
> jail-off case is stated in the docs, not claimed by a test.
>
> **Amended in the pre-build pass** that also amended `milestone6_spec.md` (see its header table):
> **22** was restated because the version first written could not hold — `harness/__init__.py`
> imports `cli` unconditionally, so no subcommand is stdlib-weight; **2a** was added so an operator
> deny is not counted as a turn failure; **7** and **8** gained the two notes that make them
> checkable at all (which token split, and which side of the `archive_conn` guard); **20** records
> that the knob is env-only. No property was weakened to fit an implementation — 22 was narrowed to
> fit a *fact about the tree*, which is the distinction that matters.

M6 = the data the harness already computes becomes durable, **decomposable**, derivable, and
publishable — without becoming a leak, and without the agent being able to edit its own record. The
invariants split five ways: **capture** (including the wall-clock decomposition), **derivation**
(one authority), **containment** (nothing sensitive escapes, and the sink is agent-unreachable),
**removability**, and **joinability** (a benchmark sweep must be able to aggregate).

Two are load-bearing and pull in opposite directions, so neither can be quietly traded away:

- **14** — telemetry is an **audit surface**, so it lives in the state dir with `past.sqlite` and
  `denials.jsonl`, not the workspace (`milestone6.md` §5a). Invariants 15–16 state exactly how much
  tamper-resistance that buys (file tools: always; shell: only under `DEEPAGENTS_JAIL=1`) so the
  claim cannot inflate.
- **4a** — wall clock **decomposes**, with every component measured at its own seam and only the
  residual inferred. A number that cannot be decomposed cannot be compared, and comparison is the
  milestone's primary purpose (§5b).

### Capture

1. **One record per completed turn.** A run of N successful turns leaves exactly N lines in
   `usage.jsonl`, each valid JSON, each carrying the same key set. **Including a `--stream` turn**,
   which bypasses the resilience layer and the HITL loop but is still a turn — a mode that silently
   produced no record would be a hole shaped exactly like a bug.

2. **A failed turn is still recorded.** A turn that raises after burning tokens produces a record
   with `failed: true` — the case an operator most wants and the one an exception path most easily
   drops. Pinned on **both** turn paths: `run_repl` and `run_batch` each carry their own general
   `except`, so a test that only exercises the REPL leaves headless — the benchmark path — unproven
   (`milestone6_spec.md` §3.2).

2a. **A governance stop is not a failure — and `failed` is derived, not measured.** Every turn
    records an `outcome` (`ok` / `denied` / `budget` / `cancelled` / `aborted` / `error`), and
    `failed` is exactly `outcome == "error"` — a property on `TurnRecord`, so the two cannot
    disagree. `turns_failed` and the `outcomes` map in the summary both fold over it, and the
    outcomes sum to `turns`.

    Four of the five non-`ok` outcomes are the harness doing what it was configured to do: the HITL
    gate halting on a deny (`hitl.HaltTurn`, `on_deny: halt`), a `--max-cost`/`--max-tokens` cap
    firing (`cost.BudgetExceeded`), an operator Ctrl-C (`KeyboardInterrupt`), and the headless
    interrupt policy failing closed (`hitl.InterruptAborted`). Conflating any of them with "the turn
    broke" puts a governance signal in the reliability column, and a benchmark sweep reading
    `turns_failed` would be measuring the operator or its own budget rather than the harness.

    **Widened during the pre-merge review, and the narrow version is why.** As first built this
    invariant covered the deny case only, so `run_turn`'s `except BaseException` still flagged the
    other three as failures — a sweep run under `--max-cost` would have reported every capped
    instance as a harness failure, which is the same error one level down. `cli._turn_outcome` is
    the single classifier; a fifth governance exception is one line there, not a fifth place to
    remember. Pinned by `test_governance_stops_are_outcomes_not_failures` (cli) and
    `test_failed_is_derived_from_outcome_and_only_error_counts` (telemetry).

3. **The sink never breaks a turn.** An unwritable sink, a full disk, or a serialization error
   degrades to a stderr warning; it never propagates into `run_turn`. *(Same rule `audit.py` and the
   `/config` dispatch already follow.)*

4. **`duration_ms` is wall-clock around the turn**, tool execution and HITL wait included — not
   model latency. Pinned by a test with a stubbed slow tool, because the field's *meaning* is what
   makes it useful or misleading, and both look identical in a JSON file.

4a. **Wall clock decomposes, and the residual is bounded.**
    `model_ms + tool_ms + retry_sleep_ms + paced_sleep_ms + hitl_wait_ms ≤ duration_ms`, and the
    residual (`duration_ms` minus those) is non-negative and small on a stubbed turn with known
    timings. `hitl_wait_ms` is a term because human think time is wall clock inside the turn that
    is neither the harness's nor the model's — omitting it would make this invariant fail the first
    time anyone runs with HITL on, which is how invariants get weakened instead of fixed
    (`milestone6_spec.md` §6).
    Every component is measured at its own seam; **only** the residual is inferred. This is the
    invariant that catches a future blocking call (a streaming path, a second limiter) silently
    disappearing into "overhead" — the failure mode a single `duration_ms` cannot expose.

4b. **Rate-limit pacing is not counted as model latency.** With a limiter configured to pace at a
    known rate, `paced_sleep_ms` is non-zero and `model_ms` excludes it. *(Without this the free-tier
    case reports ~60s "model latency" that is a property of the plan, not the model —
    `milestone6.md` §5 fork 6.)*

4c. **Retry sleep is recorded, not absorbed.** A stubbed retryable failure yields `retry_count ≥ 1`
    and a `retry_sleep_ms` matching the injected sleeps. *(`resilience.retry_call` takes `sleep=` as
    a parameter specifically so the caller owns it; `cli._invoke_resilient` passing bare `time.sleep`
    is what loses the number today.)*

4d. **A context-overflow trim is flagged.** The one-shot trim in `_invoke_resilient` sets
    `context_trimmed: true` on that turn's record — a trimmed turn is not comparable to an
    untrimmed one, and a benchmark that mixes them silently is measuring two different things.

4e. **Tool work is recorded by name.** `tool_calls` is a per-tool-name mapping (not a bare integer),
    `tool_errors` counts failures, and `tool_ms` is their summed duration — all read off
    `wrap_tool_call`'s `request.tool_call`, the same field `PauseMiddleware` uses. *(Reading
    top-level `tool_name` instead is a real bug this repo has already shipped once, in M3's pause
    gate.)*

4e-i. **The HITL gate's control flow is not tool work.** Telemetry is the *outer*
    `wrap_tool_call` wrapper (langchain composes middleware order first-is-outermost), and
    `PauseMiddleware` gates by raising langgraph's `interrupt()`. So `GraphInterrupt` — and
    `HaltTurn` on `on_deny: halt` — pass straight through telemetry's `except`. Neither may count
    as a `tool_error`, and neither may increment `tool_calls`: a gated call enters the wrapper
    twice (once to suspend, once on resume) and only the entry that reaches the tool is a call.
    *(Added by the §3.1 probe. Without it a strict-autonomy run reports double its tool work and a
    tool error for every approval — invariant 2a's mistake one level down.)*

4g. **`tool_ms` never contains human wait time.** Holds **structurally**, not by subtraction:
    `PauseMiddleware` suspends the graph rather than blocking, so the human is asked in
    `hitl.run_interrupt_loop` *after* `agent.invoke` has returned — the wait is never inside any
    `wrap_tool_call`. `milestone6_spec.md` §3.1 originally required a subtraction if telemetry
    turned out to be outer; it *is* outer, and the subtraction would have removed time that was
    never counted. Stating this as a property rather than as an implementation is what let the
    implementation change without the invariant moving.

4f. **Telemetry does not depend on the cost tracker existing.** On an unpriced model — `ollama:gemma4`,
    `pricing = "free"`, the default provider and the local-benchmark case — M1 appends **no**
    `CostTrackerMiddleware`, and telemetry must still record tokens, timings and tool mix, with
    `cost_usd` null. *(Telemetry riding on the tracker would be absent exactly where it is most
    wanted.)*

5. **No tracker ⇒ no fabricated numbers.** M1 builds `CostTrackerMiddleware` only when there is
   something to track, so a run on an unpriced model must record `cost_usd: null`, never `0.0`.
   *(The M1 rule this inherits: a cost floor is stated as a floor; a keyless run leaves the field
   NULL rather than a wrong number.)*

### Derivation — one authority

6. **`session.json` is derived, not independently accumulated.** Its token and cost totals equal the
   sum of the turn records, field by field.

7. **`session.json` agrees with `past.sqlite`.** On every field the two share
   (`input_tokens`, `output_tokens`, `cost_usd`), the file equals the `sessions` row. The row stays
   authoritative; a mismatch is a failure of the file, not of the row.

   **`input` must mean the same thing on both sides**, or this fails as arithmetic rather than as a
   bug. `cost._split_tokens` splits cache-read tokens *out of* `input`, and that split is what
   `UsageAccumulator` stores and `cli._cost_totals_for_row` writes to the row — so telemetry must
   use the same helper rather than reading `usage["input_tokens"]` raw
   (`milestone6_spec.md` §8). The test asserts the equality; this note is why it can hold at all.

8. **The summary is written after totals are final and before `session.end` workflows run.** Pinned
   by ordering, not by timing: `_finalize_session` → summary → `_pr_approval` → `run_hook`. A
   summary written after git-pr is a summary the PR cannot contain.

   **And it is written whether or not the archive is on.** In `cli.main` the `_finalize_session`
   call sits inside `if archive_conn is not None:`; the summary write must sit *outside* that guard
   at the same point in the sequence, or `DEEPAGENTS_ARCHIVE=0` silently produces no summary —
   which the failure table explicitly forbids (`milestone6_spec.md` §9, §13). The test runs the
   ordering assertion with the archive both on and off.

9. **A run with zero turns still produces a valid summary** (zeros and an empty model mix), because
   "the operator opened a session and typed `/exit`" must not yield a half-written file that the
   reader then has to special-case.

### Containment — nothing sensitive escapes

10. **No record contains prompt text, reply text, or tool arguments.** Structural, not filtered:
    the record type has no field for them. *(Same structural guard `audit.py` applies by dropping
    `context` rather than scrubbing it.)*

11. **Every string value is scrubbed, recursively**, via the shared helper extracted from
    `audit.py` — one scrub implementation, not two. A planted `sk-…` token and an
    `*_API_KEY`-shaped env value are both redacted, at any nesting depth.

    **And every dict *key*, in telemetry specifically.** `audit`'s `scrub_deep` walks values only,
    which is right for `meta` (harness-chosen labels). `tool_calls` is keyed by the tool name off
    the model's tool call, which is not a harness constant, so `telemetry._scrub_all` widens the
    traversal. Still one redaction implementation (`scrub`) — only the walk differs, and the wider
    walk lives in the module that needs it rather than in the shared one whose oracle test
    (`test_audit.py`, unedited) pins the narrower behaviour.

12. **The PR body carries aggregates only.** Built from `session.json`, which has no free-text
    field. A test plants a secret in a turn record and asserts it cannot appear in the generated
    body.

13. **git-pr degrades to today's behaviour.** Missing summary file, unreadable file, malformed JSON,
    or no `gh`/`GH_TOKEN` ⇒ the PR body is byte-identical to the current hardcoded one, and the step
    still exits 0. A telemetry failure must never be the reason a PR does not open.

14. **Every telemetry file resolves under `archive.state_dir(workspace)`, never inside the
    workspace.** Asserted on the resolved paths, not on a string prefix. This is the fork-1 decision
    (§5a) in checkable form: telemetry is an audit surface, so the audited party must not be able to
    address it.

15. **The agent's file tools cannot reach the sink.** A `write_file`/`read_file` aimed at the
    resolved `usage.jsonl` path is refused by the path guard, exactly as for
    `<state-dir>/denials.jsonl` (M4 slice D). *(This is the leg that holds unconditionally.)*

16. **The shell leg is claimed only under the jail.** With `DEEPAGENTS_JAIL=1` the shell tool cannot
    see the state dir (M4 invariant 17a) and therefore cannot edit telemetry; **with the jail off it
    can**, by absolute path. The test asserts the jail-on case and the docs state the jail-off case
    plainly — an invariant that overclaims is worse than one that is narrow, which is the lesson M4
    slice H already paid for.

17. **Telemetry survives an ephemeral run.** Under `-Ephemeral` the workspace copy is discarded on
    close while the state dir is keyed to the real workspace path; a run's `usage.jsonl` and
    `session.json` must still be there afterwards. *(The second defect of the original workspace
    placement — evidence that evaporates in the mode most likely to be used for risky runs.)*

18. **No telemetry file ever enters the git index.** Structural under invariant 14 (the sink is
    outside the commit tree entirely), so this needs no git-pr exclusion rule to hold — and the test
    asserts the *absence of the need*: a run with telemetry on leaves `git status --porcelain`
    identical to a run with it off. *(Regression guard: M4 already had to stop a masked `.env` from
    being committed; the same class of mistake writes telemetry into a PR diff.)*

19. **The in-workspace state-dir fallback is caught, not re-checked.** `state_dir()` falls back to
    `<workspace>/.deepagents` when `DEEPAGENTS_STATE_DIR` is unset, which would put the sink back
    inside the mount. Telemetry adds **no** check of its own — `harness doctor` already errors when
    an in-container state dir resolves inside the workspace, and the test asserts telemetry inherits
    that path rather than growing a second, divergent guard.

### Removability

20. **`DEEPAGENTS_TELEMETRY=0` ⇒ Milestone 5.1, byte-for-byte.** No sink file created, no summary,
    no PR block, no middleware appended, no new stderr line. *(The contract every milestone since M1
    has kept.)*

    The env var is the whole off switch: the knob is a registry field with `profile_key=None` and no
    CLI flag, the same shape `mask_enabled` and `headless` already have
    (`milestone6_spec.md` §7). So this invariant is checked exactly one way, and `.env` —
    which reaches the container through `--env-file` — is where an operator sets it. Every parameter
    the milestone adds to an existing signature defaults to the inert value (§15.1), which is what
    makes "no middleware appended" structural instead of a thing the test has to police.

21. **Telemetry adds no sibling import to `cost.py`.** `test_import_isolation`'s acyclic guard still
    passes: `telemetry.py` may import `harness.scrub` (the leaf module the scrub moves into — see
    `milestone6_spec.md` §1) and nothing else from the package; `cli.py` feeds it, exactly as it
    feeds `archive.py`.

    **One lazy exception, inside a function, on the CLI path only.** `telemetry_main`'s
    `--state-dir` default imports `harness.archive` *inside* `_resolve_state_dir` so it can call
    `archive.state_dir` rather than re-deriving the fallback. The module-level import profile — the
    thing the test asserts — is unchanged, and the alternative would give the repo two definitions
    of where state lives, which is the drift the one-authority rule exists to prevent.

22. **`harness telemetry` needs no key, no network, no model — and no runtime stack.** Asserted
    three ways: `harness.telemetry` imports nothing from the package but `harness.scrub` (so the
    module is stdlib-weight); `entry.dispatch` imports it lazily, inside the branch; and
    `tests/test_import_isolation.py` asserts `harness.telemetry` loads without pulling
    `cli`/`agent`/deepagents/dotenv, the same guard `harness.entry` / `config_cli` / `doctor` carry.

    **Restated once, in each direction, and the history is the point.** The version first written
    here said "routes through `dispatch` without importing `cli.py`", and the pre-build pass
    narrowed it to "adds no import cost that `config`/`doctor` do not already pay" — because at that
    time `harness/__init__.py` did `from harness.cli import main` **unconditionally**, so no
    subcommand was stdlib-weight. That narrowing was correct for the tree as it stood. **M5 §0.1 F6
    then landed on `main`** (PR #44: a lazy `__init__.__getattr__` plus the stdlib-only
    `harness/entry.py`), removing the fact the narrowing was fitted to, so the strong form is now
    both achievable and checked. The rule the two edits share: an invariant tracks what the tree can
    actually hold, and it moves when the tree moves — never to fit an implementation that fell
    short. Compare invariant 16.

### Joinability (a sweep must be able to aggregate)

23. **A headless run's stdout JSON joins to the ledger without parsing stderr.** `_batch_payload`
    carries `run_id` (the `past.sqlite` key) and the `session.json` path — not only `thread_id`,
    which repeats across resumes and is therefore not a key. *(This is the difference between
    telemetry you can aggregate over 300 benchmark instances and telemetry you open one file at a
    time.)*

24. **`topic` is on every record and on the session row.** A sweep passes `--topic <instance_id>`;
    the label must survive into each turn record, `session.json`, and the archive row, so all three
    join on it. *(No new mechanism — `--topic` already lands on the `past.sqlite` row; the invariant
    is that telemetry does not drop it.)*

25. **Records are append-only and one run's records are contiguous by `run_id`.** Two concurrent
    runs against the same state dir (a parallel sweep) must not interleave into an unparseable file:
    each line is independently valid JSON and carries its own `run_id`, so separation is by field,
    never by file position.

### What is deliberately *not* invariant here

- **Metric accuracy against an external oracle.** There is no second source for "what this run
  cost"; the invariants pin internal consistency (records ⇒ summary ⇒ ledger row) and leave absolute
  accuracy to M1's already-tested pricing math.
- **The deferred §8 metrics** — routing accuracy, token-reduction ratio, session success rate, TTFT.
  Each is blocked on a feature that does not exist (router, compression, post-hoc GitHub state,
  streaming). They get invariants when they get data sources, not before. **TTFT especially:** the
  harness does not stream, so there is no first-token event to time. `model_ms` is full-response
  latency and no doc may call it TTFT.
- **Benchmark correctness.** Telemetry records cost and effort; whether a SWE-bench instance was
  *resolved* is decided by the benchmark's own `FAIL_TO_PASS`/`PASS_TO_PASS` evaluation against the
  produced diff (§5b). A harness that scored itself would be marking its own homework, so no
  invariant here asserts anything about task success.
