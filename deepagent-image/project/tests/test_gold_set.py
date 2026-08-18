"""The gold set is well-formed and actually seeded (Milestone 8, slice B4).

Host tier: stdlib + `git` + `pytest`, no langchain, no model, no network.

A benchmark whose fixtures have silently rotted reports a pass rate over a set
nobody chose — and the failure looks exactly like a weak model, which is the
confusion this whole milestone exists to remove. So the set checks itself:

* every instance resolves, via `driver.prepare_workspace`, to an initialised git
  repo with a commit and a **clean tree** (§13 item 6 — no base commit means no
  patch, ever);
* every `fail_to_pass` command **fails** on the seeded state, or the instance
  measures nothing;
* every `pass_to_pass` command **passes**, or the instance is broken rather than
  hard.

The instances themselves are tracked as **plain files, not nested git repos**
(§0.1 item 20 of `milestone8.md` — a nested `.git` committed as ordinary content
is a dangling gitlink on a fresh clone, not a repo). `driver.prepare_workspace` is
what turns one into a repo, in a scratch copy, the same way a real sweep does —
so these two checks exercise that seam rather than assuming git history the
dataset no longer ships.

Everything here **skips** when `benchmarks/` is absent, because invariant 28 says
deleting that directory must break nothing.
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

# tests/ -> project/ -> deepagent-image/ -> repo root (on a host checkout). Inside
# the built image, tests/ lands at /project/tests -- only two ancestors, not
# three -- because benchmarks/ is never COPYed into the image (CLAUDE.md
# "Benchmark sweeps"). Indexing `parents[3]` there used to crash collection
# outright (IndexError, then -- once caught -- `.root` is a `str`, not a `Path`,
# so `/ "benchmarks"` raised TypeError right after). Falling back to the topmost
# `Path` in `.parents` keeps `_REPO_ROOT` an actual Path and points `_DATASET` at
# something that structurally cannot exist in-container, so the skipif below
# still does its job (invariant 28).
_FILE_PARENTS = Path(__file__).resolve().parents
_REPO_ROOT = _FILE_PARENTS[3] if len(_FILE_PARENTS) > 3 else _FILE_PARENTS[-1]
_GOLD = _REPO_ROOT / "benchmarks" / "gold"
_DATASET = _GOLD / "gold.jsonl"

pytestmark = pytest.mark.skipif(
    not _DATASET.is_file(), reason="benchmarks/gold is absent (invariant 28)"
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
    # The dataset stores literal `pytest ...` commands so the same field carries
    # straight into tiers 2 and 3. Run them through this interpreter so a bare
    # host with no `pytest` on PATH still works.
    args = shlex.split(command)
    if args and args[0] == "pytest":
        args = [sys.executable, "-m", "pytest", *args[1:]]
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


def test_the_gold_set_has_at_least_five_instances():
    # Done-when #7. Fewer than five and the set stops distinguishing loop shapes:
    # each instance exists to fail in a *different* way (§5.4).
    assert len(_instances()) >= 5


def test_the_dataset_parses_and_every_workspace_exists():
    for inst in _instances():
        ws = inst.resolve_workspace(_DATASET)
        assert ws.is_dir(), f"{inst.instance_id}: {ws} is missing"


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_has_no_nested_git_of_its_own(instance_id):
    """Regression guard for §0.1 item 20: the gold set once shipped as five bare
    gitlinks pointing at a nested `.git` that existed only on the authoring
    machine, so a fresh clone got five empty directories. Instances are tracked
    as plain files; the repo is created per-run by `driver.prepare_workspace`."""
    ws = _instance(instance_id).resolve_workspace(_DATASET)
    assert not (ws / ".git").exists(), (
        f"{instance_id} has its own .git — should be plain content, with the "
        "base commit created in the scratch copy instead"
    )


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_is_a_git_repo_with_a_base_commit(instance_id, tmp_path):
    """§13 item 6, as a standing check rather than a note.

    No base commit means nothing to diff against, which means every patch is
    empty and every instance scores zero — silently, and indistinguishably from
    a model that did nothing. Exercised through `driver.prepare_workspace`, the
    same seam a real sweep uses, since the instance itself is no longer a repo.
    """
    if not patch_mod.git_available():
        pytest.skip("no git binary on PATH")
    ws = _instance(instance_id).resolve_workspace(_DATASET)
    scratch = driver.prepare_workspace(ws, tmp_path / instance_id)
    assert (scratch / ".git").exists(), f"{instance_id}: prepare_workspace did not create a repo"
    base = patch_mod.resolve_base(scratch)
    assert len(base) == 40


@pytest.mark.parametrize("instance_id", _ids())
def test_each_instance_hands_the_agent_a_clean_tree(instance_id, tmp_path):
    """The seeded bug is *in* the commit, not sitting uncommitted beside it.

    A dirty tree at handoff would put the fixture's own leftovers into the
    extracted patch, so every prediction would carry them and the diff would stop
    describing what the agent did.
    """
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
    """The bug is really there.

    An instance whose target test already passes measures nothing: every run
    "solves" it, including a run where the harness did not work at all.
    """
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
    """The rest of the suite is green, so a broken instance is not mistaken for a
    hard one — and so `pass_to_pass` can catch a fix that breaks the module."""
    inst = _instance(instance_id)
    ws = inst.resolve_workspace(_DATASET)
    for command in inst.pass_to_pass:
        proc = _run(command, ws)
        assert proc.returncode == 0, (
            f"{instance_id}: `{command}` fails on the SEEDED state, so it cannot "
            f"serve as a regression guard\n{proc.stdout[-2000:]}"
        )


def test_the_gold_set_is_not_collected_by_the_harness_suite():
    """Invariant 26, asserted structurally.

    `benchmarks/` lives at the repo root while the harness suite's rootdir is
    `deepagent-image/project` (CI runs `pytest tests/` from there), so the
    instances' own `conftest.py` and `tests/` can never reach this suite. Pinned
    because the failure mode of getting it wrong is loud but confusing: the gold
    set's *deliberately failing* tests would be reported as harness failures.
    """
    project_root = Path(__file__).resolve().parents[1]
    assert _GOLD.is_dir()
    assert project_root not in _GOLD.parents
    assert not (project_root / "benchmarks").exists()


def test_benchmarks_has_no_importer():
    """Invariant 28: deleting `benchmarks/` breaks nothing.

    It is fixture data. Nothing under `harness/` may import it or hard-code a
    path into it — the dataset path is an argument to `harness bench run`, never
    a constant in the code.

    Checked over the parsed AST with docstrings excluded, so a *doc* example
    (`"workspace": "benchmarks/gold/001"`) does not read as a dependency. The
    first draft of this test used a substring scan and flagged exactly that.
    """
    import ast

    harness_dir = Path(__file__).resolve().parents[1] / "harness"
    offenders = []
    for path in sorted(harness_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in getattr(node, "names", [])]
                if any("benchmarks" in (n or "") for n in names) or "benchmarks" in (
                    getattr(node, "module", "") or ""
                ):
                    offenders.append(f"{path.name}: imports benchmarks")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "benchmarks" in node.value
                and id(node) not in docstrings
            ):
                offenders.append(f"{path.name}: hard-codes {node.value!r}")
    assert not offenders, f"harness modules depend on benchmarks/: {offenders}"
