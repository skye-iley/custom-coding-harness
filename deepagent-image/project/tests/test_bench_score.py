"""Tests for harness/bench/score.py — the UNOFFICIAL local diagnostic scorer.

Host tier: stdlib + a real `git` binary + `pytest` importable by this
interpreter (the same requirement `test_gold_set.py` already has for running
`fail_to_pass`/`pass_to_pass` commands directly). Skips when git is absent.

This module is explicitly **not** the milestone 8 contract (§9 "no bespoke
scorer, ever" is about the official number). These tests exist to keep the
diagnostic honest for its own stated purpose: telling apart "the patch didn't
even apply" from "the patch applied but didn't fix the bug" from "resolved" —
never to assert a pass rate against anything comparable outside this repo.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from _bootstrap import _load

patch_mod = _load("harness.bench.patch")
driver = _load("harness.bench.driver")
dataset_mod = _load("harness.bench.dataset")
score_mod = _load("harness.bench.score")

pytestmark = pytest.mark.skipif(
    not patch_mod.git_available(), reason="no git binary on PATH"
)

# Same fallback `test_gold_set.py` uses: tests/ -> project/ -> deepagent-image/
# -> repo root on a host checkout; fewer ancestors in-container, where
# benchmarks/ is never COPYed in and the skipif below does its job.
_FILE_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT = _FILE_PARENTS[3] if len(_FILE_PARENTS) > 3 else _FILE_PARENTS[-1]
_GOLD_DATASET = _REPO_ROOT / "benchmarks" / "gold" / "gold.jsonl"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _seed_instance(tmp_path: Path) -> tuple:
    """A tiny buggy project + its dataset Instance, gold-set-style: the
    instance directory is plain files, not its own repo."""
    ws = tmp_path / "gold" / "001"
    ws.mkdir(parents=True)
    # Several unchanged context lines around the one that's wrong -- a 1-line
    # file with 0 lines of context does not reproduce the regression this
    # fixture exists to catch (see test_a_correct_patch_scores_resolved's
    # docstring): git apply's Windows text=True stdin corruption only broke
    # real gold-set patches, which carry multi-line context, not this file's
    # original 2-line shape.
    (ws / "buggy.py").write_text(
        "def add(a, b):\n"
        "    if a is None or b is None:\n"
        "        raise TypeError(\"a and b must not be None\")\n"
        "    return a - b\n"
    )
    (ws / "test_buggy.py").write_text(
        "from buggy import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    (ws / "test_other.py").write_text("def test_always_passes():\n    assert True\n")

    dataset_path = tmp_path / "gold" / "gold.jsonl"
    instance = dataset_mod.Instance(
        instance_id="gold-001",
        workspace=str(ws),
        task_prompt="fix add()",
        fail_to_pass=("pytest test_buggy.py::test_add",),
        pass_to_pass=("pytest test_other.py",),
    )
    return instance, dataset_path


def _build_fix_patch(instance, dataset_path: Path, tmp_path: Path) -> str:
    """A real patch, extracted the same way the driver does: prepare a scratch
    copy, fix the bug, diff against the base commit `prepare_workspace` made."""
    scratch = tmp_path / "authoring-scratch"
    source = instance.resolve_workspace(dataset_path)
    driver.prepare_workspace(source, scratch)
    base = patch_mod.resolve_base(scratch)
    (scratch / "buggy.py").write_text(
        "def add(a, b):\n"
        "    if a is None or b is None:\n"
        "        raise TypeError(\"a and b must not be None\")\n"
        "    return a + b\n"
    )
    patch = patch_mod.extract_patch(scratch, base)
    driver.remove_tree(scratch)
    return patch


def test_a_correct_patch_scores_resolved(tmp_path):
    """Also the regression guard for a real bug: on Windows,
    `subprocess.run(..., text=True, input=patch)` translates every `\\n` in the
    patch to `\\r\\n` on write to the child's stdin, so `git apply` silently
    failed to match multi-line context against the LF-only working tree --
    reported identically to a genuinely broken patch. Caught by running a real
    sweep's gold-001 patch, not by review or a 2-line synthetic fixture (which
    has too little context to reproduce it). `_apply_patch` now passes
    `input=patch.encode("utf-8")`."""
    instance, dataset_path = _seed_instance(tmp_path)
    patch = _build_fix_patch(instance, dataset_path, tmp_path)

    row = score_mod.score_instance(instance, patch, dataset_path, tmp_path / "score-scratch")

    assert row["applied"] is True
    assert row["apply_error"] is None
    assert row["resolved"] is True
    assert row["fail_to_pass"] == [
        {"cmd": "pytest test_buggy.py::test_add", "passed": True, "tail": row["fail_to_pass"][0]["tail"]}
    ]
    assert all(c["passed"] for c in row["pass_to_pass"])


def test_an_empty_patch_scores_unresolved_not_a_crash(tmp_path):
    instance, dataset_path = _seed_instance(tmp_path)

    row = score_mod.score_instance(instance, "", dataset_path, tmp_path / "score-scratch")

    assert row["patch_empty"] is True
    assert row["applied"] is True  # an empty patch is a no-op apply, not a failure to apply
    assert row["resolved"] is False  # the seeded bug is still there
    assert row["fail_to_pass"][0]["passed"] is False


def test_a_patch_that_does_not_apply_is_told_apart_from_a_wrong_fix(tmp_path):
    """The exact distinction the tool exists for: an extraction/harness bug
    (patch doesn't even apply) must not read the same as a model that tried
    and failed."""
    instance, dataset_path = _seed_instance(tmp_path)
    garbage = "not a valid unified diff\n"

    row = score_mod.score_instance(instance, garbage, dataset_path, tmp_path / "score-scratch")

    assert row["applied"] is False
    assert row["apply_error"]
    assert row["resolved"] is False
    # No tests were run against an unpatched tree pretending to be scored -
    # apply failure short-circuits before fail_to_pass/pass_to_pass.
    assert row["fail_to_pass"] == []


def test_an_instance_with_no_fail_to_pass_commands_is_unscored_not_failed(tmp_path):
    instance, dataset_path = _seed_instance(tmp_path)
    bare = dataset_mod.Instance(
        instance_id="gold-002", workspace=instance.workspace, task_prompt="x",
    )
    patch = _build_fix_patch(instance, dataset_path, tmp_path)

    row = score_mod.score_instance(bare, patch, dataset_path, tmp_path / "score-scratch")

    assert row["applied"] is True
    assert row["resolved"] is None


def test_score_sweep_writes_scores_jsonl_beside_the_official_files(tmp_path):
    instance, dataset_path = _seed_instance(tmp_path)
    dataset_path.write_text(json.dumps({
        "instance_id": instance.instance_id,
        "workspace": instance.workspace,
        "task_prompt": instance.task_prompt,
        "fail_to_pass": list(instance.fail_to_pass),
        "pass_to_pass": list(instance.pass_to_pass),
    }) + "\n")
    patch = _build_fix_patch(instance, dataset_path, tmp_path)

    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / driver.PREDICTIONS_FILE).write_text(json.dumps({
        "instance_id": instance.instance_id,
        "model_name_or_path": "ollama:gemma4",
        "model_patch": patch,
    }) + "\n")

    rc = score_mod.score_sweep(dataset_path, run_dir)

    assert rc == 0
    scores = (run_dir / score_mod.SCORES_FILE).read_text(encoding="utf-8").splitlines()
    assert len(scores) == 1
    row = json.loads(scores[0])
    assert row["instance_id"] == instance.instance_id
    assert row["resolved"] is True
    # The official contract files are untouched by scoring.
    assert (run_dir / driver.PREDICTIONS_FILE).exists()
    assert not (run_dir / driver.RUNS_FILE).exists()


@pytest.mark.skipif(not _GOLD_DATASET.is_file(), reason="benchmarks/gold is absent")
def test_a_real_gold_set_patch_applies_windows_stdin_regression(tmp_path):
    """The regression this exists to catch would NOT reproduce on the
    synthetic fixtures above (2-4 lines, tried both) — only the real
    gold-001 instance and its real fix reliably triggered
    `text=True`'s Windows stdin newline-translation corruption
    (`\\n` -> `\\r\\n` on write, silently breaking every multi-line-context
    `git apply` match). Whatever about the real file/patch made it
    reproducible and a small synthetic one not is not fully understood; using
    the real fixture is what actually pins the bug rather than a fixture that
    merely looks similar. `_apply_patch` now sends `input=` as bytes.
    """
    instances = dataset_mod.load_dataset(_GOLD_DATASET)
    instance = next(i for i in instances if i.instance_id == "gold-001-off-by-one")

    scratch = tmp_path / "authoring"
    source = instance.resolve_workspace(_GOLD_DATASET)
    driver.prepare_workspace(source, scratch)
    base = patch_mod.resolve_base(scratch)
    buggy = (scratch / "paginate.py").read_text()
    fixed = buggy.replace("start + size + 1", "start + size")
    assert fixed != buggy, "gold-001's known off-by-one text changed -- update this test"
    (scratch / "paginate.py").write_text(fixed)
    patch = patch_mod.extract_patch(scratch, base)
    driver.remove_tree(scratch)

    row = score_mod.score_instance(instance, patch, _GOLD_DATASET, tmp_path / "score-scratch")
    assert row["applied"] is True, row["apply_error"]
    assert row["resolved"] is True
