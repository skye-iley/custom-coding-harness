"""Tests for harness/bench/patch.py — Milestone 8 B2 (the one patch extractor).

Host tier: stdlib + a real `git` binary, no langchain, no model, no network.
Skips when git is absent rather than failing — CI installs pytest and nothing
else, and a missing git is an environment fact, not a regression.

**The extractor is driven directly here, not through the headless JSON**
(invariant 12a). A sweep must not depend on `--emit-patch`: the driver calls this
function against a workspace it prepared, and a foreign harness has no such flag,
so the path the driver uses is the path that has to be tested.

The rule every case below follows, and the one M7 §0.2 paid for: **assert a patch
by applying it**, never by substring. `"def paginate" in model_patch` is exactly
the mistake — a substring assertion against a serialised blob cannot tell a
correct artifact from a plausible-looking one. `git apply --check` against a
*fresh* copy of the base can.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _bootstrap import _load

patch_mod = _load("harness.bench.patch")

pytestmark = pytest.mark.skipif(
    not patch_mod.git_available(), reason="no git binary on PATH"
)


# --- helpers ------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _repo(tmp_path: Path) -> Path:
    """A seeded instance: an initialised repo with one commit and a clean tree.

    §13 item 6's requirement, in fixture form — no base commit means nothing to
    diff against, so a gold-set instance without one produces no patch at all.
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "paginate.py").write_text("def paginate(rows, n):\n    return rows[:n + 1]\n")
    (ws / "src" / "util.py").write_text("VALUE = 1\n")
    _git(ws.parent, "init", "-q", str(ws))
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "test")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "seed the bug")
    return ws


def _fresh_base(tmp_path: Path, ws: Path, base: str, name: str = "fresh") -> Path:
    """A clone of `ws` checked out at `base` — the tree a scorer would apply to.

    Applying to the workspace the patch came from would pass trivially (the
    changes are already there), which is why invariant 9 says *fresh copy*.
    """
    target = tmp_path / name
    _git(tmp_path, "clone", "-q", str(ws), str(target))
    _git(target, "checkout", "-q", base)
    return target


def _applies(tree: Path, patch: str, tmp_path: Path) -> bool:
    p = tmp_path / "candidate.patch"
    p.write_text(patch, encoding="utf-8", newline="")
    proc = subprocess.run(
        ["git", "-C", str(tree), "apply", "--check", str(p)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr)
    return proc.returncode == 0


# --- the untracked-file case (invariant 8) ------------------------------------


def test_a_new_file_the_agent_never_staged_appears_in_the_patch(tmp_path):
    """The single most likely silent defect in this milestone.

    Without `git add -A -N` the patch is EMPTY, applies cleanly as a no-op, and
    scores 0 with a signature identical to "the model did nothing". This is
    `gold-005-new-module`'s reason for existing, asserted here at the seam.
    """
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "validate.py").write_text("def validate(x):\n    return x is not None\n")

    result = patch_mod.extract_patch(ws, base)

    assert not patch_mod.is_empty(result), (
        "an untracked new file produced an empty patch — intent-to-add is missing"
    )
    assert "src/validate.py" in patch_mod.changed_paths(result)
    fresh = _fresh_base(tmp_path, ws, base)
    assert _applies(fresh, result, tmp_path)
    _git(fresh, "apply", str(tmp_path / "candidate.patch"))
    assert (fresh / "src" / "validate.py").is_file(), (
        "the patch applied but the new file is not there"
    )


def test_a_new_file_is_not_merely_mentioned_but_actually_carried(tmp_path):
    # The M7 §0.2 trap in its exact shape: a header naming the file would satisfy
    # a substring assertion while carrying none of its content.
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "validate.py").write_text("SENTINEL = 'carried'\n")

    result = patch_mod.extract_patch(ws, base)
    fresh = _fresh_base(tmp_path, ws, base)
    _git(fresh, "apply", str(_write(tmp_path, result)))
    assert (fresh / "src" / "validate.py").read_text() == "SENTINEL = 'carried'\n"


def _write(tmp_path: Path, patch: str) -> Path:
    p = tmp_path / "written.patch"
    p.write_text(patch, encoding="utf-8", newline="")
    return p


