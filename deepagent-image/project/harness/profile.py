"""Per-agent bind-mount scoping (M9, docs/milestones/in-progress/milestone9.md).

`AgentProfile` is a harness-owned object naming which workspace paths a bwrap
seam (`jail.bwrap_args`, `scripts/sandbox-exec.sh`) may bind, and how. It is
**not** `deepagents.profiles.HarnessProfile` -- that beta library class controls
prompt assembly and tool/middleware exclusion; this controls filesystem bind
scope, a third axis consumed by a completely different call path. See
milestone9.md §1 for the full naming-collision writeup.

Stdlib only, no harness-sibling imports -- constructible by the same
pre-runtime-stack code that builds `bwrap_args`, and stays in the host test
tier (mirrors mask.py/cost.py/jail.py's acyclic discipline).
"""

from __future__ import annotations

from dataclasses import dataclass

_VALID_MODES = ("rw", "ro")


def _validate_relpath(relpath: str) -> None:
    """Reject anything that isn't a plain workspace-relative path.

    This is the direct mitigation for design_doc.md §10's "Sandbox Escape
    (Dynamic Binds)" risk: an absolute path or a `..`-escape could bind
    something outside the workspace. Raise at construction -- the earliest
    point of entry, same principle M5.1 applied to enum knobs -- rather than
    validating after a path is joined onto `workspace` downstream.
    """
    if not relpath:
        raise ValueError("BindEntry.relpath must not be empty")
    normalized = relpath.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(
            f"BindEntry.relpath must be workspace-relative, got an absolute path: {relpath!r}"
        )
    if len(normalized) >= 2 and normalized[1] == ":":
        raise ValueError(
            f"BindEntry.relpath must be workspace-relative, got an absolute path: {relpath!r}"
        )
    if ".." in normalized.split("/"):
        raise ValueError(
            f"BindEntry.relpath must not escape the workspace with '..': {relpath!r}"
        )


@dataclass(frozen=True)
class BindEntry:
    """One bind-mount entry: a workspace-relative path plus its access mode.

    `mode` is per-entry rather than per-profile (milestone9.md §5 fork B) so a
    profile can mix full access to one sub-path with read-only access to
    another in the same bind list.
    """

    relpath: str
    mode: str

    def __post_init__(self) -> None:
        _validate_relpath(self.relpath)
        if self.mode not in _VALID_MODES:
            raise ValueError(f"BindEntry.mode must be one of {_VALID_MODES}, got {self.mode!r}")


@dataclass(frozen=True)
class AgentProfile:
    """Which workspace paths an agent's bwrap jail may bind, and how.

    `harness_profile_key` and `network` are reserved, unresolved placeholders
    (milestone9.md §3.1 / §4) -- a pointer to a `deepagents.profiles.HarnessProfile`
    registration and chain item 3's `NetworkPolicy`, respectively -- so a later
    milestone can add to this object without a second field-registry change.
    Neither is read by any code this milestone ships.
    """

    name: str
    binds: list[BindEntry]
    harness_profile_key: str | None = None
    network: None = None


DEFAULT_PROFILE = AgentProfile(
    name="default",
    binds=[BindEntry(relpath=".", mode="rw")],
    harness_profile_key=None,
    network=None,
)
