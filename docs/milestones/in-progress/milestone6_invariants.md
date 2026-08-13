# Milestone 6 — Invariants

> Test-facing companion to `milestone6.md` (same folder). Kept **separate** while M6 is in-progress
> so these checkable properties drive testing without the planning prose around them. On completion
> this folds into `milestone6.md` as a section and the standalone file is dropped (see the milestone
> lifecycle in `docs/README.md`).
>
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

## Capture

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

## Derivation — one authority

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

## Containment — nothing sensitive escapes

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

## Removability

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

## Joinability (a sweep must be able to aggregate)

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

## What is deliberately *not* invariant here

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