# --- exclusions (invariants 10 and 11) ----------------------------------------


def test_no_harness_artifact_reaches_the_patch(tmp_path):
    """Asserted on a workspace where EVERY excluded path exists and is dirty, so
    a missing exclusion fails rather than passing by luck."""
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)

    (ws / "src" / "paginate.py").write_text("def paginate(rows, n):\n    return rows[:n]\n")
    (ws / ".deepagents").mkdir()
    (ws / ".deepagents" / "session.env").write_text("DEEPAGENTS_SESSION_BRANCH=agent/x\n")
    (ws / ".agent_telemetry").mkdir()
    (ws / ".agent_telemetry" / "interrupts.jsonl").write_text('{"id": "1"}\n')
    (ws / ".conda").mkdir()
    (ws / ".conda" / "env-marker").write_text("x" * 1000)
    (ws / ".harness-config.yaml").write_text("autonomy_level: guided\n")
    (ws / ".harness-profile.yaml").write_text("model: ollama:gemma4\n")

    result = patch_mod.extract_patch(ws, base)
    paths = patch_mod.changed_paths(result)

    assert paths == ["src/paginate.py"], f"harness artifacts leaked: {paths}"
    for excluded in patch_mod.DEFAULT_EXCLUDES:
        assert excluded not in result, excluded
    # Invariant 11: exclusion is by pathspec, so the result still applies. A diff
    # filtered after the fact would not.
    assert _applies(_fresh_base(tmp_path, ws, base), result, tmp_path)


def test_exclusion_does_not_hide_a_real_change_in_a_similarly_named_path(tmp_path):
    # `.deepagents` is excluded; `src/deepagents_helper.py` is not. A prefix match
    # instead of a pathspec would swallow the second.
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "deepagents_helper.py").write_text("HELPER = 1\n")

    result = patch_mod.extract_patch(ws, base)
    assert patch_mod.changed_paths(result) == ["src/deepagents_helper.py"]


# --- the base (invariant 12) --------------------------------------------------


def test_the_patch_is_taken_against_the_recorded_base_not_head(tmp_path):
    """The failure mode: `git-pr` commits at session.end, `git diff HEAD` goes
    empty, and the patch is silently lost — indistinguishable from an agent that
    changed nothing.

    Asserted with a commit sitting on top: the run that committed must produce
    the SAME patch as the run that did not.
    """
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)

    (ws / "src" / "paginate.py").write_text("def paginate(rows, n):\n    return rows[:n]\n")
    (ws / "src" / "validate.py").write_text("def validate(x):\n    return True\n")
    before_commit = patch_mod.extract_patch(ws, base)

    # The git lifecycle does its thing.
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "agent/session commit")
    assert patch_mod.resolve_base(ws) != base, "the test did not actually move HEAD"

    after_commit = patch_mod.extract_patch(ws, base)
    assert after_commit == before_commit
    assert not patch_mod.is_empty(after_commit)
    assert _applies(_fresh_base(tmp_path, ws, base), after_commit, tmp_path)


def test_diffing_against_head_after_a_commit_would_have_been_empty(tmp_path):
    # The control for the case above: it is only interesting because the naive
    # implementation really does produce nothing.
    ws = _repo(tmp_path)
    (ws / "src" / "paginate.py").write_text("def paginate(rows, n):\n    return rows[:n]\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "agent/session commit")
    head = patch_mod.resolve_base(ws)
    assert patch_mod.is_empty(patch_mod.extract_patch(ws, head))


# --- the index is not ours to change ------------------------------------------


def test_extraction_leaves_the_real_index_untouched(tmp_path):
    """Read-only with respect to the operator's repo.

    `git add -A -N` against the real index would stage intent-to-add entries in
    a workspace the operator may still be using — a side effect of *reading* a
    diff. The scratch `GIT_INDEX_FILE` is what avoids it.
    """
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "validate.py").write_text("X = 1\n")
    before = _git(ws, "status", "--porcelain")

    patch_mod.extract_patch(ws, base)

    assert _git(ws, "status", "--porcelain") == before
    assert "?? src/validate.py" in before  # still untracked, not intent-to-add


