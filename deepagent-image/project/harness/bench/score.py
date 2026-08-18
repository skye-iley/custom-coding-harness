"""`harness bench score` — an UNOFFICIAL, local-only diagnostic scorer.

**This is not the milestone 8 contract, and it never touches it.** `milestone8.md`
§9 says "no bespoke scorer, ever" — that non-goal is about the *official* number:
correctness of a fix comes from the benchmark's own evaluation harness (SWE-bench
eval, the Aider runner), because a scorer written here would produce a figure
nobody outside this repo could compare against. `dataset.py` carries
`fail_to_pass`/`pass_to_pass` for exactly that reason — "carried, not run".

What this module is for instead: telling apart *the harness didn't converge* from
*the model isn't capable of this*, on your **own gold set**, for your **own
diagnosis**, never published or compared against anyone else's number. It reads
`predictions.jsonl` from a finished `harness bench run`, re-applies each
`model_patch` to a **fresh** clone of that instance's base state (never the
driver's post-run scratch — a stale env or leftover file there would make a
patch look like it fixed something it didn't), and runs the dataset's own
`fail_to_pass` / `pass_to_pass` commands. Output is a **separate** file,
`scores.jsonl`, sitting beside `predictions.jsonl` / `runs.jsonl` in the same run
directory — additive, never read by `bench show`, `bench run`, or anything that
feeds the official contract.

Needs `pytest` importable by whatever Python runs this (same requirement
`tests/test_gold_set.py` already has for running `fail_to_pass`/`pass_to_pass`
commands directly) — not a harness dependency, a scoring-time one.

Stdlib + `git` only, same tier as the rest of `harness/bench/`.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from harness.bench import driver
from harness.bench.dataset import DatasetError, Instance, load_dataset

SCORES_FILE = "scores.jsonl"

_STDERR_TAIL_CHARS = 4000


def _tail(text: str) -> str:
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return "…" + text[-_STDERR_TAIL_CHARS:]


def _log(message: str) -> None:
    print(f"[bench score] {message}", file=sys.stderr)


def _run_command(command: str, cwd: Path) -> tuple[bool, str]:
    """Run one `fail_to_pass`/`pass_to_pass` command, same interpreter trick
    `test_gold_set.py` uses so a bare host with no `pytest` on PATH still works
    when it's installed as a library."""
    args = shlex.split(command)
    if args and args[0] == "pytest":
        args = [sys.executable, "-m", "pytest", *args[1:]]
    try:
        proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    except OSError as exc:
        return False, f"could not run {command!r}: {exc}"
    passed = proc.returncode == 0
    tail = _tail((proc.stdout or "") + (proc.stderr or ""))
    return passed, tail


