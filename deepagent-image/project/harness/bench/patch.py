"""The one patch extractor (Milestone 8, slice B2).

Turns a workspace the agent has been working in into a `git diff` an *official*
scorer can consume: the `model_patch` of a SWE-bench-style predictions line.
Stdlib + the `git` binary only, so it stays in the host test tier and is keyless
in the strong sense.

**There is exactly one implementation, and the driver calls it directly**
(`milestone8_invariants.md` 12a). `--emit-patch` is a *caller* of this function,
not a second copy of it — a convenience for an operator running one instance by
hand. A sweep does not depend on the flag, because a foreign harness (Aider,
SWE-agent, Claude Code) has no `--emit-patch` and never will, and the driver has
to be able to produce a patch from a workspace it owns. Two implementations of
`git add -A -N` + pathspec exclusion is two chances to get the untracked-file
case wrong, and the one that gets it wrong is the one nobody is looking at.

Four things here are load-bearing, each for a failure that is otherwise silent:

* **Intent-to-add is not optional.** `git diff` shows *nothing* for an untracked
  file. An agent that fixes a bug by adding a new module would produce an **empty
  patch** — which applies cleanly as a no-op and scores zero with a signature
  identical to "the model did nothing". `git add -A -N` is what makes the new
  file visible to the diff.
* **The index is never touched.** The intent-to-add marks go into a throwaway
  index outside the workspace (`GIT_INDEX_FILE`), seeded with `read-tree <base>`.
  So extraction is read-only with respect to the operator's repo *and*
  independent of whatever state the agent left the real index in — an agent that
  ran `git add` or `git rm --cached` mid-run cannot change the patch.
* **Exclusion is by pathspec, not by filtering the diff text afterwards.**
  Editing a unified diff after the fact to drop a file is how you produce a patch
  that does not apply.
* **The base is a recorded commit, not `HEAD`.** The git session lifecycle
  (`git-branch` / `git-pr`) commits, after which `git diff HEAD` is empty and the
  patch is silently lost. Diffing against a SHA captured before the run makes the
  patch identical whether or not anything committed.

`--no-ext-diff` is on for a fifth reason of the same kind: a workspace
`.gitconfig` declaring an external differ would otherwise emit something that is
not a patch at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Harness artifacts that must never reach a prediction. `.deepagents/` and
# `.agent_telemetry/` are harness state the agent happens to be able to see,
# `.conda/` is a rebuilt environment (and enormous), and the two config files are
# operator settings. A scorer applying any of them to a fresh checkout would at
# best add noise and at worst fail to apply.
DEFAULT_EXCLUDES = (
    ".deepagents",
    ".agent_telemetry",
    ".conda",
    ".harness-config.yaml",
    ".harness-profile.yaml",
)

# A hung `git` must not wedge a 50-instance sweep. Generous, because a large
# binary diff on a cold filesystem is legitimately slow.
DEFAULT_TIMEOUT_SECONDS = 120.0


class PatchError(RuntimeError):
    """Extraction failed. Raised, never swallowed into an empty patch.

    The distinction matters more here than almost anywhere else in the harness:
    an empty patch is a *valid* result (the agent changed nothing) and scores
    zero, so degrading a failure into one would hide a broken extractor behind a
    number that looks like a weak model. That is the exact confusion this
    milestone exists to remove.
    """


def git_available() -> bool:
    """Is there a `git` binary on PATH? The host tier skips rather than fails."""
    return shutil.which("git") is not None


def _run(workspace: Path, args: list[str], *, timeout: float,
         env_extra: dict[str, str] | None = None) -> bytes:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:  # no git binary
        raise PatchError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PatchError(f"git {' '.join(args)} timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise PatchError(f"git {' '.join(args)}: {detail}")
    return proc.stdout


def is_git_repo(workspace: Path | str) -> bool:
    """True when `workspace` is inside a git work tree."""
    workspace = Path(workspace)
    if not workspace.is_dir():
        return False
    try:
        out = _run(workspace, ["rev-parse", "--is-inside-work-tree"],
                   timeout=DEFAULT_TIMEOUT_SECONDS)
    except PatchError:
        return False
    return out.decode("utf-8", "replace").strip() == "true"


def resolve_base(workspace: Path | str, rev: str = "HEAD",
                 *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """The commit SHA `rev` names, recorded so a later commit cannot move it.

    Called once **before** a run, and the result carried through to
    `extract_patch`. Resolving to a SHA rather than keeping the string `"HEAD"`
    is the whole mechanism: `HEAD` is a moving target and the git lifecycle moves
    it (`milestone8.md` §5.2).
    """
    out = _run(Path(workspace), ["rev-parse", "--verify", f"{rev}^{{commit}}"],
               timeout=timeout)
    sha = out.decode("utf-8", "replace").strip()
    if not sha:
        raise PatchError(f"could not resolve {rev!r} to a commit in {workspace}")
    return sha


def extract_patch(
    workspace: Path | str,
    base: str,
    *,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """The unified diff from `base` to the current working tree of `workspace`.

    Returns `""` when nothing changed — a real, scorable answer ("the agent
    changed nothing"), which is why every *failure* raises `PatchError` instead
    of returning the same thing.

    The working tree is read as-is, dirty and uncommitted. That ordering is the
    point: this must run *before* `session.end`, while the changes are still in
    the tree rather than in a commit.
    """
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise PatchError(f"workspace does not exist: {workspace}")
    if not is_git_repo(workspace):
        raise PatchError(
            f"{workspace} is not a git repository — there is no base to diff "
            "against, so no patch can be produced"
        )

    # The scratch index lives OUTSIDE the workspace. Inside, `git add -A` would
    # sweep it into the very diff it is being used to compute — measured, not
    # guessed: a first pass put it in the workspace and the temp file showed up
    # as an added file in the patch.
    with tempfile.TemporaryDirectory(prefix="deepagents-bench-") as scratch:
        index = Path(scratch) / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        # Seed from the base tree rather than copying the real index: the patch
        # then describes base -> worktree regardless of what the agent staged.
        _run(workspace, ["read-tree", base], timeout=timeout, env_extra=env)
        # Intent-to-add. Without this line a new file is invisible to `git diff`
        # and the whole patch is silently empty.
        _run(workspace, ["add", "-A", "-N", "--", "."], timeout=timeout, env_extra=env)
        raw = _run(
            workspace,
            [
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--binary",
                base,
                "--",
                ".",
                *[f":(exclude){p}" for p in excludes],
            ],
            timeout=timeout,
            env_extra=env,
        )

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Loud, not lossy. `errors="replace"` here would hand a scorer a patch
        # that looks fine and does not apply, which is worse than no patch: the
        # instance would score zero and read as a weak model.
        raise PatchError(
            "the diff is not valid UTF-8 (a non-UTF-8 source file?), so it "
            "cannot be carried as JSON without corrupting it"
        ) from exc


def is_empty(patch: str | None) -> bool:
    """True when the patch changes nothing.

    Its own function because "empty" is a result a sweep must *count* rather than
    treat as a gap: an all-empty sweep is the §0 failure mode reproduced inside
    the instrument, and it has to be loud (`milestone8.md` §8).
    """
    return not (patch or "").strip()


def changed_paths(patch: str | None) -> list[str]:
    """The `b/`-side paths a patch touches, in order, for reporting.

    Parsed from `diff --git a/X b/X` headers only — enough to say *what* an
    instance changed in a summary line, and deliberately not a diff parser. The
    patch itself is never rebuilt from this (invariant 11: exclusion is by
    pathspec, and a diff reconstructed from text does not apply).
    """
    out: list[str] = []
    for line in (patch or "").splitlines():
        if not line.startswith("diff --git "):
            continue
        _, _, rest = line.partition("diff --git ")
        # `a/path b/path`; take the b-side, which exists for adds and edits and
        # is `/dev/null`-free in git's own header form.
        parts = rest.split(" b/", 1)
        if len(parts) == 2 and parts[1]:
            out.append(parts[1])
    return out
