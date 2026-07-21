"""Workspace refresh: pull live host edits into an ephemeral workspace copy.

Only meaningful under `run-docker -Ephemeral` / `EPHEMERAL=1`, which mounts a
throwaway COPY of the workspace at AGENT_WORKSPACE (so changes revert on close)
and — for this feature — additionally bind-mounts the REAL workspace read-only
at DEEPAGENTS_WORKSPACE_SRC. `refresh_into` mirrors that source into the copy
(source wins on conflict) so edits a human makes on the host DURING the run
become visible to the agent, while the copy itself stays throwaway.

Absent the source mount (a normal, non-ephemeral run) the whole feature is inert:
`workspace_src()` returns None and both the `/refresh` REPL command and the
`refresh_workspace` agent tool report "unavailable" instead of acting.

stdlib only (no providers/cost/langchain imports), so it is host-testable and
safe to import on a bare interpreter.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

WORKSPACE_SRC_ENV = "DEEPAGENTS_WORKSPACE_SRC"

# Excluded from the copy: the heavy, rebuildable workspace conda env. Matches
# run-docker's copy_workspace, which already omits it from the ephemeral copy —
# a refresh must not drag gigabytes of env back in either.
_EXCLUDE_TOP = frozenset({".conda"})


def workspace_src() -> Path | None:
    """The read-only source mount, or None when the feature is off.

    Set by run-docker only in ephemeral mode. Returns the Path only when the env
    var is set AND points at an existing directory, so a caller can treat None as
    a single "refresh unavailable" signal (not an ephemeral run, or misconfigured).
    """
    raw = os.getenv(WORKSPACE_SRC_ENV, "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def _resolve_subpath(base: Path, subpath: str | None) -> Path:
    """Resolve `subpath` under `base`, refusing to escape it.

    A `..`/absolute/symlinked subpath that would land outside `base` raises
    ValueError — the agent (or a fat-fingered operator) must not be able to point
    a refresh at, say, /project/state or the host root through the source mount.
    """
    if not subpath:
        return base.resolve()
    base_r = base.resolve()
    candidate = (base_r / subpath).resolve()
    if candidate != base_r and base_r not in candidate.parents:
        raise ValueError(f"path {subpath!r} escapes the workspace")
    return candidate


def refresh_into(dst: Path, src: Path, subpath: str | None = None) -> list[str]:
    """Copy `src` -> `dst` (mirror, source wins on conflict), excluding `.conda`.

    Copies every file present in `src` over `dst`, overwriting on conflict, so
    host-side edits made during the run appear in the agent's working copy. Files
    that exist only in `dst` (e.g. ones the agent created this run) are LEFT
    ALONE — this pulls the source *in*, it does not delete divergent agent work,
    so it is safe to call mid-turn without nuking uncommitted output. `subpath`
    scopes both sides to a single file or subdirectory. Returns the dst-relative
    paths written (for a "updated N file(s)" summary).
    """
    src_root = _resolve_subpath(src, subpath)
    if not src_root.exists():
        raise FileNotFoundError(
            f"{subpath or '.'!r} is not present in the source workspace"
        )
    dst_resolved = dst.resolve()
    written: list[str] = []

    def _copy_file(s: Path, d: Path) -> None:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        written.append(str(d.relative_to(dst_resolved)))

    if src_root.is_file():
        # A single-file subpath: mirror it to the same relative location in dst.
        _copy_file(src_root, dst_resolved / src_root.relative_to(src.resolve()))
        return written

    src_resolved = src.resolve()
    for cur in sorted(src_root.rglob("*")):
        rel = cur.relative_to(src_resolved)
        # Skip the excluded top-level dir and everything under it.
        if rel.parts and rel.parts[0] in _EXCLUDE_TOP:
            continue
        target = dst_resolved / rel
        if cur.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif cur.is_file():
            _copy_file(cur, target)
    return written