def test_the_scratch_index_never_appears_in_the_patch(tmp_path):
    # Measured, not assumed: a first pass put the temp index inside the workspace
    # and `git add -A` swept it into the diff it was being used to compute.
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "validate.py").write_text("X = 1\n")
    result = patch_mod.extract_patch(ws, base)
    assert all("index" not in p.split("/")[-1] for p in patch_mod.changed_paths(result))


def test_what_the_agent_staged_does_not_change_the_patch(tmp_path):
    # The scratch index is seeded from the base tree, so an agent that ran
    # `git add` (or `git rm --cached`) mid-run cannot move the result.
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "paginate.py").write_text("def paginate(rows, n):\n    return rows[:n]\n")
    (ws / "src" / "validate.py").write_text("X = 1\n")
    unstaged = patch_mod.extract_patch(ws, base)

    _git(ws, "add", "-A")
    assert patch_mod.extract_patch(ws, base) == unstaged


# --- deletions, edits, empties ------------------------------------------------


def test_a_deleted_file_is_carried_and_applies(tmp_path):
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / "src" / "util.py").unlink()

    result = patch_mod.extract_patch(ws, base)
    fresh = _fresh_base(tmp_path, ws, base)
    assert _applies(fresh, result, tmp_path)
    _git(fresh, "apply", str(_write(tmp_path, result)))
    assert not (fresh / "src" / "util.py").exists()


def test_an_unchanged_workspace_yields_an_empty_patch(tmp_path):
    """Empty is a real answer — "the agent changed nothing" — not an error. Every
    genuine failure raises instead, so the two can never be confused."""
    ws = _repo(tmp_path)
    result = patch_mod.extract_patch(ws, patch_mod.resolve_base(ws))
    assert result == ""
    assert patch_mod.is_empty(result) is True


def test_a_workspace_dirty_only_with_excluded_paths_yields_an_empty_patch(tmp_path):
    # A run where the agent did nothing but the harness wrote its own state must
    # report empty, not a patch full of harness noise.
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    (ws / ".deepagents").mkdir()
    (ws / ".deepagents" / "session.env").write_text("x\n")
    assert patch_mod.is_empty(patch_mod.extract_patch(ws, base))


# --- failure is loud ----------------------------------------------------------


def test_a_workspace_that_is_not_a_repo_raises_rather_than_returning_empty(tmp_path):
    """The distinction the whole milestone rests on: a broken extractor must not
    look like a weak model. An empty patch scores zero and reads as "the agent
    did nothing"; a raise is a defect a driver can record as one."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    with pytest.raises(patch_mod.PatchError, match="not a git repository"):
        patch_mod.extract_patch(plain, "HEAD")


def test_a_missing_workspace_raises(tmp_path):
    with pytest.raises(patch_mod.PatchError, match="does not exist"):
        patch_mod.extract_patch(tmp_path / "nope", "HEAD")


def test_an_unresolvable_base_raises(tmp_path):
    ws = _repo(tmp_path)
    with pytest.raises(patch_mod.PatchError):
        patch_mod.resolve_base(ws, "no-such-ref")


def test_resolve_base_records_a_sha_not_a_moving_ref(tmp_path):
    ws = _repo(tmp_path)
    base = patch_mod.resolve_base(ws)
    assert len(base) == 40 and base != "HEAD"
    (ws / "src" / "util.py").write_text("VALUE = 2\n")
    _git(ws, "commit", "-aqm", "move HEAD")
    # The recorded value did not move with HEAD — which is the entire mechanism.
    assert patch_mod.resolve_base(ws) != base


# --- reporting helpers --------------------------------------------------------


def test_changed_paths_is_empty_for_an_empty_patch():
    assert patch_mod.changed_paths("") == []
    assert patch_mod.changed_paths(None) == []


def test_is_empty_treats_whitespace_as_empty():
    assert patch_mod.is_empty(None) is True
    assert patch_mod.is_empty("   \n") is True
    assert patch_mod.is_empty("diff --git a/x b/x\n") is False
