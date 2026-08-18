# Milestone 8 — self-test findings (`bench score` + `--raw-trace`, gold set)

> **Purpose of this doc.** `harness bench score` (post-B5 addition, `harness/bench/score.py`)
> plus `--raw-trace file` give a way to tell *"the harness didn't converge"* apart from
> *"the model isn't capable of this"* on the gold set, per `milestone8.md` §9's own framing. This
> record is that diagnosis, run against three fresh self-test sweeps
> (`bench-out/run-20260818-{153903-6290b0,154945-e9a1ca,174730-82d39e}`), all on
> `ollama:gemma4:harnesstest1`, all against the (now de-nested) 5-instance gold set. It exists to
> separate what is a **harness/benchmark-environment defect** (fix later, tracked here) from what is
> **model/prompt behavior** (not a bug, a measurement) — per the request that produced it.

---

## 0. Headline finding

**Every gold-set instance, in every one of the three sweeps, hits `pytest: not found` on its first
test-running command, and none of the documented recovery paths in `AGENTS.md` work either.** This
is a harness/benchmark-environment defect, not a model-capability gap: the model is doing exactly
what the prompt and `AGENTS.md` tell it to do, and every documented path is broken for a reason that
has nothing to do with reasoning quality.

For four of the five instances the model still produces a correct patch anyway — by reasoning about
the seeded bug from source, and then **asserting success it never verified**. For the fifth
(`gold-003-read-the-failure`, whose task explicitly requires reading a real pytest traceback), the
task is literally unsolvable as stated, and the instance either burns its whole step budget hunting
for a way to run tests, or gives up and asks the (headless, unreachable) user a clarifying question.

This means the 4/5 and 5/5 numbers recorded in `milestone8_baseline.md` are not currently evidence
that the agent's edit-and-verify loop works — they are evidence that this model can sometimes
one-shot a small bug from reading source alone. The verify step the gold set is supposed to exercise
does not currently run at all, in any instance, in any sweep.

---

## 1. Root cause — no working test runner reaches the agent's shell

Chain, each link confirmed by grep over the raw-trace logs and by reading the referenced source:

1. **The `runtime` Docker stage — the one `run-docker` / `harness bench run` actually uses — ships no
   pytest, deliberately.** `deepagent-image/Dockerfile:1-2`: *"runtime stage: the shippable harness
   image — NO test code, NO pytest."* Pytest is added only in the `test` stage
   (`Dockerfile:107-121`, `requirements-dev.txt`), which nothing in the bench driver builds or runs.
2. **The workspace conda env, which is where a project's own pytest would live, is never created.**
   `conda-init-workspace.sh` requires `$WS/environment.yml` and **exits 1 if it's missing**
   (`scripts/conda-init-workspace.sh:10-13`). Gold-set instances carry no `environment.yml` (correctly
   — the gold set is meant to be exactly what its own repo ships), and `SEED_WORKSPACE=0` (which the
   bench driver sets, correctly, per `milestone8_baseline.md` §4.2) means nothing seeds one either.
   Net: `conda-init-workspace` cannot run even if the agent tried it, and no `.conda/env` ever exists
   for a benchmark instance.
3. **`./scripts/run-in-env.sh`, the wrapper `AGENTS.md` recommends first, does not exist either** —
   same `SEED_WORKSPACE=0` reason.
4. **`AGENTS.md`'s "manual activation" fallback is shell-incompatible with the tool that runs it.**
   `agent.py:352,359` wraps every `execute` call as `sandbox-exec exec -- /bin/sh -c <command>`. On
   Ubuntu 24.04 `/bin/sh` is dash, which has no `source` builtin — `source /opt/conda/etc/profile.d/conda.sh`
   fails with `source: not found` regardless of whether conda itself is reachable. `AGENTS.md`
   documents `source ...`, never `. ...` (the POSIX-portable spelling), so the one fallback path it
   names is unusable through the agent's own shell tool, independent of point 2.
5. **Result: zero surviving path to a working `pytest` inside the container the benchmark actually
   runs.** Confirmed identically across all 5 instances × 3 sweeps — grep evidence below.

```
$ grep -rn "pytest: not found\|source: not found\|No module named pytest" bench-out/*/state/*/raw-trace/*.log | wc -l
# every instance, every sweep hits at least one of these
```

Representative sequence (`gold-003-read-the-failure`, `run-20260818-174730-82d39e`, calls 1–8):

```
execute: "pytest tests/"                                          -> /bin/sh: 1: pytest: not found        (127)
execute: "./scripts/run-in-env.sh python -m pytest tests/"        -> /bin/sh: 1: ./scripts/run-in-env.sh: not found (127)
execute: "source .../conda.sh && conda activate ... && python -m pytest tests/" -> /bin/sh: 1: source: not found (127)
ls "./scripts"                                                     -> Error: Path '/scripts': path_not_found
glob "*/setup.*|*env.sh"                                            -> []
execute: "python -m pytest tests/"                                 -> /opt/venv/bin/python: No module named pytest (1)
```

