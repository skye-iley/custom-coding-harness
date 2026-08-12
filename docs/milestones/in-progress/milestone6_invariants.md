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
becoming a leak**. The invariants split four ways: **capture**, **derivation** (one authority),
**containment** (nothing sensitive escapes), and **removability**.

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

14. **The sink stays inside `.agent_telemetry/`**, which is git-ignored and git-pr-excluded — so no
    telemetry file is ever staged into the agent's commit. *(Regression guard: M4 already had to fix
    a masked `.env` being committed; the same class of mistake writes telemetry into a PR diff.)*

15. **The sink placement is a recorded decision, not an accident.** It is in the workspace, so the
    agent's own tools can rewrite it. The test that pins this is a *documentation* test in spirit:
    if telemetry ever needs to be tamper-evident, it moves to the state dir like M4 slice D's
    `denials.jsonl`, and this invariant changes with it.

## Removability

16. **`DEEPAGENTS_TELEMETRY=0` ⇒ Milestone 5.1, byte-for-byte.** No sink file created, no summary,
    no PR block, no middleware appended, no new stderr line. *(The contract every milestone since M1
    has kept.)*

17. **Telemetry adds no sibling import to `cost.py`.** `test_import_isolation`'s acyclic guard still
    passes: `telemetry.py` may import `harness.audit` and nothing else from the package; `cli.py`
    feeds it, exactly as it feeds `archive.py`.

18. **The keyless path stays keyless.** `harness telemetry show` routes through `dispatch` without
    importing `cli.py` — the property M5 §0.1 F6 established for `config`/`doctor`. A new
    subcommand that re-drags the runtime stack in silently undoes it.

## What is deliberately *not* invariant here

- **Metric accuracy against an external oracle.** There is no second source for "what this run
  cost"; the invariants pin internal consistency (records ⇒ summary ⇒ ledger row) and leave absolute
  accuracy to M1's already-tested pricing math.
- **The deferred §8 metrics** — routing accuracy, token-reduction ratio, session success rate, TTFT.
  Each is blocked on a feature that does not exist (router, compression, post-hoc GitHub state,
  streaming). They get invariants when they get data sources, not before.
