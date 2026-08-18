"""The silver set is well-formed and actually seeded (Milestone 8 follow-on).

Host tier: stdlib + `git` + `pytest`, no langchain, no model, no network.

One step up in scale from the gold set, not a leap to full mini-project size:
each instance is 4-7 files with real cross-module coupling -- an event
published under one name and subscribed under another, a delete that doesn't
propagate through a store/cache/service/api chain, a "shared" object that
turns out not to be shared across call sites. The gold set's instances are
mostly one-file localize-and-fix; these need tracing a bug through several
files that each look correct in isolation, which is closer to what a
production-sized change actually requires and is not exercised by anything
in `benchmarks/gold/`.

Same discipline as `test_gold_set.py`:

* every instance resolves, via `driver.prepare_workspace`, to an initialised git
  repo with a commit and a clean tree;
* every `fail_to_pass` command fails on the seeded (buggy) state;
* every `pass_to_pass` command passes on the seeded state -- these exist
  specifically to prove the bug is isolated to one file/layer rather than
  entangled with everything else in the instance;
* a reference fix (applied by hand once, to verify the instance, not shipped
  here) makes every instance's suite pass -- verified when each instance was
  authored; not re-verified per test run, since there is no fix to apply.

Skips when `benchmarks/` is absent, same contract as the gold set (invariant 28).
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from _bootstrap import _load

dataset_mod = _load("harness.bench.dataset")
patch_mod = _load("harness.bench.patch")
driver = _load("harness.bench.driver")

_FILE_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT = _FILE_PARENTS[3] if len(_FILE_PARENTS) > 3 else _FILE_PARENTS[-1]
_SILVER = _REPO_ROOT / "benchmarks" / "silver"
_DATASET = _SILVER / "silver.jsonl"

pytestmark = pytest.mark.skipif(
    not _DATASET.is_file(), reason="benchmarks/silver is absent (invariant 28)"
)


def _instances():
    return dataset_mod.load_dataset(_DATASET)


def _ids():
    if not _DATASET.is_file():
        return []
    return [i.instance_id for i in _instances()]


def _instance(instance_id):
    return next(i for i in _instances() if i.instance_id == instance_id)


def _run(command: str, cwd: Path):
    args = shlex.split(command)
    if args and args[0] == "pytest":
        args = [sys.executable, "-m", "pytest", *args[1:]]
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


def test_the_silver_set_has_at_least_three_instances():
    assert len(_instances()) >= 3


def test_the_dataset_parses_and_every_workspace_exists():
    for inst in _instances():
        ws = inst.resolve_workspace(_DATASET)
        assert ws.is_dir(), f"{inst.instance_id}: {ws} is missing"


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_spans_more_than_one_source_file(instance_id):
    """The whole point of the tier: a bug an agent cannot localize by reading
    a single module. Counts .py files outside tests/, so the test file itself
    doesn't inflate the count."""
    ws = _instance(instance_id).resolve_workspace(_DATASET)
    sources = [p for p in ws.glob("*.py")]
    assert len(sources) >= 3, (
        f"{instance_id}: only {len(sources)} source file(s) at the instance root "
        "-- too small to exercise cross-module tracing"
    )


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_has_no_nested_git_of_its_own(instance_id):
    ws = _instance(instance_id).resolve_workspace(_DATASET)
    assert not (ws / ".git").exists(), (
        f"{instance_id} has its own .git -- should be plain content, with the "
        "base commit created in the scratch copy instead"
    )


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_is_a_git_repo_with_a_base_commit(instance_id, tmp_path):
    if not patch_mod.git_available():
        pytest.skip("no git binary on PATH")
    ws = _instance(instance_id).resolve_workspace(_DATASET)
    scratch = driver.prepare_workspace(ws, tmp_path / instance_id)
    assert (scratch / ".git").exists(), f"{instance_id}: prepare_workspace did not create a repo"
    base = patch_mod.resolve_base(scratch)
    assert len(base) == 40


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_hands_the_agent_a_clean_tree(instance_id, tmp_path):
    if not patch_mod.git_available():
        pytest.skip("no git binary on PATH")
    ws = _instance(instance_id).resolve_workspace(_DATASET)
    scratch = driver.prepare_workspace(ws, tmp_path / instance_id)
    proc = subprocess.run(
        ["git", "-C", str(scratch), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    assert proc.stdout.strip() == "", f"{instance_id} was handed over dirty:\n{proc.stdout}"


@pytest.mark.parametrize("instance_id", _ids())
def test_each_fail_to_pass_command_actually_fails_on_the_seeded_state(instance_id):
    inst = _instance(instance_id)
    ws = inst.resolve_workspace(_DATASET)
    assert inst.fail_to_pass, f"{instance_id} has no fail_to_pass command"
    for command in inst.fail_to_pass:
        proc = _run(command, ws)
        assert proc.returncode != 0, (
            f"{instance_id}: `{command}` already passes, so the instance measures "
            f"nothing\n{proc.stdout[-2000:]}"
        )


@pytest.mark.parametrize("instance_id", _ids())
def test_each_pass_to_pass_command_passes_on_the_seeded_state(instance_id):
    """These exist to prove the bug is isolated -- an instance where the
    'control' tests also fail on the seeded state is not distinguishing a
    real fix from a lucky one."""
    inst = _instance(instance_id)
    ws = inst.resolve_workspace(_DATASET)
    assert inst.pass_to_pass, f"{instance_id} has no pass_to_pass command -- no regression guard"
    for command in inst.pass_to_pass:
        proc = _run(command, ws)
        assert proc.returncode == 0, (
            f"{instance_id}: `{command}` fails on the SEEDED state, so it cannot "
            f"serve as a regression guard\n{proc.stdout[-2000:]}"
        )


@pytest.mark.parametrize("instance_id", _ids())
def test_every_instance_carries_a_bound_at_or_above_the_gold_baseline(instance_id):
    """The counterpart to the spec set's tighter bound: a silver instance has
    more files to read before the fix is even obvious, so it is authored with
    a per-instance ceiling above the recorded gold-set baseline
    (`milestone8_baseline.md`: 120 steps / 900 seconds), exercising
    `driver.effective_limits` on the other side of the clamp from the spec
    set."""
    inst = _instance(instance_id)
    assert inst.max_steps is not None and inst.max_steps >= 120
    assert inst.max_seconds is not None and inst.max_seconds >= 900


def test_the_silver_set_is_not_collected_by_the_harness_suite():
    project_root = Path(__file__).resolve().parents[1]
    assert _SILVER.is_dir()
    assert project_root not in _SILVER.parents
    assert not (project_root / "benchmarks").exists()