Every one of the four documented/expected routes fails, in this exact order, in every instance,
every sweep — this is not instance-specific bad luck.

### 1a. Why `harness bench score` still reported 4/5

`score.py`'s `_run_command` re-applies the patch and runs `fail_to_pass`/`pass_to_pass` **on the
host** (whatever Python invoked `harness bench score`), not inside the benchmark container. The
host dev venv has pytest. So scoring works even though the *agent itself*, inside its own container,
never had a way to run it. This is exactly right for what `score.py` claims to be (§9's
non-goal boundary is about a number compared to someone else's run, not about this) — but it means
the 4/5 figure is silent about the fact that the agent could not verify its own patch, which is a
capability the gold set's "read-the-failure" instance in particular was designed to exercise (its
task text: *"Run `pytest tests/` to see the failure, read the traceback... The whole suite must
pass afterwards"*).

---

## 2. Per-instance outcome across the three self-test sweeps

| Instance | 153903 | 154945 (raw-trace) | 174730 (raw-trace + score) | Category |
|---|---|---|---|---|
| `gold-001-off-by-one` | resolved (lucky) | resolved (lucky) | resolved (lucky) | model reasoned to a correct fix from source alone; **never confirmed via pytest** — see §3 |
| `gold-002-hidden-caller` | resolved (lucky) | resolved (lucky) | resolved (lucky) | same shape as 001 |
| `gold-003-read-the-failure` | **empty patch, gave up** | **empty patch, gave up (asked the unreachable user a question)** | **empty patch, stopped/steps** | **harness/benchmark defect (§1), not model capability** — task cannot be completed as literally stated |
| `gold-004-regression-trap` | resolved (lucky) | resolved (lucky) | resolved (lucky) | same shape as 001; also the instance M8 baseline already flagged as sometimes making zero tool calls — a separate, already-recorded model-behavior observation |
| `gold-005-new-module` | resolved (lucky) | resolved (lucky) | resolved (lucky) | reasoned + edited correctly; hit and **recovered from** an `edit_file` mismatch (§3) before giving up on verification the same way as 001/002/004 |

"Lucky" is doing real work in that table: in **zero** of the 15 (instance × sweep) runs did the
agent ever see a passing test result from its own tools. Every "resolved" instance ends with the
model asserting the fix should work rather than confirming it did — e.g. `gold-001`'s final message
(174730 sweep): *"If the execution environment correctly activates the workspace conda environment
... the test suite should now pass."* That is a hedge, not a verification, and `score.py`'s
after-the-fact host-side check is the only reason it happens to be true.

---

## 3. Model-capability observations (not harness bugs — recorded for completeness, not fixing)

Two things surfaced by the raw traces are model behavior, in the same spirit as
`milestone8_baseline.md` §2.1's zero-tool-call observation on `gold-004` — worth knowing, not worth
"fixing" by tuning a prompt until the number goes up:

- **`gold-003` also got stuck on a self-inflicted `edit_file` mismatch, independent of §1.** After
  finally reaching the actual bug (`wordcount.py`'s `max()` over an empty sequence — correctly
  diagnosed via `read_file`, no pytest needed for that part), its `old_string` dropped one real line
  (`ranked = counts.most_common()`) that exists in the file. `edit_file` correctly rejected the
  no-match four times in a row with an identical error, and the model retried the **byte-identical**
  `old_string` each time instead of re-reading the file to check why it didn't match — it never
  adapted. This is what actually exhausted the remaining step budget once §1's environment search
  had already spent the first half of it.
- **`gold-005` hit the same class of `edit_file` mismatch and recovered from it** — after one failed
  attempt it called `read_file` again, saw the real text, and split the edit into two smaller,
  correctly-scoped calls that both succeeded. Contrast with `gold-003`'s four identical retries: the
  same failure mode, opposite adaptation, same model, same sweep. Recorded as a capability data
  point, not a bug — this is exactly the kind of thing a benchmark ladder exists to surface.

Neither of these is actionable as a harness fix; both are here so a future reader doesn't re-derive
them from the raw traces from scratch.

---

## 4. Recommended fixes (harness/benchmark-system category — tracked, not yet applied)

In priority order, since §1 is the dominant finding:

1. **Give the benchmark container a working test runner.** The cleanest fix that preserves
   `SEED_WORKSPACE=0`'s fidelity goal (a benchmark instance is exactly what its dataset says) is for
   the **driver**, not the seeded workspace, to guarantee pytest is reachable — e.g. the bench driver
   uses (or the runtime image gains, gated) a Python that already has pytest, independent of whether
   the instance ships its own `environment.yml`. Putting it on the *dataset* (every gold instance
   ships its own `environment.yml` declaring pytest) is the alternative, but it pushes a harness
   concern onto every instance author forever, including tier-2/3 datasets nobody here controls.
2. **Fix `AGENTS.md`'s manual-activation fallback to use `.` instead of `source`**, or note explicitly
   that the `execute` tool's shell is `/bin/sh` (dash), not bash. This is a one-line, zero-risk
   correction independent of point 1, and it is currently *guaranteed* to fail for every agent that
   reaches for it, benchmark or not — this was found via the gold set but is not gold-set-specific.
3. **Consider whether `conda-init-workspace`'s hard failure on a missing `environment.yml` is the
   right error for a workspace that has no conda dependencies at all** (the gold set's instances are
   plain-stdlib Python + pytest; they need *a* test runner, not a conda env). A workspace with no
   `environment.yml` and no conda-managed dependencies has no need to fail loudly here — this is a
   secondary observation, not equally load-bearing with point 1.
4. **Re-run the 4/5-resolved baseline's claim once 1–2 land**, since the current baseline's "resolved"
   column was never actually validated by the agent's own tools in any instance — a real fix might
   change which instances need iteration vs. one-shot, which is exactly the signal the gold set exists
   to produce.

None of these are applied by this document — it is the diagnosis `milestone8.md` §9 asked
`bench score` to produce, handed off for a later session to act on.

---

## 5. Fixes applied and re-verified (2026-08-18, same session)

All four §4 recommendations landed, plus the §4.3 secondary fix:

1. **Working test runner, driver-guaranteed.** A new `bench` Dockerfile stage
   (`FROM runtime`) adds `pytest` to `/opt/venv` and nothing else — no `tests/`,
   never the shippable `deepagent-harness` runtime image. Built explicitly
   (`scripts/build.{ps1,sh} -Bench` / `BENCH=1 ./build.sh`), tagged
   `deepagent-harness-bench`. `run-docker.{ps1,sh}` now read the image to run
   from `DEEPAGENTS_IMAGE` (default `deepagent-harness`, previously a hardcoded
   literal in five places each) instead of hardcoding it, and
   `HolderRunner.build_env` (`harness/bench/runner.py`) sets it to
   `deepagent-harness-bench` **unconditionally** for every bench instance — not
   opt-in, so an instance's ability to verify its own patch can't depend on an
   operator remembering a flag. `check-parity.{sh,ps1}` gates `DEEPAGENTS_IMAGE`
   as a semantic-parity marker. Chose "driver guarantees pytest" over "every
   dataset ships its own `environment.yml`" per the operator's explicit call —
   preserves `SEED_WORKSPACE=0`'s fidelity goal and doesn't push a harness
   concern onto every future tier-2/3 dataset author.
2. **`AGENTS.md`'s `source` → `.`**, plus a note that `execute`'s shell is
   `/bin/sh` (dash). `conda-init-workspace.sh`'s own echoed activation hint
   fixed the same way.
3. **`conda-init-workspace.sh` no longer hard-fails on a missing
   `environment.yml`.** A workspace with no conda dependencies has nothing to
   activate — that's not an error. Exits 0 with a pointer to run tests directly,
   not 1 with `Missing .../environment.yml`.
4. **Re-ran the baseline.** Single-instance check on `gold-003-read-the-failure`
   (the one this doc's §0 headline names as unsolvable-as-stated): the raw trace
   now shows `pytest tests/` executing for real inside the container
   (`platform linux -- Python 3.12.3, pytest-9.1.1`, a real traceback), the model
   read the actual failure, fixed `wordcount.py`, and `bench score` confirms
   **RESOLVED** — both `fail_to_pass` and `pass_to_pass` genuinely green, not
   asserted from source-reading alone. Full 5-instance re-run:
   **4/5 resolved** (001/002/003/004), **005 now hits the `--max-steps 60`
   bound** (`outcome: stopped`, exit 43) rather than one-shotting — a real
   verify-and-iterate loop against actual pytest output costs more steps than
   the old "reason from source, assert success" path did, which is new signal
   the broken test runner was hiding, not a regression. A future baseline
   re-record should raise `--max-steps` for 005 or budget for iteration.

The zero-tool-call and `edit_file`-retry observations in §3 are model behavior,
unaffected by this fix, and still recorded there as-is.

---

## 6. Raw-trace side finding: request-side repetition, and the M7 invariant it touched

Reading the `--raw-trace file` logs from the sweeps above (§0–§5) to diagnose the pytest gap
surfaced a second, separate issue: every model call in a multi-step turn reprints the full system
prompt and full tool-schema list, even though both are static across nearly every call in a run —
pure repetition with no diagnostic value, and the dominant cost of a bench sweep's trace files.

Fixed in this session: `rawtrace.format_request_dedup`, wired into `agent.RawTraceMiddleware` in
place of the always-full `format_request`. A block is replaced by a `(unchanged from previous
call, sha256=…)` pointer only when it hashes identical to the immediately preceding call's
rendering of the same block, and falls back to a full re-render the instant either changes.
Message content, tool calls, and tool results are never touched by this.

This narrows `milestone7.md` invariant 1 ("bodies are verbatim"), so it isn't a bug fix scoped to
this doc alone — `milestone7.md` §0.3 carries the full reasoning and the amended invariant text,
and `deepagent-image/CLAUDE.md`'s raw-trace section documents the behavior. Recorded here too
because it was this session's benchmark self-test — reading real sweep output, not review — that
found it, same as every other finding in this document.
