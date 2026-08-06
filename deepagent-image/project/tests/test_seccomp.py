"""Narrow-seccomp-profile invariants (M4 slice H, invariant 31).

The point of these tests is that the relaxation stays *narrow*. Slice H buys an
inner boundary (the bwrap fs jail) by relaxing the container's seccomp filter,
which is only a good trade while the relaxation is five syscalls wide. A
regression that swapped in `seccomp=unconfined`, or quietly widened the set, has
to fail here rather than sail through because the jail still happens to work.

Host-runnable, stdlib only, no network: the sync path is not exercised, only the
pure transform and the committed artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from _bootstrap import _load

seccomp = _load("seccomp")


def _minimal_upstream() -> dict:
    """A stand-in shaped like moby's default: deny-by-default, gated namespace ops."""
    return {
        "defaultAction": "SCMP_ACT_ERRNO",
        "syscalls": [
            {"names": ["read", "write"], "action": "SCMP_ACT_ALLOW"},
            {
                "names": ["clone"],
                "action": "SCMP_ACT_ALLOW",
                "args": [{"index": 0, "value": 2114060288, "op": "SCMP_CMP_MASKED_EQ"}],
                "excludes": {"caps": ["CAP_SYS_ADMIN"]},
            },
            {
                "names": ["mount", "unshare"],
                "action": "SCMP_ACT_ALLOW",
                "includes": {"caps": ["CAP_SYS_ADMIN"]},
            },
        ],
    }


def test_relax_appends_exactly_one_entry_and_leaves_upstream_alone():
    upstream = _minimal_upstream()
    before = json.dumps(upstream, sort_keys=True)

    relaxed = seccomp.relax_userns(upstream)

    # Input untouched -- callers keep the upstream copy for diffing.
    assert json.dumps(upstream, sort_keys=True) == before
    assert len(relaxed["syscalls"]) == len(upstream["syscalls"]) + 1
    # Every upstream rule survives verbatim, so a reader diffing against Docker's
    # default sees one added block and nothing else.
    assert relaxed["syscalls"][:-1] == upstream["syscalls"]
    assert set(relaxed["syscalls"][-1]["names"]) == set(seccomp.RELAXED_SYSCALLS)


def test_relaxed_profile_verifies():
    assert seccomp.verify_profile(seccomp.relax_userns(_minimal_upstream())) == []


def test_unconfined_default_action_is_rejected():
    """The whole point of vendoring: an allow-by-default profile is unconfined."""
    profile = seccomp.relax_userns(_minimal_upstream())
    profile["defaultAction"] = "SCMP_ACT_ALLOW"

    problems = seccomp.verify_profile(profile)

    assert any("defaultAction" in p for p in problems)


def test_missing_relaxation_is_rejected():
    problems = seccomp.verify_profile(_minimal_upstream())
    assert any("no unconditional allow entry" in p for p in problems)


def test_widened_relaxation_is_rejected():
    """A regression that adds syscalls to the relaxation must fail, not pass."""
    profile = seccomp.relax_userns(_minimal_upstream())
    profile["syscalls"][-1]["names"].append("ptrace")

    problems = seccomp.verify_profile(profile)

    assert any("drifted" in p and "ptrace" in p for p in problems)


def test_narrowed_relaxation_is_rejected():
    """Dropping one breaks the jail; catch it here rather than at bwrap exec."""
    profile = seccomp.relax_userns(_minimal_upstream())
    profile["syscalls"][-1]["names"].remove("pivot_root")

    problems = seccomp.verify_profile(profile)

    assert any("drifted" in p and "pivot_root" in p for p in problems)


def test_duplicate_relaxation_entries_are_rejected():
    profile = seccomp.relax_userns(_minimal_upstream())
    profile = seccomp.relax_userns(profile)

    problems = seccomp.verify_profile(profile)

    assert any("expected 1" in p for p in problems)


def test_committed_profile_is_narrow():
    """The artifact actually shipped is Docker's default plus exactly our five.

    This is the CI regression guard (`seccomp-sync --check`) as a unit test: it
    is what fails if someone regenerates the profile from a different source or
    hand-edits it wider.
    """
    path = seccomp.profile_path()
    assert path.exists(), f"vendored profile missing: {path}"

    profile = seccomp.load_profile(path)

    assert seccomp.verify_profile(profile) == []
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"


def test_committed_profile_still_gates_the_dangerous_syscalls():
    """Relaxing namespaces must not have loosened anything else.

    bpf / perf_event_open / keyctl stay CAP_SYS_ADMIN-gated or absent. Observable
    at runtime too: under this profile they return EPERM, under unconfined they
    reach the kernel (EINVAL/EFAULT).
    """
    profile = seccomp.load_profile()
    dangerous = {"bpf", "perf_event_open", "keyctl"}

    for entry in profile["syscalls"]:
        names = set(entry.get("names") or [])
        if not (names & dangerous):
            continue
        unconditional = not (
            entry.get("includes") or entry.get("excludes") or entry.get("args")
        )
        assert not (
            entry.get("action") == "SCMP_ACT_ALLOW" and unconditional
        ), f"dangerous syscalls granted unconditionally: {sorted(names & dangerous)}"

    # Upstream keeps bpf/keyctl in the same CAP_SYS_ADMIN-gated block as several
    # namespace syscalls, so the relaxation entry must be its own separate block
    # naming only our five -- never a widened copy of that gated block.
    relax = [
        e
        for e in profile["syscalls"]
        if e.get("action") == "SCMP_ACT_ALLOW"
        and not (e.get("includes") or e.get("excludes") or e.get("args"))
        and set(e.get("names") or []) & set(seccomp.RELAXED_SYSCALLS)
    ]
    assert len(relax) == 1
    assert set(relax[0]["names"]) == set(seccomp.RELAXED_SYSCALLS)
    assert not (set(relax[0]["names"]) & dangerous)
