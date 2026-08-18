"""The batch driver (Milestone 8, slice B3): `harness bench`.

Runs a pinned dataset of coding tasks through the harness, unattended, and writes
two files:

* **`predictions.jsonl`** — exactly `{"instance_id", "model_name_or_path",
  "model_patch"}` and nothing else, because anything extra risks an *official*
  scorer rejecting the file. Correctness is that scorer's judgement, never this
  driver's (`milestone8.md` §9).
* **`runs.jsonl`** — everything the harness knows: the outcome, the bound that
  stopped it, the wall-clock decomposition, per-tool call counts, tokens, cost.
  This is the first real consumer of M6 §5b, and a join that does not work is the
  milestone finding something rather than a blocker.

Both are **append-only and flushed per instance**. A sweep that dies at instance
40 of 50 must not lose 39.

Stdlib only, routed through `entry.dispatch` with a function-local import, so
`harness bench` is keyless in the strong sense: no API key, no network, no model,
and no runtime stack.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.bench import patch as patch_mod
from harness.bench.dataset import DatasetError, load_dataset, select
from harness.bench.runner import HolderRunner, Limits, RunnerError, find_repo_root

PREDICTIONS_FILE = "predictions.jsonl"
RUNS_FILE = "runs.jsonl"

# What the driver copies out of an instance directory. `.conda` is a rebuildable
# environment and can be gigabytes; `.git` is copied when the instance ships one
# (a foreign/tier-3 source might), and `_init_base_commit` creates one when it
# doesn't (a tier-1 gold instance, tracked as plain content — see §1 of
# `milestone8_next_session.md`).
SCRATCH_EXCLUDE = (".conda",)

# Deterministic identity for the base commit `_init_base_commit` makes. A machine
# with no git identity configured (a bare CI runner) must not fail the whole sweep
# at instance 1, and the SHA must not vary sweep to sweep on the same instance.
_BASE_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "deepagents-bench",
    "GIT_AUTHOR_EMAIL": "bench@deepagents.local",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "deepagents-bench",
    "GIT_COMMITTER_EMAIL": "bench@deepagents.local",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- scratch workspaces --------------------------------------------------------


def _init_base_commit(target: Path) -> None:
    """`git init` + one commit of the working tree as it stands.

    A gold-set instance ships as plain files, not its own repo (§1 of
    `milestone8_next_session.md` — a nested `.git` doesn't survive a clone as
    real content, only as a gitlink pointing nowhere). `patch.extract_patch`
    needs a real repo with a base commit to diff against, so the driver makes
    one here, in the scratch copy, never in the dataset's source tree.
    """
    env = dict(os.environ)
    env.update(_BASE_COMMIT_ENV)

    def run(args: list[str]) -> None:
        proc = subprocess.run(
            ["git", "-C", str(target), *args],
            capture_output=True, env=env, check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
            raise DatasetError(f"git {' '.join(args)} failed while initialising {target}: {detail}")

    run(["init", "-q"])
    run(["add", "-A"])
    run(["commit", "-q", "-m", "bench: base state", "--no-verify"])


def prepare_workspace(source: Path, target: Path) -> Path:
    """Copy an instance into a scratch tree the driver owns, with a base commit.

    **The driver owns the copy, not `EPHEMERAL=1`.** Ephemeral mode reverts the
    workspace *on container close*, which means a patch can only be taken from
    inside, before close — i.e. only our own harness could produce one. With the
    driver owning the tree, the workspace still exists after the process exits and
    **anything** can be diffed the same way, which is the seam a cross-harness
    tier needs (`milestone8.md` §5.3/§9).

    A source that already ships `.git` (a foreign or tier-3 instance) is copied
    as-is; one that doesn't (the tier-1 gold set) gets `.git` created here, after
    the copy, so the dataset's own tree is never touched.
    """
    source = Path(source)
    if not source.is_dir():
        raise DatasetError(f"instance workspace does not exist: {source}")
    target = Path(target)
    remove_tree(target)
    shutil.copytree(
        source, target,
        ignore=shutil.ignore_patterns(*SCRATCH_EXCLUDE),
        symlinks=True,
    )
    if not (target / ".git").exists():
        _init_base_commit(target)
    return target


def _force_writable(func, path, _exc) -> None:
    """Clear the read-only bit and retry.

    Git stores loose objects read-only, and on Windows `os.unlink` refuses a
    read-only file outright (`WinError 5`). Without this a sweep silently leaves
    a full copy of every instance behind — including its whole `.git` — which on
    a 50-instance run is how a disk fills up with no error message anywhere.
    Found by the tests, not by reasoning.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """Delete a scratch tree; True when it is gone afterwards.

    Not `ignore_errors=True`: that is what turned the Windows read-only failure
    above into silence in the first place. The caller reports what it could not
    remove rather than pretending it did.
    """
    path = Path(path)
    if not path.exists():
        return True
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_writable)
    else:  # pragma: no cover - the image is 3.12; kept for a bare host
        shutil.rmtree(path, onerror=_force_writable)
    return not path.exists()


