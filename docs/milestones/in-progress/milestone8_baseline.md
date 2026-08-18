# Milestone 8 — Gold-set baseline record (B5)

> **Evidence, not guidance.** Same discipline as `milestone4_manual_verification.md`: this file
> records what a green sweep looked like, on what hardware, against which model tag, under which
> bounds. Without it the first regression has nothing to be a regression *from*.
>
> **A number here is not a score to beat.** `milestone8.md` §9 makes scoring a hard non-goal for the
> harness; the pass/fail column below was produced by applying each prediction to a fresh checkout
> of the instance base and running its own `fail_to_pass` / `pass_to_pass` commands **by hand**, the
> way an official scorer would. No such scorer ships in `harness/`.

---

## 1. Conditions

| | |
|---|---|
| Date | 2026-08-18 |
| Host | Windows 11 (10.0.26200), Docker 29.6.2 (Docker Desktop) |
| Model | `ollama:gemma4:harnesstest1` (gemma4 8B, Q4_K_M, ~9 GB) |
| `num_ctx` | 32768 (`providers/ollama/models/gemma4-harnesstest1.toml`) |
| Dataset | `benchmarks/gold/gold.jsonl`, 5 instances |
| Bounds | `--max-steps 120 --max-seconds 900 --max-turns 1` |
| Command | `python -m harness bench run ../../benchmarks/gold/gold.jsonl --out <dir> --max-steps 120 --max-seconds 900` |
| Telemetry | on (the driver forces `DEEPAGENTS_TELEMETRY=1`) |
| Git lifecycle | off (`DEEPAGENTS_WORKFLOWS_DIR` pointed at a nonexistent path) |
| Workspace seeding | off (`SEED_WORKSPACE=0` — see §4.2) |
| NetJail | off |

**Re-baseline whenever any row above changes.** A different model tag, a different `num_ctx`, or a
different step bound produces a number that is not comparable to this one — §4.1 is the measured
demonstration of exactly that, on the same harness build within one hour.

---

## 2. Result — 4 / 5 resolved

| # | Instance | Patch | `fail_to_pass` | `pass_to_pass` | Full suite | Wall |
|---|---|---|---|---|---|---|
| 1 | `gold-001-off-by-one` | applied | ✅ | ✅ | ✅ | 53.9s |
| 2 | `gold-002-hidden-caller` | applied | ✅ | ✅ | ✅ | 62.3s |
| 3 | `gold-003-read-the-failure` | applied | ✅ | ✅ | ✅ | 94.2s |
| 4 | `gold-004-regression-trap` | **empty** | — | — | — | 22.8s |
| 5 | `gold-005-new-module` | applied | ✅ | ✅ | ✅ | 112.3s |

Every produced patch **applied cleanly** to a fresh checkout of its base and left the whole instance
suite green. Instance 5 is the load-bearing one twice over: it is a real task shape *and* the
regression test for this milestone's most likely silent defect, since without `git add -A -N` its
fix — a brand-new module — would have come out as an empty patch.

`bench show`:

```
instances      5
empty patches  1   <-- nothing to score
outcomes       ok=5
stopped by     -
errors         0
wall clock     345.5s (container launch 88.4s, harness 257.1s)
turn time      model 254.6s, tool 1.9s, retry_sleep 0.0s, paced_sleep 0.0s, hitl_wait 0.0s, residual 0.7s
cost           not priced (free local model)
models         ollama:gemma4:harnesstest1
```

### 2.1 The one failure, and what it is *not*

`gold-004-regression-trap` made **zero tool calls** (`tool_calls: {}`, 7.6k input tokens, 6.2s of
model time) and returned prose instead of editing anything. That is a model behaviour, not a harness
one, and the ledger is what says so: a harness that failed to expose its tools would show the same
empty patch, and the tool-call column is the only thing that tells the two apart. Recorded as an
observation rather than fixed — tuning a prompt until the number goes up is how a gold set stops
measuring anything.

The instance itself is verified sound: its trap works. Applying the *tempting* minimal fix (clear the
whole cache on eviction) makes `test_evicted_key_is_really_gone` pass and breaks
`test_recent_keys_survive_eviction`, so an agent that stops at the first green scores zero — measured,
not assumed.

---

## 3. The M6 join — exercised, and it holds

`milestone8.md` §6 predicted this would surface problems, because several M6 fields had never been
read by anything. It did surface two (§4), but the decomposition itself came out clean:

* **`run_id` is the join key** and it worked; every instance's `runs.jsonl` row carries its telemetry.
* **The residual is 0.7s across 257s of harness time — 0.3%.** M6 invariant 4a exists to catch a
  blocking call vanishing into "overhead"; on five instances there is nothing hiding there.
* **`cost_usd` stayed `null`** end to end and `bench show` prints "not priced" rather than `$0.00`.
  This is the first time M6's null ≠ 0.0 rule has been exercised by a consumer.
* **`model_ms` is 99% of harness time** on a local 8B model, which is the expected shape and makes
  the tool-time column a usable signal rather than noise.

One number needed splitting that the plan did not anticipate: **345.5s of driver wall clock against
257.1s the harness measured.** The 88.4s difference is container start-up and teardown — five
containers at ~18s each. It is real time a sweep spends and the harness structurally cannot see, so
`bench show` reports it as its own column rather than folding it into a residual, where it would have
looked like an unexplained gap in the decomposition.

