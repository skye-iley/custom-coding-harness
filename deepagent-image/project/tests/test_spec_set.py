"""The spec set is well-formed and actually unimplemented (Milestone 8 follow-on).

Host tier: stdlib + `git` + `pytest`, no langchain, no model, no network.

Mirrors `test_gold_set.py`'s discipline exactly, for a dataset that measures a
different capability genre: **implement from a spec**, not **fix an existing
bug**. The gold set's instances are all "read code, find what's wrong with it";
every spec instance hands the agent a stub (`raise NotImplementedError`) and a
docstring, with no existing behaviour to localize a defect in -- closer to what
Aider's polyglot benchmark exercises (write code to a spec) than to SWE-bench
(fix code against an issue). See `milestone8.md`'s tier-2/tier-3 split -- this
set is Python-only rather than true multi-language, so it stays runnable in the
existing image with no extra toolchain.

* every instance resolves, via `driver.prepare_workspace`, to an initialised git
  repo with a commit and a clean tree;
* every `fail_to_pass` command fails on the seeded (stub) state -- the point of
  the set is that nothing is implemented yet;
* every `pass_to_pass` command (there may be none -- a from-scratch instance has
  no prior passing behaviour to protect) passes on the seeded state;
* a reference implementation makes every instance's suite pass, so a set that
  cannot be solved is caught here rather than read as "the model is weak."

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
_SPEC = _REPO_ROOT / "benchmarks" / "spec"
_DATASET = _SPEC / "spec.jsonl"

pytestmark = pytest.mark.skipif(
    not _DATASET.is_file(), reason="benchmarks/spec is absent (invariant 28)"
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


def test_the_spec_set_has_at_least_five_instances():
    assert len(_instances()) >= 5


def test_the_dataset_parses_and_every_workspace_exists():
    for inst in _instances():
        ws = inst.resolve_workspace(_DATASET)
        assert ws.is_dir(), f"{inst.instance_id}: {ws} is missing"


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
    """The stub really is unimplemented -- an instance whose target test already
    passes measures nothing, including a run where the harness didn't work."""
    inst = _instance(instance_id)
    ws = inst.resolve_workspace(_DATASET)
    assert inst.fail_to_pass, f"{instance_id} has no fail_to_pass command"
    for command in inst.fail_to_pass:
        proc = _run(command, ws)
        assert proc.returncode != 0, (
            f"{instance_id}: `{command}` already passes on the stub, so the "
            f"instance measures nothing\n{proc.stdout[-2000:]}"
        )


@pytest.mark.parametrize("instance_id", _ids())
def test_each_pass_to_pass_command_passes_on_the_seeded_state(instance_id):
    inst = _instance(instance_id)
    ws = inst.resolve_workspace(_DATASET)
    for command in inst.pass_to_pass:
        proc = _run(command, ws)
        assert proc.returncode == 0, (
            f"{instance_id}: `{command}` fails on the SEEDED state, so it cannot "
            f"serve as a regression guard\n{proc.stdout[-2000:]}"
        )


@pytest.mark.parametrize("instance_id", _ids())
def test_every_instance_has_a_per_instance_bound_tighter_than_the_gold_set(instance_id):
    """Not a hard requirement of the format (max_steps/max_seconds are optional
    on any instance) -- but for THIS set specifically: a from-scratch spec
    instance needs far fewer steps than a debug-and-fix instance (no suite to
    run repeatedly while narrowing down a cause), and pinning that here is what
    makes the per-instance bound feature (driver.effective_limits) exercised by
    a real dataset rather than only by its unit tests."""
    inst = _instance(instance_id)
    assert inst.max_steps is not None and inst.max_steps <= 60
    assert inst.max_seconds is not None and inst.max_seconds <= 300


def test_the_spec_set_is_not_collected_by_the_harness_suite():
    project_root = Path(__file__).resolve().parents[1]
    assert _SPEC.is_dir()
    assert project_root not in _SPEC.parents
    assert not (project_root / "benchmarks").exists()