def _discard(path: Path) -> None:
    if not remove_tree(path):
        # Loud, because the alternative is a sweep that quietly fills the disk.
        _log(f"  ! could not remove scratch workspace {path}")


# --- per-instance bounds --------------------------------------------------------


def effective_limits(instance, ceiling: Limits) -> tuple[Limits, bool]:
    """This instance's `Limits`, and whether it was clamped down to fit.

    `ceiling` is what `--max-steps`/`--max-seconds` set for the whole sweep.
    `instance.max_steps`/`max_seconds` (dataset-authored, optional) may ask for
    *less* than that -- a toy instance that should stop on a runaway loop long
    before the ceiling a bigger instance in the same sweep needs. They may never
    ask for *more*: `min()` against the ceiling, not the instance's own number,
    is what keeps invariant 7's "never unbounded" true regardless of what a
    dataset file claims. `max_turns` is never per-instance -- an instance IS one
    turn (`Limits`'s own docstring), so there is nothing to override.
    """
    steps = ceiling.max_steps if instance.max_steps is None else min(instance.max_steps, ceiling.max_steps)
    seconds = (
        ceiling.max_seconds if instance.max_seconds is None
        else min(instance.max_seconds, ceiling.max_seconds)
    )
    clamped = (
        (instance.max_steps is not None and instance.max_steps > ceiling.max_steps)
        or (instance.max_seconds is not None and instance.max_seconds > ceiling.max_seconds)
    )
    return Limits(max_steps=steps, max_seconds=seconds, max_turns=ceiling.max_turns), clamped


# --- resume --------------------------------------------------------------------


def completed_instance_ids(predictions_path: Path) -> set[str]:
    """Instance ids already written, so a re-run resumes instead of restarting.

    Tolerant of a truncated final line — a sweep killed mid-write is exactly the
    case this reads — but never of a *duplicate*: writing a second row for an id
    already present would break invariant 18.
    """
    path = Path(predictions_path)
    if not path.is_file():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a half-written last line from a killed sweep
        if isinstance(obj, dict) and obj.get("instance_id"):
            done.add(str(obj["instance_id"]))
    return done


# --- per-sweep run directories --------------------------------------------

# `--out` is a CONTAINER; each invocation of `harness bench run` gets its own
# subfolder here (predictions.jsonl, runs.jsonl, scratch/, state/ -- including
# raw-trace/ under state/<instance>/ when enabled), named so a plain string
# sort orders them oldest-to-newest. Timestamp is for a human skimming the
# directory; the hex suffix is what actually prevents a collision between two
# sweeps started in the same second.
_RUN_DIR_PREFIX = "run-"


def _new_run_dir_name() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{_RUN_DIR_PREFIX}{ts}-{secrets.token_hex(3)}"