---

## 4. What the first sweeps found

Both were found by *running* the thing, not by review. Both are fixed and pinned by tests.

### 4.1 The bound was the measurement (`--max-steps 40`)

The first full sweep, at `--max-steps 40`, scored **2/5** — and four of the five instances recorded
`outcome: stopped`, `stop_reason: steps`. The sweep was reporting the bound, not the harness.
`milestone8.md` §8 names this failure mode in advance ("a sweep that is mostly `stopped/steps` is
reporting the bound"), and the run is the demonstration that `stop_reason` makes it legible instead
of leaving five ambiguous zeros:

| Instance | 40 steps | 120 steps |
|---|---|---|
| `gold-001-off-by-one` | stopped/steps, patch applied but incomplete | ok, resolved |
| `gold-002-hidden-caller` | stopped/steps | ok, resolved |
| `gold-003-read-the-failure` | stopped/steps, **empty patch** | ok, resolved |
| `gold-004-regression-trap` | ok, empty patch (no tool calls) | unchanged |
| `gold-005-new-module` | stopped/steps, patch applied, target not fixed | ok, resolved |

**120 is the recorded bound, not a tuned one:** a local 8B model spends steps liberally (`execute` to
run the suite, re-read, edit, re-run), and the instances at 120 finished on their own — none of the
resolved four hit the ceiling. Raise it, don't lower it, if a future model needs more.

### 4.2 Three harness files were in every prediction

The first end-to-end run put `environment.yml`, `.gitignore` and `scripts/run-in-env.sh` into every
patch. `run-docker`'s `seed_workspace` writes them into any workspace missing them — correct for an
interactive session, wrong for a benchmark instance, which must be exactly what its dataset says it
is. A scorer would have been handed three harness files alongside the fix.

Fixed with a `SEED_WORKSPACE=0` knob in **both** launchers (parity-guarded), which the driver sets.
An instance that needs a conda env ships its own `environment.yml` in its commit.

### 4.3 The Windows launcher swallowed every exit code

`run-docker.ps1` ended with `& docker @dockerArgs` inside a `try/finally` and never re-raised
`$LASTEXITCODE`, so the script always exited 0. On the 40-step sweep, four instances the step bound
stopped — harness exit **43** — reached the driver as a clean **0**. `run-docker.sh` has always ended
`exit $?`; the pair had drifted, and nothing caught it because no consumer had ever read the exit
code before.

Two fixes, deliberately both: the launcher now propagates the code, and `runs.jsonl` records
**`exit_code`** (what the driver observed) alongside **`harness_exit_code`** (what the harness
reported on its own JSON). Carrying only one of the two is what made the drift invisible.

---

## 5. Reproducing this

```bash
cd deepagent-image
./scripts/build.sh                     # the bounds must exist inside the image
ollama serve                           # the shipped default provider
cd project
python -m harness bench run ../../benchmarks/gold/gold.jsonl \
    --out /tmp/gold-run --max-steps 120 --max-seconds 900
python -m harness bench show --out /tmp/gold-run
```

Interrupting and re-running resumes; both output files are append-only and flushed per instance.

The pass/fail column in §2 is **not** produced by the harness. It was obtained by copying each
instance, `git apply`-ing its prediction, and running that instance's own `fail_to_pass` /
`pass_to_pass` commands. Tiers 2 and 3 hand that step to the benchmark's official evaluation harness,
which is the whole reason `predictions.jsonl` carries exactly the three official keys and nothing
else.

---

## 6. Reconfirmation after the de-nest fix (2026-08-18) — 5/5

§1's dataset was fixed after this baseline was recorded (`milestone8.md` §0.1 items 20-21): the five
instances had been committed as bare gitlinks with no tracked content, unusable on a fresh clone.
Same conditions as §1 otherwise (same model tag, same bounds, same command), re-run once the fix
landed to confirm the driver still produces valid, scorable patches against the now-real dataset —
not a required re-baseline by §1's own rule (the dataset's *content* didn't change, only its repo
shape), but the one thing the fix's own PR test plan flagged as not directly re-verified.

```
instances      5
empty patches  0
outcomes       ok=5
stopped by     -
errors         0
wall clock     380.0s (container launch 75.6s, harness 304.4s)
turn time      model 302.7s, tool 1.0s, retry_sleep 0.0s, paced_sleep 0.0s, hitl_wait 0.0s, residual 0.7s
cost           not priced (free local model)
models         ollama:gemma4:harnesstest1
```

Scored the same way as §2 (fresh copy, `git apply --check` then apply, run the pinned
`fail_to_pass`/`pass_to_pass` commands): **5/5 resolved**, including `gold-004-regression-trap`,
which made zero tool calls in the §2 run and produced a real patch this time. Consistent with §2.1's
own framing — that was already recorded as model behavior, not a harness defect, and this run is the
confirmation: same instance, same bounds, same model tag, different outcome, nothing in the harness
changed between the two. §2's table stays the recorded baseline; this is corroborating evidence the
fix didn't regress the sweep, not a replacement for it.
