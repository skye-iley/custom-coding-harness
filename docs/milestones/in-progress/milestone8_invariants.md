# Milestone 8 — Invariants

> Test-facing companion to `milestone8.md` (same folder). Kept **separate** while M8 is in-progress
> so these checkable properties drive testing without the planning prose around them. On completion
> this folds into `milestone8.md` as a section and the standalone file is dropped (see the milestone
> lifecycle in `docs/README.md`).
>
> **Status: written before the code**, on the M6/M7 precedent. A benchmark milestone whose
> correctness is "the sweep finished and the numbers look plausible" is untestable by construction —
> a sweep that scores 0/5 because the patch extraction is broken looks exactly like a sweep that
> scores 0/5 because the model is weak. Every invariant below exists to make one of those two
> readings a test failure instead of a judgement call.

M8 = the harness becomes measurable — a pinned set of tasks runs unattended, each run yields an
artifact an *official* scorer accepts, and each run's numbers join to the ledger. The invariants
split six ways: **bounds** (nothing runs forever, and a stop is legible), **patch fidelity** (the
artifact is scorable), **sweep integrity** (a sweep is resumable and never silently partial),
**joinability** (the M6 contract actually holds), **containment & non-interference**, and
**removability**.

The rule the milestone rests on, stated once: **a measurement that cannot distinguish a harness
defect from a model limitation is not a measurement.** That is the whole reason M8 exists — the
three defects in `milestone8.md` §0 all presented as "the model got worse". Every invariant that
looks like paranoia about empty patches or misclassified outcomes is that rule applied.

## Bounds (nothing runs forever, and a stop says which bound stopped it)