def _apply_patch(workspace: Path, patch: str) -> str | None:
    """Apply `patch` to `workspace`'s working tree. Returns an error string, or
    None on success (including the empty-patch case, which is a no-op apply).

    **`input=` is bytes, not `text=True`.** On Windows, `text=True` writes
    stdin through the platform's universal-newline translation, turning every
    `\\n` in the patch into `\\r\\n` before git ever sees it. Against an
    LF-only source file that silently fails every context-line match with no
    hint it was ever an encoding problem — `git apply` just reports "patch
    does not apply", indistinguishable from a genuinely broken patch. Measured,
    not theorised: this is exactly what turned a clean gold-set patch into
    `apply_failed` the first time this ran on Windows. `harness/bench/patch.py`
    avoids `text=True` on its git subprocess calls for the same class of
    reason (decodes bytes itself instead).
    """
    if not (patch or "").strip():
        return None
    proc = subprocess.run(
        ["git", "-C", str(workspace), "apply", "--whitespace=nowarn", "-"],
        input=patch.encode("utf-8"), capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        return detail or f"git apply exited {proc.returncode}"
    return None


def score_instance(instance: Instance, patch: str, dataset_path: Path, scratch: Path) -> dict:
    """One instance: fresh clone at base state, apply `patch`, run its tests.

    `resolved` is `None` (unscored, not failed) when the instance carries no
    `fail_to_pass` commands — the same "measures nothing" case
    `tests/test_gold_set.py` already treats as distinct from a real failure.
    """
    source = instance.resolve_workspace(dataset_path)
    driver.prepare_workspace(source, scratch)  # fresh copy + base commit, same as a real sweep

    apply_error = _apply_patch(scratch, patch)
    applied = apply_error is None

    fail_to_pass = []
    pass_to_pass = []
    if applied:
        for cmd in instance.fail_to_pass:
            passed, tail = _run_command(cmd, scratch)
            fail_to_pass.append({"cmd": cmd, "passed": passed, "tail": tail})
        for cmd in instance.pass_to_pass:
            passed, tail = _run_command(cmd, scratch)
            pass_to_pass.append({"cmd": cmd, "passed": passed, "tail": tail})

    if not applied:
        resolved = False
    elif not instance.fail_to_pass:
        resolved = None
    else:
        resolved = (
            all(c["passed"] for c in fail_to_pass)
            and all(c["passed"] for c in pass_to_pass)
        )

    return {
        "instance_id": instance.instance_id,
        "patch_empty": not (patch or "").strip(),
        "applied": applied,
        "apply_error": apply_error,
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "resolved": resolved,
    }


def _read_predictions(path: Path) -> dict[str, str]:
    """`instance_id -> model_patch`, tolerant of a truncated final line —
    same reasoning as `driver.completed_instance_ids`: a killed sweep's
    predictions file is still worth scoring as far as it got."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("instance_id"):
            out[str(obj["instance_id"])] = str(obj.get("model_patch") or "")
    return out


def score_sweep(
    dataset_path: Path,
    run_dir: Path,
    *,
    scratch_root: Path | None = None,
    only: tuple[str, ...] = (),
) -> int:
    """Score every prediction in `run_dir` against `dataset_path`. Writes
    `<run_dir>/scores.jsonl` (overwritten whole, not appended — a rescore is a
    fresh judgement, not a resume; unlike `predictions.jsonl` there is no
    expensive model call being protected here, just fast local subprocesses).
    """
    run_dir = Path(run_dir)
    predictions = _read_predictions(run_dir / driver.PREDICTIONS_FILE)
    if not predictions:
        _log(f"no predictions found in {run_dir} — run `harness bench run` first")
        return 2

    try:
        instances = load_dataset(dataset_path)
    except DatasetError as exc:
        _log(str(exc))
        return 2
    by_id = {i.instance_id: i for i in instances}

    wanted = set(only) if only else set(predictions)
    scratch_root = Path(scratch_root) if scratch_root else run_dir / "score-scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for instance_id, patch in predictions.items():
        if instance_id not in wanted:
            continue
        instance = by_id.get(instance_id)
        if instance is None:
            _log(f"  ! {instance_id}: no matching instance in {dataset_path}, skipping")
            continue
        _log(instance_id)
        scratch = scratch_root / instance_id
        try:
            row = score_instance(instance, patch, dataset_path, scratch)
        except DatasetError as exc:
            row = {
                "instance_id": instance_id, "patch_empty": not patch.strip(),
                "applied": False, "apply_error": str(exc),
                "fail_to_pass": [], "pass_to_pass": [], "resolved": False,
            }
        finally:
            driver.remove_tree(scratch)
        rows.append(row)
        mark = "RESOLVED" if row["resolved"] else ("unscored" if row["resolved"] is None else "unresolved")
        _log(f"  {mark}")

    driver.remove_tree(scratch_root)

    out_path = run_dir / SCORES_FILE
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _log(f"wrote {out_path}")
    print(render_score_summary(summarize_scores(rows)), end="")
    return 0


def summarize_scores(rows: list[dict]) -> dict:
    resolved = sum(1 for r in rows if r["resolved"] is True)
    unresolved = sum(1 for r in rows if r["resolved"] is False)
    unscored = sum(1 for r in rows if r["resolved"] is None)
    return {
        "instances": len(rows),
        "resolved": resolved,
        "unresolved": unresolved,
        "unscored": unscored,
        "apply_failed": sum(1 for r in rows if not r["applied"]),
    }


def render_score_summary(summary: dict) -> str:
    lines = [
        "--- UNOFFICIAL local score (not the M8 contract; see harness/bench/score.py) ---",
        f"instances      {summary['instances']}",
        f"resolved       {summary['resolved']}",
        f"unresolved     {summary['unresolved']}",
        f"unscored       {summary['unscored']}"
        + ("   <-- no fail_to_pass commands" if summary["unscored"] else ""),
        f"apply failed   {summary['apply_failed']}"
        + ("   <-- patch didn't apply to a fresh clone; harness/extraction bug, not the model"
           if summary["apply_failed"] else ""),
    ]
    return "\n".join(lines) + "\n"