def _existing_run_dirs(container: Path) -> list[Path]:
    if not container.is_dir():
        return []
    return sorted(
        (p for p in container.iterdir() if p.is_dir() and p.name.startswith(_RUN_DIR_PREFIX)),
        key=lambda p: p.name,
    )


def resolve_run_dir(container: Path, instances: list) -> Path:
    """Which subfolder of `container` this invocation writes into.

    The most recent existing subfolder is **reused** when it is not yet
    complete for the instances *this* invocation selected — the resume
    behaviour `completed_instance_ids` already gives one sweep, one level up:
    a sweep killed mid-way continues in the same folder rather than starting a
    fresh one next to it.

    A subfolder where every currently-selected instance is already done is
    finished business. Reusing it would make a sweep the operator explicitly
    re-ran look like a silent no-op (0 instances to do, nothing written) — so
    a **new** subfolder is created instead, matching "only continue instead of
    creating a new run if it has not completed."

    `instances` is the already-`select()`-ed list (post `--only`/`--limit`),
    not the whole dataset — a `--limit 1` run must not read as "incomplete"
    forever just because the other four were never asked for.
    """
    existing = _existing_run_dirs(container)
    if existing:
        latest = existing[-1]
        done = completed_instance_ids(latest / PREDICTIONS_FILE)
        todo = [i for i in instances if i.instance_id not in done]
        if todo:
            return latest
    return container / _new_run_dir_name()


def resolve_show_dir(path: Path) -> Path:
    """Where `bench show` reads from.

    `path` is treated as a specific run's own folder when it already looks
    like one (a `predictions.jsonl` sitting directly in it) -- true of any
    folder `resolve_run_dir` ever returned, and also of the pre-nesting flat
    layout, so an old `--out` pointed straight at a finished sweep keeps
    working unchanged. Otherwise `path` is a container: report on the most
    recent run inside it, or `path` itself if there are no runs yet (in which
    case `summarize` reports all-zero, same as it always has for an empty
    directory).
    """
    path = Path(path)
    if (path / PREDICTIONS_FILE).is_file():
        return path
    existing = _existing_run_dirs(path)
    return existing[-1] if existing else path