> **Built (B1).** 1–6 are pinned by `tests/test_limits.py` (the arithmetic, host tier,
> injected clock), the M8 block in `tests/test_cli.py` (the classifier, the middleware, the
> exit code, and invariant 6 asserted as the *absence* of the config key), the `stopped`
> cases in `tests/test_telemetry.py`, and two `tests/test_live_model.py` cases — the tier
> that catches a bound the harness believes in and LangGraph does not honour. **7 and 7a
> are built**: 7 as part of B3 (`harness bench run`'s refusal); 7a as a post-B5 follow-on
> (per-instance dataset bounds, `harness/bench/dataset.py` + `driver.effective_limits`),
> pinned in `tests/test_bench.py`.

1. **Each bound actually terminates its own runaway.** With `--max-steps N`, a graph that would loop
   forever ends after at most `N` super-steps. With `--max-seconds T`, a run whose model calls
   exceed `T` ends at the next step boundary. With `--max-turns K`, a session ends after `K` turns.
   Each asserted independently, with the other two unset — a test that sets all three cannot tell
   which one fired.

2. **A bound stop is `stopped`, never `error`.** All three raise into `cli._turn_outcome` and record
   `OUTCOME_STOPPED`. `GraphRecursionError` in particular must not fall through to `OUTCOME_ERROR`
   — that is the defect `milestone8.md` §3 documents, and it is present in the code today.

3. **`stop_reason` distinguishes the three.** `"steps"` / `"seconds"` / `"turns"`, and `null` on
   every turn that was not stopped. A sweep must be able to ask *which* ceiling it is measuring;
   one outcome with three causes cannot answer that.

4. **A stopped turn still produces a telemetry record.** The stop happens *inside* the graph, and
   M6 writes from `run_turn`'s `finally` (`cli.py:977–985`) — so the record exists, carries the
   partial `duration_ms` and tool counts, and is not silently dropped. *(This is the first stop path
   M6 never saw; the invariant is here because "should" was doing the work in the plan.)*

5. **`--max-cost` / `--max-tokens` keep recording `budget`, not `stopped`.** The new outcome must not
   swallow the M1 caps. A sweep distinguishes "the operator's cap fired" from "the agent did not
   converge" because the two lead to opposite actions.

6. **An unset bound is absent, not infinite.** With no flag, `recursion_limit` is **not a key in the
   graph config at all**, no deadline is computed, and no counter is compared. Asserted structurally
   (the key's absence), not by passing a large number — M7 invariant 18's lesson.

7. **`harness bench run` refuses to start without a step bound and a time bound.** Exit non-zero,
   nothing written. An unbounded sweep is the failure mode this milestone removes; it must not be
   reachable by forgetting a flag.

7a. **A per-instance bound (`dataset.Instance.max_steps`/`max_seconds`) can only tighten the CLI
    bound, never loosen it.** `driver.effective_limits` computes `min(instance_value, ceiling)`
    when the instance sets a value; an instance asking for more than the ceiling is clamped, logged
    once at the point of the clamp, and the *clamped* value — never the requested one — is what
    reaches the runner and what `runs.jsonl`'s `limits` field records. This is invariant 7 restated
    one level down: a dataset file is authored data, not an operator override, so it cannot be the
    thing that makes a sweep unbounded. Pinned by
    `test_effective_limits_clamps_an_instance_asking_for_more_than_the_ceiling` and the end-to-end
    `test_a_sweep_invokes_each_instance_with_its_own_effective_limits` (`tests/test_bench.py`).

## Patch fidelity (the artifact is scorable)

> **Built (B2).** 8–12, 12a, 12b and 14 are pinned by `tests/test_bench_patch.py` (host tier,
> skips without `git`), which drives the extractor **the way the driver does** rather than
> through `--emit-patch` — that is invariant 12a holding today. The wiring and the removable
> contract are in the B2 block of `tests/test_cli.py`, and
> `test_live_model.py::test_a_real_turn_produces_a_patch_that_actually_applies` is the case a
> stub cannot substitute for. Every one asserts by **applying** the patch. **13 is not built**:
> `predictions.jsonl` belongs to the driver (B3).

8. **A new file the agent never staged appears in the patch.** The `gold-005-new-module` case, and
   the single most likely silent defect in this milestone: without `git add -A -N` the patch is
   empty, applies cleanly as a no-op, and scores 0 with a signature identical to "the model did
   nothing". Asserted by *creating an untracked file and reading it out of the patch*.

9. **The patch applies.** `git apply --check` succeeds against a **fresh copy of the instance at its
   base commit** — not against the workspace the patch came from, which would pass trivially.
   Asserted by applying, never by substring. *(The M7 §0.2 lesson: a substring assertion against a
   serialised blob cannot tell a correct artifact from a plausible-looking one. `"def paginate" in
   model_patch` is exactly that mistake.)*

10. **No harness artifact is in the patch.** No path under `.deepagents/`, `.agent_telemetry/`,
    `.conda/`, and neither `.harness-config.yaml` nor `.harness-profile.yaml`. Asserted on a
    workspace where all of them exist and are dirty, so an absent exclusion fails rather than
    passing by luck.

11. **Exclusion is by pathspec, not by editing the diff.** A unified diff filtered after the fact
    does not apply. Asserted structurally — the excluded paths never enter the diff — and by
    invariant 9 holding on a workspace with excluded content present.

12. **The patch is taken against the recorded base, while the tree is dirty.** Not `HEAD`, and not
    after any commit. A run in which the git lifecycle committed produces the *same* patch as one in
    which it did not. *(The failure mode is a `git-pr` commit emptying `git diff HEAD` — silent, and
    indistinguishable from an agent that changed nothing.)*

12a. **There is exactly one patch extractor, and the driver calls it directly.** The intent-to-add +
    pathspec + diff-against-base logic lives once, in `harness/bench/patch.py`; `--emit-patch` is a
    caller of it, not a second implementation. Asserted by driving invariants 8–12 **through the
    driver's path**, not only through the headless JSON — a sweep must not depend on the flag.
    *(Two implementations of `git add -A -N` is two chances to get invariant 8 wrong, and the one
    that gets it wrong is the one nobody is looking at. It is also the seam `milestone8.md` §9's
    cross-harness runners need: a foreign harness has no `--emit-patch` and never will.)*

12b. **The scratch workspace keeps `.git`.** Whatever the driver copies an instance with preserves
    the repo and its history, so there is a base to diff against. Asserted on a copy, not on the
    source. *(§13 item 2: true of `run-docker`'s ephemeral copier, and it transfers to the driver's
    own — the requirement outlived the code path that first satisfied it.)*

13. **`predictions.jsonl` carries exactly three keys** — `instance_id`, `model_name_or_path`,
    `model_patch` — and nothing else. Extra keys risk a scorer rejecting the file; everything else
    the harness knows belongs in `runs.jsonl`.

14. **Off is off.** Without `--emit-patch`, no `git` subprocess runs and `model_patch` is `null`
    (present, not absent — M6's convention, so a driver gets a testable null rather than a
    `KeyError` that looks like a schema change).

## Sweep integrity (never silently partial)

> **Built (B3).** 15–18 are pinned by `tests/test_bench.py` (host tier, subprocess injected):
> a killed sweep resumed with no duplicate row, one instance's failure not aborting the rest,
> the empty-patch counter, and every instance appearing in both files exactly once.

15. **A sweep resumes.** Killed at instance *k* of *n* and re-run, it skips the first *k* and
    completes the rest. Both output files are append-only and flushed per instance; nothing is
    buffered to the end.

16. **One instance's failure never aborts the sweep.** A crashed, stopped, or timed-out instance
    yields a prediction with an empty `model_patch` and a ledger row carrying its outcome, and the
    driver continues.

17. **Empty patches are counted and reported prominently.** `bench show` states how many predictions
    were empty. An all-empty sweep must be loud — silence there is exactly the §0 failure mode
    reproduced inside the instrument.

18. **Every instance in the dataset appears in both outputs, exactly once.** No dropped rows, no
    duplicates on resume.

## Joinability (the M6 contract, actually exercised)

> **Built (B3), and exercised for real.** 19–22 are pinned by `tests/test_bench.py` — including
> the fixture where two instances share a `thread_id` — and were then run against five real
> instances: `milestone8_baseline.md` §3 records a 0.3% residual, a `cost_usd` that stayed
> `null` end to end, and the one number that had to be split in two (container launch vs.
> harness time).

19. **The join key is `run_id`, never `thread_id`.** `thread_id` repeats across resumes and is
    explicitly not the `past.sqlite` key (`cli.py:2092–2097`). A driver written against it merges
    two instances silently. Asserted with a fixture in which two instances share a `thread_id`.

20. **Every `runs.jsonl` row carries the wall-clock decomposition and per-tool counts**, joined from
    `<state-dir>/usage.jsonl` — not recomputed, not estimated.

21. **`cost_usd: null` survives as null.** On the free local model — the benchmark's default case —
    a null is not summed as zero and a sweep is not reported as priced. `null` and `0.0` are
    different claims (M6's rule, first tested here).

22. **The residual is reported, not hidden.** `bench show` surfaces `duration_ms` minus the measured
    components. M6 invariant 4a exists to catch a blocking call vanishing into "overhead", and a
    50-instance sweep is the first dataset where a systematic residual is visible rather than noise.

## Containment & non-interference

> **Built.** 23 is in `tests/test_import_isolation.py`; 25 and the gold-set collection guard
> (26) are in `tests/test_bench.py` / `tests/test_gold_set.py`. 24 is unchanged from B2 — the
> flag is a diff of the workspace the agent already owns, on the existing headless-JSON
> channel, and adds no new path out of the container.

23. **The driver is keyless in the strong sense.** `harness.bench` imports without pulling `cli`,
    `agent`, deepagents, or dotenv — pinned in `tests/test_import_isolation.py` alongside `entry` /
    `doctor` / `telemetry` / `config_cli`.

24. **`--emit-patch` reads only the workspace.** It adds no new path out of the container: it is a
    diff of the tree the agent already owns, on the existing headless-JSON channel, under an
    explicit flag. Re-checked against `milestone4.md` §19 rather than asserted from intent.

25. **A bench run opens no PR and creates no branch.** With the workflows dir pointed away, neither
    `git-branch` nor `git-pr` runs — no branch, no commit, no push, no PR. A sweep of 50 instances
    must not produce 50 pull requests.

26. **The gold set is not collected by the harness suite.** `pytest tests/` neither runs nor imports
    anything under `benchmarks/`.

## Removability

> **Built.** 28 is pinned by `tests/test_gold_set.py` over the parsed AST (a docstring example
> naming the directory is not a dependency; the first draft's substring scan said otherwise).

27. **Deleting the feature reverts to M7.** Removing `harness/bench/`, `harness/limits.py`, the
    `DeadlineMiddleware`, the four `FieldSpec` entries, the `entry.dispatch` route,
    `OUTCOME_STOPPED` + `stop_reason`, the `_turn_outcome` lines, and the `model_patch` key leaves a
    harness that behaves as it did before this milestone, with no dead references.

28. **`benchmarks/` has no importer.** Deleting the directory breaks nothing in the harness or its
    tests.

## What is deliberately *not* invariant here

- **A score.** Nothing asserts a pass rate, and no test compares one sweep to another. The
  correctness of a fix is the scorer's judgement, not the harness's (`milestone8.md` §9). An
  invariant on the number would make a model upgrade a build failure.
- **Timing comparability across configurations.** M7 already established that telemetry taken with
  tracing on is not comparable to telemetry taken with it off; the same is true across step bounds,
  models and hosts. The baseline record (B5) states its conditions instead.
- **Determinism of the agent.** The instances are deterministic; the agent is not. Two sweeps of the
  same set may differ, and nothing asserts otherwise. This is why B5 records a baseline rather than
  a expected-output fixture.
- **Parallel-safety.** `--jobs` is a non-goal (§9) and nothing asserts the driver is safe to run
  concurrently. The `run_turn`-threads-`telemetry`-as-a-parameter seam (`cli.py:972–975`) keeps it
  possible; it does not make it true.
- **Anything about a runner other than `HolderRunner`.** The `Runner` protocol is declared and one
  implementation satisfies it; no invariant constrains an adapter that does not exist. Invariant 12a
  is the only thing carrying weight for the future case, and it does so by holding *today* — the
  driver's own path must work without `--emit-patch`, which is exactly what a foreign runner would
  need. A protocol asserted with one implementation asserts nothing about the second; tier 2 writes
  its own invariants.
