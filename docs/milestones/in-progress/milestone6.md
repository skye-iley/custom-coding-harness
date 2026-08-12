# Milestone 6 — Telemetry

## 0. Build status

**Planned — nothing built yet.** Branch `feat/milestone6-telemetry` carries three docs only: this
plan, `milestone6_invariants.md` (the checkable properties), and `milestone6_spec.md` (the
implementation-level doc — **build from that one**). All three unimplemented.

This doc is the plan, written before the code so the code has something to be wrong against. Every
claim below about *existing* behaviour was read off the tree at `6e0c104`; every claim about new
behaviour is a proposal until a slice lands and this §0 says so.

**A pre-build pass re-checked every seam the spec names against the tree** and found five claims
that did not hold plus five decisions left unmade. All ten are settled in `milestone6_spec.md`
(header table lists them) and the two that reach this doc are corrected in place: §3 T3's summary
placement, and §3 T1's scrub-extraction naming. The spec is the doc that changed; nothing about the
plan's scope, forks, or §5a placement argument moved.

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
