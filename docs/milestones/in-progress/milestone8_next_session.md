# Milestone 8 — next-session handoff

Written 2026-08-17, end of the session that built B1–B5. Branch
`feat/milestone8-bench-ladder`, 3 commits, clean tree, **not pushed**. The user
deliberately parked the push/PR until the work below lands.

State at handoff: 1159 passed / 14 skips, `check-parity` OK, and a real gold-set
sweep scored 4/5 on the shipped local model (`docs/milestones/in-progress/milestone8_baseline.md`).

## 0. What is verified vs. inferred

Everything in §1 was read out of git in this session and is **verified**:

```
$ git ls-files -s | grep '^160000'
160000 f388c2a8f81ec8eef5471e7bd03cbe93e57dea46 0  benchmarks/gold/001-off-by-one
160000 88c937a7c4027491e7fe05b1b840464504b8ab07 0  benchmarks/gold/002-hidden-caller
160000 6ae9fd66e26874deda1b8c75be343ff033a21429 0  benchmarks/gold/003-read-the-failure
160000 b47a7cc166304d678f9a4f6becafcc73effbab4f 0  benchmarks/gold/004-regression-trap
160000 01130a7beba1177ff7ab0a50e5cc4efb7104c5bc 0  benchmarks/gold/005-new-module
```

`git ls-files benchmarks/` returns those five gitlinks plus `benchmarks/gold/gold.jsonl`
and nothing else — i.e. **no instance file content is tracked anywhere in this repo.**
There is also no `.gitmodules`, so these are not even submodules; they are bare
gitlinks pointing at commits that exist only in the untracked `.git` dirs on this
machine. A fresh clone gets five empty directories and the set is unusable.

Instance layout on disk (checked for `001-off-by-one`; assume the rest match, but
confirm):

```
.git/  .gitignore  README.md  conftest.py  paginate.py  tests/
__pycache__/  .pytest_cache/          # run droppings, must not be tracked
```

Module paths, verified: `deepagent-image/project/harness/bench/{dataset,driver,patch,runner}.py`
and `deepagent-image/project/tests/{test_bench,test_bench_patch,test_gold_set}.py`.
Note the gold set itself lives at **repo root** `benchmarks/gold/`, outside the
project tree — so any path the driver uses to reach it crosses that boundary and
is worth re-reading rather than assuming.

**Not verified this session** (the session ran out before reading the code): how
`dataset.py`/`driver.py` currently locate an instance and whether either already
does anything with `.git`. Read those two files first — the fix in §2 depends on
what is already there, and it is possible the driver already copies the instance
to a scratch workspace in a way that makes part of §2 unnecessary.

## 1. De-nest the five `.git` dirs (blocks everything else)

The set ships broken; nothing downstream is worth doing until this is fixed.

Two viable shapes — pick one and apply it to all five:

1. **Base-as-content + driver `git init`** (recommended). Track each instance's
   working files as ordinary content. The driver, when it stages an instance into
   its scratch workspace, runs `git init` + one commit to create the base state.
   Keeps the repo free of vendored git objects and keeps the fixtures readable and
   diffable in review.
2. **Vendor the objects.** Rename `.git` to something git will track (`_git/`,
   restored by the driver). Preserves the exact base commit SHAs but puts opaque
   binary objects in the repo and makes the fixtures un-reviewable.

Steps for shape 1:

- `git rm --cached benchmarks/gold/00*` to drop the five gitlink entries.
- Remove or relocate each instance's nested `.git` (they are untracked, so nothing
  in this repo is lost — but the base commits inside them disappear with them, so
  if any of the gold answers or `gold.jsonl` reference a base SHA, capture that
  first).
- Add the working files. Make sure `__pycache__/` and `.pytest_cache/` are excluded —
  check whether the instance-level `.gitignore` covers them once the instance is no
  longer its own repo, since a nested `.gitignore` still applies but its patterns
  were written for a repo root.
- Confirm with `git ls-files benchmarks/` that real files now appear and no
  `160000` entries remain.

**Verify on a clean clone, not in place.** Clone the branch to a scratch dir and
confirm the instances are populated there — an in-place check passes trivially
because the files are already on disk.

## 2. Driver init step

`harness/bench/patch.py` extracts a prediction with `git add -A -N` + `git diff`
(the intent-to-add step is load-bearing — see `milestone8.md` §5.2: without it a
fix delivered as a new module emits an empty patch and scores zero, silently).
That requires a real repo at the instance root. Once §1 removes the nested `.git`,
the driver has to create it.

So: in whichever of `dataset.py`/`driver.py` stages an instance into the scratch
workspace, add `git init` + an initial commit of the base state, before the run
starts. Two constraints to respect:

- The git-branch / git-pr lifecycle must stay **off** during a bench run, or its
  commit swallows the diff (`milestone8.md` §5.2 again).
- The commit needs deterministic identity (fixed author/email/date, or
  `-c user.name=... -c user.email=...`), or a machine without a git identity
  configured fails at commit time and the whole sweep dies on instance 1.

Cover it with a test that **applies** the resulting patch — `git apply --check`
against a fresh base — never a substring assertion. That is the M7 §0.2 lesson
carried into `milestone8_invariants.md`'s patch-fidelity group: a substring
assertion against a serialised blob cannot tell verbatim from escaped.

## 3. CI decision on `test_gold_set.py`

`test_gold_set.py` spawns a pytest subprocess inside each instance (~22s total
observed) and depends on the fixtures existing. Both halves of that are a problem
for the pytest-only CI job: the runtime, and — until §1 lands — fixtures that are
simply absent on a fresh clone, which would make it red-by-default.

After §1 the fixtures exist, so the real question is only cost and whether a
nested pytest run behaves in that job. Decide between:

- **Run it on CI** behind an explicit marker CI enables (~110s added). Real
  coverage of the tier that the rest of M8 is built on. This was the leaning at
  the end of the session, not a settled decision.
- **Gate it off**, leaving the tier to local runs and smoke.

Either way, make the choice explicit in the marker/config rather than leaving it
implicitly collected.

## 4. Then, and only then

Push the branch and open the PR. Outward-facing, and the user asked for it to wait
until the gold set is fixed.

## Carry-forward: the three defects the sweep found

Already fixed and committed, recorded here because they generalise — all three
came out of *running* the sweep, none out of review:

- A step bound was silently retryable: `resilience.is_retryable` scans an error
  message for an embedded status code, and `--max-steps 500` makes LangGraph emit
  "Recursion limit of 500 reached".
- `run-docker.ps1` swallowed every container exit code, so instances the bound
  stopped reported a clean 0.
- The launcher's workspace seeding put three harness files into every prediction.
