"""AgentProfile / BindEntry invariants (M9, milestone9_invariants.md).

Pure dataclass + bind-list construction logic, no bwrap/docker needed --
mirrors test_limits.py's "arithmetic only" placement.
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

profile = _load("profile")


def test_default_profile_has_a_single_rw_workspace_root_entry():
    """Invariant 2: the shipped default must resolve to exactly today's bind."""
    assert profile.DEFAULT_PROFILE.name == "default"
    assert profile.DEFAULT_PROFILE.binds == [profile.BindEntry(relpath=".", mode="rw")]
    assert profile.DEFAULT_PROFILE.harness_profile_key is None
    assert profile.DEFAULT_PROFILE.network is None


def test_bind_entry_rejects_absolute_paths():
    """Invariant 4: the §10 sandbox-escape mitigation."""
    with pytest.raises(ValueError, match="workspace-relative"):
        profile.BindEntry(relpath="/etc", mode="ro")


def test_bind_entry_rejects_windows_drive_absolute_paths():
    with pytest.raises(ValueError, match="workspace-relative"):
        profile.BindEntry(relpath="C:\\Windows", mode="ro")


def test_bind_entry_rejects_dotdot_escape():
    """Invariant 4: `..` anywhere in the relpath is a hard construction failure."""
    with pytest.raises(ValueError, match="escape"):
        profile.BindEntry(relpath="../../etc", mode="ro")


def test_bind_entry_rejects_dotdot_in_a_middle_segment():
    with pytest.raises(ValueError, match="escape"):
        profile.BindEntry(relpath="src/../../etc", mode="rw")


def test_bind_entry_rejects_empty_relpath():
    with pytest.raises(ValueError):
        profile.BindEntry(relpath="", mode="rw")


def test_bind_entry_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="mode"):
        profile.BindEntry(relpath="src", mode="rwx")


def test_bind_entry_accepts_a_plain_relative_subpath():
    entry = profile.BindEntry(relpath="src/components", mode="ro")
    assert entry.relpath == "src/components"
    assert entry.mode == "ro"


def test_agent_profile_can_mix_rw_and_ro_entries():
    """Invariant 5: mode is per-entry, not per-profile."""
    p = profile.AgentProfile(
        name="architect",
        binds=[
            profile.BindEntry(relpath="src", mode="rw"),
            profile.BindEntry(relpath="docs", mode="ro"),
        ],
    )
    modes = {b.relpath: b.mode for b in p.binds}
    assert modes == {"src": "rw", "docs": "ro"}
