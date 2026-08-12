# Milestone 6 — Invariants

> Test-facing companion to `milestone6.md` (same folder). Kept **separate** while M6 is in-progress
> so these checkable properties drive testing without the planning prose around them. On completion
> this folds into `milestone6.md` as a section and the standalone file is dropped (see the milestone
> lifecycle in `docs/README.md`).
>
> **Status: none of these are implemented or tested yet.** They are written first, on purpose — a
> telemetry milestone whose correctness is "the numbers look about right" is untestable by
> construction, so the numbers get pinned before they exist.

M6 = the data the harness already computes becomes durable, derivable, and publishable **without
becoming a leak, and without the agent being able to edit its own record**. The invariants split
four ways: **capture**, **derivation** (one authority), **containment** (nothing sensitive escapes,
and the sink is agent-unreachable), and **removability**.

The load-bearing one is 14: telemetry is an **audit surface**, so it lives in the state dir with
`past.sqlite` and `denials.jsonl`, not in the workspace. `milestone6.md` §5a has the argument;
invariants 15–16 state exactly how much tamper-resistance that buys (file tools: always; shell:
only under `DEEPAGENTS_JAIL=1`) so the claim can't quietly inflate.

## Capture

1. **One record per completed turn.** A run of N successful turns leaves exactly N lines in
   `usage.jsonl`, each valid JSON, each carrying the same key set.

2. **A failed turn is still recorded.** A turn that raises after burning tokens produces a record
   with `failed: true` — the case an operator most wants and the one an exception path most easily
   drops.

3. **The sink never breaks a turn.** An unwritable sink, a full disk, or a serialization error
   degrades to a stderr warning; it never propagates into `run_turn`. *(Same rule `audit.py` and the
   `/config` dispatch already follow.)*

4. **`duration_ms` is wall-clock around the turn**, tool execution and HITL wait included — not
   model latency. Pinned by a test with a stubbed slow tool, because the field's *meaning* is what
   makes it useful or misleading, and both look identical in a JSON file.

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

8. **The summary is written after totals are final and before `session.end` workflows run.** Pinned
   by ordering, not by timing: `_finalize_session` → summary → `_pr_approval` → `run_hook`. A
   summary written after git-pr is a summary the PR cannot contain.

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

21. **Telemetry adds no sibling import to `cost.py`.** `test_import_isolation`'s acyclic guard still
    passes: `telemetry.py` may import `harness.audit` and nothing else from the package; `cli.py`
    feeds it, exactly as it feeds `archive.py`.

22. **The keyless path stays keyless.** `harness telemetry show` routes through `dispatch` without
    importing `cli.py` — the property M5 §0.1 F6 established for `config`/`doctor`. A new
    subcommand that re-drags the runtime stack in silently undoes it.

## What is deliberately *not* invariant here

- **Metric accuracy against an external oracle.** There is no second source for "what this run
  cost"; the invariants pin internal consistency (records ⇒ summary ⇒ ledger row) and leave absolute
  accuracy to M1's already-tested pricing math.
- **The deferred §8 metrics** — routing accuracy, token-reduction ratio, session success rate, TTFT.
  Each is blocked on a feature that does not exist (router, compression, post-hoc GitHub state,
  streaming). They get invariants when they get data sources, not before.
