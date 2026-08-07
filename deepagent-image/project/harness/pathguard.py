"""Path-guard middleware — defense-in-depth traversal check for file tools.

Implements the design_doc.md §2 snippet: ``realpath`` both target and base,
``os.path.commonpath`` equality (not ``startswith``, which is vulnerable to
sibling-escape like ``/workspace-evil`` vs ``/workspace``).

Raises ``PathGuardDenied`` (a ``PermissionError`` subclass) on traversal,
sibling-escape, or symlink-out — never on a legitimate in-bounds path.

Covers the **file** tools only (read/write/edit/ls/glob) that funnel through
the backend's ``_resolve_path``. The **shell** tool does not route through this
guard — it is bounded by the container root + the docker mask.

Defense-in-depth only: the docker mask (and future bwrap bind-whitelist) is the
real boundary. This guard is a racy TOCTOU guard rail, not the security claim.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathGuardDenied(PermissionError):
    """Raised when a path traversal is detected.

    Carries the offending relpath for audit ``meta``.
    """

    def __init__(self, target: str, base: str, reason: str = "path traversal"):
        self.target = target
        self.base = base
        self.relpath = relative_to(target, base) if base else target
        super().__init__(f"{reason}: {target} (base: {base})")


def relative_to(target: str, base: str) -> str:
    """Best-effort relpath computation for the audit trail.

    ``os.path.relpath`` *raises* on inputs a denial can legitimately produce (a
    different Windows drive; an empty path on posix), and every caller here is
    already inside an exception path where a second exception would replace the
    real ``PathGuardDenied``. So it never raises — it degrades to the absolute
    target instead."""
    try:
        return os.path.relpath(target, base)
    except (ValueError, OSError):
        return target


def validate_path(target: str, base: str) -> str:
    """Validate that `target` is within `base`, using realpath + commonpath.

    Args:
        target: The path the tool is trying to access (resolved).
        base: The allowed root directory (workspace root).

    Returns:
        The resolved absolute path (same as realpath(target)) on success.

    Raises:
        PathGuardDenied: if the target escapes the base directory.
    """
    abs_target = os.path.realpath(target)
    abs_base = os.path.realpath(base)

    # commonpath check — not startswith — so sibling-escape is caught
    # (/workspace-evil vs /workspace).
    common = os.path.commonpath([abs_target, abs_base])
    if common != abs_base:
        raise PathGuardDenied(
            str(target), str(base),
            reason=f"path escapes base directory (commonpath={common}, expected={abs_base})"
        )

    return abs_target


def validate_path_or_none(target: str | None, base: str) -> str | None:
    """Like ``validate_path`` but returns ``None`` when ``target`` is ``None``."""
    if target is None:
        return None
    return validate_path(target, base)