def _append(path: Path, row: dict) -> None:
    """One line, opened and closed per row.

    Not a held handle flushed at the end: the whole point is that a sweep killed
    at instance 40 keeps 39, and an open buffer is exactly what loses them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()


# --- the M6 join ---------------------------------------------------------------


def read_usage_records(state_dir: Path | None, run_id: str | None) -> list[dict]:
    """This run's telemetry records, keyed by **`run_id`**.

    Never `thread_id`: it repeats across resumes and is explicitly not the
    `past.sqlite` key (`cli.py`), so a driver written against it would silently
    merge two instances (invariant 19).

    Read directly rather than through `harness.telemetry` so the driver keeps its
    keyless, sibling-free import surface — the parse is three lines and the sink
    format is a stable, versioned contract.
    """
    if state_dir is None or not run_id:
        return []
    sink = Path(state_dir) / "usage.jsonl"
    if not sink.is_file():
        return []
    out: list[dict] = []
    for line in sink.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("run_id") == run_id:
            out.append(obj)
    return out


def _sum(records: list[dict], key: str) -> int:
    return sum(int(r.get(key) or 0) for r in records)


def _sum_optional(records: list[dict], key: str) -> float | None:
    """Sum the non-null values, or `None` when every record is null.

    `cost_usd` is `null` and never `0.0` on the free local model — the
    benchmark's own default case. Summing null as zero would report a $0 sweep as
    a *priced* one, which is a different claim (invariant 21).
    """
    vals = [r.get(key) for r in records if r.get(key) is not None]
    if not vals:
        return None
    return sum(float(v) for v in vals)


def build_run_row(
    instance,
    result,
    *,
    patch: str | None,
    records: list[dict],
    capabilities: frozenset[str],
    started_at: str,
    ended_at: str,
    patch_error: str | None = None,
    limits: Limits | None = None,
    limits_clamped: bool = False,
) -> dict:
    """One `runs.jsonl` row: the ledger a sweep is actually read from.

    Anything a runner cannot measure is **`null`**, never an estimate and never a
    zero — that is what `capabilities()` is for, and it is what keeps a
    cross-harness comparison honest when one runner exposes tokens and another
    does not.
    """
    payload = result.payload or {}
    has_tokens = "tokens" in capabilities
    has_cost = "cost" in capabilities
    has_tools = "tool_calls" in capabilities

    tools: dict[str, int] = {}
    if has_tools:
        for rec in records:
            for name, n in (rec.get("tool_calls") or {}).items():
                tools[name] = tools.get(name, 0) + int(n or 0)

    turn_ms = _sum(records, "duration_ms")
    components = {
        "model_ms": _sum(records, "model_ms"),
        "tool_ms": _sum(records, "tool_ms"),
        "retry_sleep_ms": _sum(records, "retry_sleep_ms"),
        "paced_sleep_ms": _sum(records, "paced_sleep_ms"),
        "hitl_wait_ms": _sum(records, "hitl_wait_ms"),
    }
    # Stored, not left for a reader to recompute. An explicit residual is
    # auditable; an implicit one is invisible, and M6 invariant 4a exists to
    # catch a blocking call quietly disappearing into "overhead".
    components["residual_ms"] = turn_ms - sum(components.values())

    # The last record's outcome is the instance's outcome: a `stopped` refusal is
    # written after the turns it refused to extend.
    outcome = records[-1].get("outcome") if records else None
    stop_reason = next(
        (r.get("stop_reason") for r in reversed(records) if r.get("stop_reason")), None
    )

    return {
        "instance_id": instance.instance_id,
        "run_id": payload.get("run_id"),
        "thread_id": payload.get("thread_id"),
        "usage_log": payload.get("usage_log"),
        "model": payload.get("model"),
        # TWO exit codes, deliberately. `exit_code` is what the driver observed
        # from the launcher process; `harness_exit_code` is what the harness
        # itself reported on the headless JSON. They should agree, and the first
        # gold-set sweep is where they did not: `run-docker.ps1` ended inside a
        # `try/finally` without re-raising `$LASTEXITCODE`, so every instance the
        # step bound stopped (harness exit 43) arrived as a clean 0. Recording
        # both is what made that visible; the launcher is fixed, and keeping both
        # is what keeps it visible if it regresses.
        "exit_code": result.exit_code,
        "harness_exit_code": payload.get("exit_code"),
        "outcome": outcome,
        "stop_reason": stop_reason,
        "turns": len(records),
        "duration_ms": round(result.duration_ms),
        "harness_duration_ms": turn_ms,
        "time": components,
        "tool_calls": (tools if has_tools else None),
        "tokens": (
            {
                "input": _sum(records, "input"),
                "output": _sum(records, "output"),
                "cache_read": _sum(records, "cache_read"),
                "cache_write": _sum(records, "cache_write"),
            }
            if has_tokens and records
            else None
        ),
        "cost_usd": (_sum_optional(records, "cost_usd") if has_cost else None),
        "patch_empty": patch_mod.is_empty(patch),
        "patch_paths": patch_mod.changed_paths(patch),
        # The bound this instance actually ran under, not just the sweep's
        # ceiling -- a per-instance dataset override (dataset.Instance.max_steps/
        # max_seconds) makes those two potentially different, and a reader of
        # runs.jsonl needs to know which one a `stopped/steps` outcome means.
        "limits": (
            {"max_steps": limits.max_steps, "max_seconds": limits.max_seconds,
             "clamped_to_ceiling": limits_clamped}
            if limits is not None else None
        ),
        "error": result.error or patch_error,
        "stderr_tail": (result.stderr_tail if result.exit_code else ""),
        "started_at": started_at,
        "ended_at": ended_at,
    }


def build_prediction_row(instance, model: str | None, patch: str | None) -> dict:
    """Exactly the three official keys, in the official order.

    Nothing else goes here, ever. Everything the harness knows belongs in
    `runs.jsonl`; an extra key risks a scorer rejecting the whole file
    (invariant 13).
    """
    return {
        "instance_id": instance.instance_id,
        "model_name_or_path": model or "unknown",
        "model_patch": patch or "",
    }


# --- the sweep -----------------------------------------------------------------


def _log(message: str) -> None:
    print(f"[bench] {message}", file=sys.stderr)


def run_sweep(
    dataset_path: Path,
    out_dir: Path,
    *,
    limits: Limits,
    runner,
    scratch_root: Path,
    only: tuple[str, ...] = (),
    limit: int | None = None,
    dry_run: bool = False,
) -> int:
    """Iterate every instance and write both files. Returns a process exit code.

    One instance's failure never aborts the sweep (invariant 16): a crashed,
    stopped or timed-out instance yields a prediction with an empty `model_patch`
    and a ledger row carrying its outcome, and the loop continues. The exit code
    reports whether the *sweep* completed, not whether the instances passed —
    scoring is not this driver's job.
    """
    instances = select(load_dataset(dataset_path), only=only, limit=limit)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = out_dir / PREDICTIONS_FILE
    runs_path = out_dir / RUNS_FILE

    done = completed_instance_ids(predictions_path)
    todo = [i for i in instances if i.instance_id not in done]
    if done:
        _log(f"resuming: {len(done)} already done, {len(todo)} to go")

    if dry_run:
        for inst in instances:
            mark = "skip (done)" if inst.instance_id in done else "run"
            eff, clamped = effective_limits(inst, limits)
            note = " (clamped to ceiling)" if clamped else ""
            _log(
                f"{mark}: {inst.instance_id} <- {inst.resolve_workspace(dataset_path)}"
                f"  [max_steps={eff.max_steps} max_seconds={eff.max_seconds:g}{note}]"
            )
        _log(
            f"dry run: {len(todo)} instance(s) would run under a ceiling of max_steps="
            f"{limits.max_steps} max_seconds={limits.max_seconds:g} "
            f"max_turns={limits.max_turns}"
        )
        return 0

    scratch_root = Path(scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    capabilities = runner.capabilities()

    for n, inst in enumerate(todo, start=1):
        _log(f"[{n}/{len(todo)}] {inst.instance_id}")
        started_at = _now_iso()
        started = time.perf_counter()
        scratch = scratch_root / inst.instance_id
        patch = None
        patch_error = None
        try:
            source = inst.resolve_workspace(dataset_path)
            prepare_workspace(source, scratch)
            base = patch_mod.resolve_base(scratch, inst.base_commit)
        except (DatasetError, patch_mod.PatchError) as exc:
            # The instance is unusable. Still writes both rows: a sweep must
            # account for every instance in the dataset exactly once, and a
            # missing row is indistinguishable from one nobody looked at.
            _discard(scratch)
            _log(f"  ! {exc}")
            result = _failed_result(exc, time.perf_counter() - started)
            _write_pair(
                predictions_path, runs_path, inst, result, None, [], capabilities,
                started_at, _now_iso(), model=None, patch_error=str(exc),
            )
            continue

        eff_limits, clamped = effective_limits(inst, limits)
        if clamped:
            _log(
                f"  ! {inst.instance_id} asked for more than the sweep ceiling "
                f"(max_steps={inst.max_steps}, max_seconds={inst.max_seconds}); "
                f"clamped to max_steps={eff_limits.max_steps} max_seconds={eff_limits.max_seconds:g}"
            )
        try:
            result = runner.invoke(scratch, inst.task_prompt, eff_limits)
        except RunnerError as exc:
            # A launcher that cannot be found is not an instance failure — every
            # later instance would fail the same way. Stop and say so.
            _discard(scratch)
            _log(f"  ! {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001 - one instance must not end a sweep
            result = _failed_result(exc, time.perf_counter() - started)

        try:
            patch = patch_mod.extract_patch(scratch, base)
        except patch_mod.PatchError as exc:
            # Loud and recorded, never degraded into an empty patch: an empty
            # patch is a valid result ("the agent changed nothing") and scores
            # zero, so hiding a broken extractor behind one is precisely the
            # confusion this milestone exists to remove.
            patch_error = str(exc)
            _log(f"  ! patch extraction failed: {exc}")

        records = read_usage_records(
            getattr(runner, "state_dir_for", lambda _ws: None)(scratch),
            (result.payload or {}).get("run_id"),
        )
        _write_pair(
            predictions_path, runs_path, inst, result, patch, records, capabilities,
            started_at, _now_iso(),
            model=(result.payload or {}).get("model"),
            patch_error=patch_error,
            limits=eff_limits, limits_clamped=clamped,
        )
        _discard(scratch)

        empty = " (EMPTY PATCH)" if patch_mod.is_empty(patch) else ""
        _log(f"  exit={result.exit_code} {result.duration_ms / 1000:.1f}s{empty}")

    _log(f"wrote {predictions_path}")
    _log(f"wrote {runs_path}")
    return 0


def _failed_result(exc: BaseException, elapsed_s: float):
    from harness.bench.runner import RunResult

    return RunResult(
        exit_code=1, payload=None, duration_ms=elapsed_s * 1000,
        error=f"{type(exc).__name__}: {exc}",
    )


def _write_pair(predictions_path, runs_path, inst, result, patch, records,
                capabilities, started_at, ended_at, *, model, patch_error,
                limits: Limits | None = None, limits_clamped: bool = False) -> None:
    _append(predictions_path, build_prediction_row(inst, model, patch))
    _append(runs_path, build_run_row(
        inst, result, patch=patch, records=records, capabilities=capabilities,
        started_at=started_at, ended_at=ended_at, patch_error=patch_error,
        limits=limits, limits_clamped=limits_clamped,
    ))


# --- `bench show` --------------------------------------------------------------


def summarize(out_dir: Path) -> dict:
    """Aggregate a finished sweep. Pure over the two files, so it is testable."""
    out_dir = Path(out_dir)
    preds = _read_jsonl(out_dir / PREDICTIONS_FILE)
    runs = _read_jsonl(out_dir / RUNS_FILE)

    outcomes: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    for row in runs:
        key = str(row.get("outcome") or "unknown")
        outcomes[key] = outcomes.get(key, 0) + 1
        if row.get("stop_reason"):
            reason = str(row["stop_reason"])
            stop_reasons[reason] = stop_reasons.get(reason, 0) + 1

    empty = sum(1 for p in preds if not (p.get("model_patch") or "").strip())
    components = {
        key: sum(int((r.get("time") or {}).get(key) or 0) for r in runs)
        for key in ("model_ms", "tool_ms", "retry_sleep_ms", "paced_sleep_ms",
                    "hitl_wait_ms", "residual_ms")
    }
    # TWO clocks, and conflating them is how ~half a sweep's time disappears.
    # `wall_ms` is what the driver timed: the whole container lifetime. `harness_ms`
    # is what the harness itself measured inside it. The difference is container
    # start-up and teardown -- real time a sweep spends and the harness structurally
    # cannot see -- so it is reported as its own number rather than folded into a
    # residual that would then look like an unexplained gap in the decomposition.
    wall = sum(int(r.get("duration_ms") or 0) for r in runs)
    harness = sum(int(r.get("harness_duration_ms") or 0) for r in runs)
    return {
        "instances": len(preds),
        "empty_patches": empty,
        "outcomes": outcomes,
        "stop_reasons": stop_reasons,
        "errors": sum(1 for r in runs if r.get("error")),
        "wall_ms": wall,
        "harness_ms": harness,
        "launch_ms": max(wall - harness, 0),
        "time": components,
        "residual_ms": components["residual_ms"],
        "cost_usd": _sum_optional(runs, "cost_usd"),
        "models": sorted({str(r.get("model")) for r in runs if r.get("model")}),
    }


def render_show(summary: dict) -> str:
    lines = [
        f"instances      {summary['instances']}",
        # Prominent and unconditional. An all-empty sweep must be LOUD -- silence
        # there is the §0 failure mode reproduced inside the instrument
        # (invariant 17).
        f"empty patches  {summary['empty_patches']}"
        + ("   <-- nothing to score" if summary["empty_patches"] else ""),
        "outcomes       "
        + (", ".join(f"{k}={v}" for k, v in summary["outcomes"].items()) or "-"),
        "stopped by     "
        + (", ".join(f"{k}={v}" for k, v in summary["stop_reasons"].items()) or "-"),
        f"errors         {summary['errors']}",
        f"wall clock     {summary['wall_ms'] / 1000:.1f}s"
        f" (container launch {summary['launch_ms'] / 1000:.1f}s,"
        f" harness {summary['harness_ms'] / 1000:.1f}s)",
        # The decomposition M6 §5b exists for, on a real dataset. `residual` is
        # the only inferred number in it, and it is printed rather than left for
        # a reader to subtract -- an explicit residual is auditable, an implicit
        # one is invisible (invariant 22).
        "turn time      " + ", ".join(
            f"{k.removesuffix('_ms')} {v / 1000:.1f}s" for k, v in summary["time"].items()
        ),
        # `null` is not `$0.00`: on the free local model nothing priced the run,
        # and printing a dollar figure would state a measurement never made.
        "cost           "
        + (f"${summary['cost_usd']:.4f}" if summary["cost_usd"] is not None
           else "not priced (free local model)"),
        "models         " + (", ".join(summary["models"]) or "-"),
    ]
    return "\n".join(lines) + "\n"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# --- argv ----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness bench",
        description="Run a pinned dataset of coding tasks through the harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a sweep")
    run.add_argument("dataset", help="path to a jsonl dataset")
    run.add_argument("--out", default="bench-out",
                     help="container directory (default: bench-out). Each invocation gets "
                          "its own run-<timestamp>-<hex> subfolder here; an incomplete one "
                          "from a prior invocation is resumed in place rather than starting "
                          "a new one, exactly like resume within a single sweep.")
    run.add_argument("--limit", type=int, default=None, help="run at most N instances")
    run.add_argument("--only", default="", help="comma-separated instance ids")
    run.add_argument("--dry-run", action="store_true", help="list what would run")
    run.add_argument("--max-steps", type=int, default=None, help="per-turn step bound (required)")
    run.add_argument("--max-seconds", type=float, default=None,
                     help="per-instance wall-clock bound (required)")
    run.add_argument("--max-turns", type=int, default=1,
                     help="turns per instance (default 1: an instance IS one turn)")
    run.add_argument("--model", default=None, help="model spec to pin across the sweep")
    run.add_argument("--net-jail", action="store_true",
                     help="run each instance under the deny-all-egress network jail")
    run.add_argument("--scratch", default=None,
                     help="where instance copies are made (default: <run-dir>/scratch)")
    run.add_argument("--raw-trace", choices=("file", "console", "both"), default=None,
                     help="M7 raw trace per instance, for troubleshooting a bad patch. "
                          "'file' lands at <run-dir>/state/<instance_id>/raw-trace/<run_id>.log "
                          "-- right beside that instance's usage.jsonl. Off by default.")

    show = sub.add_parser("show", help="summarize a completed sweep")
    show.add_argument("--out", default="bench-out",
                     help="a run's own directory, or the --out container -- the most recent "
                          "run inside it is reported")

    score = sub.add_parser(
        "score",
        help="UNOFFICIAL local diagnostic: re-apply each prediction's patch to a fresh "
             "clone and run the dataset's own fail_to_pass/pass_to_pass. Not the M8 "
             "contract -- see harness/bench/score.py.",
    )
    score.add_argument("dataset", help="path to the jsonl dataset that produced the run")
    score.add_argument("--out", default="bench-out",
                       help="a run's own directory, or the --out container -- the most "
                            "recent run inside it is scored")
    score.add_argument("--only", default="", help="comma-separated instance ids")
    return parser


def bench_main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "show":
        print(render_show(summarize(resolve_show_dir(Path(args.out)))), end="")
        return 0

    if args.command == "score":
        from harness.bench.score import score_sweep

        only = tuple(x.strip() for x in args.only.split(",") if x.strip())
        run_dir = resolve_show_dir(Path(args.out))
        return score_sweep(Path(args.dataset), run_dir, only=only)

    # Invariant 7: refuse to start without BOTH bounds. An unbounded sweep is the
    # failure mode this milestone exists to remove, and it must not be reachable
    # by forgetting a flag. The harness itself still defaults to no bound -- that
    # is a choice about interactive runs (§12 fork 1) -- so the driver is where
    # the requirement lives.
    missing = [
        name for name, value in (("--max-steps", args.max_steps),
                                 ("--max-seconds", args.max_seconds))
        if value is None
    ]
    if missing:
        parser.error(
            f"{' and '.join(missing)} required: a benchmark run must never inherit "
            "an unbounded default. LangGraph's own recursion limit is 10007, which "
            "on a free local model is no bound at all."
        )
    if args.max_steps < 1 or args.max_seconds <= 0:
        parser.error("--max-steps must be >= 1 and --max-seconds > 0")

    # .resolve() here, unconditionally -- not a style choice. HolderRunner.invoke
    # launches run-docker.{ps1,sh} with cwd=repo_root, which is generally NOT the
    # cwd this process was started from (the docs' own example runs this from
    # deepagent-image/project with `--out ../../bench-out`). A relative out_dir
    # would then mean two different things to two different processes: this one
    # resolves it against its own cwd when it does `shutil.copytree`, and the
    # launcher resolves the SAME string against repo_root when it builds
    # -WorkspacePath / STATE_HOST_DIR. Measured, not theorised: with a relative
    # --out matching the documented example, the launcher's own
    # `if (-not (Test-Path $WorkspacePath)) { New-Item ... }` auto-created an
    # EMPTY directory two levels above the repo and bind-mounted THAT -- the real,
    # populated scratch copy sat untouched one level over. Every instance's agent
    # then correctly found an empty workspace and produced an empty patch, and the
    # state dir landed the same way, invisible to `runs.jsonl`'s own join. Not a
    # model failure; a workspace that was never really there.
    out_container = Path(args.out).resolve()
    dataset_path = Path(args.dataset)
    only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    try:
        # Selected (post --only/--limit) so a subfolder's completeness is
        # judged against what THIS invocation asked for, not the whole
        # dataset -- see resolve_run_dir's docstring.
        instances = select(load_dataset(dataset_path), only=only, limit=args.limit)
    except DatasetError as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2

    out_dir = resolve_run_dir(out_container, instances)
    _log(f"run directory: {out_dir}")
    scratch_root = Path(args.scratch).resolve() if args.scratch else out_dir / "scratch"
    try:
        repo_root = find_repo_root()
    except RunnerError as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2

    runner = HolderRunner(
        repo_root=repo_root,
        model=args.model,
        net_jail=args.net_jail,
        raw_trace=args.raw_trace,
        state_root=out_dir / "state",
    )
    try:
        return run_sweep(
            dataset_path,
            out_dir,
            limits=Limits(
                max_steps=args.max_steps,
                max_seconds=args.max_seconds,
                max_turns=args.max_turns,
            ),
            runner=runner,
            scratch_root=scratch_root,
            only=only,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except DatasetError as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2
