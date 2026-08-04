"""bwrap fs-jail bind-set invariants (M4 slice H, invariants 5/17a/32/35).

These are the properties that make the jail a boundary rather than decoration:
the state dir and floor paths must not be reachable, masked paths must be
overmounted, and the whole thing must stay off and inert by default.

Host-runnable, stdlib only. The jail is *built* here as an argument list, never
executed -- actually entering a namespace is image-only (smoke).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _bootstrap import _load

jail = _load("jail")


def _pairs(args: list[str], flag: str) -> list[tuple[str, str]]:
    """Extract (source, target) for a two-argument bind flag like --ro-bind."""
    out = []
    for i, item in enumerate(args):
        if item == flag and i + 2 < len(args):
            out.append((args[i + 1], args[i + 2]))
    return out


def _single(args: list[str], flag: str) -> list[str]:
    """Extract targets for a one-argument flag like --tmpfs."""
    return [args[i + 1] for i, item in enumerate(args) if item == flag and i + 1 < len(args)]


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    return workspace


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_workspace_is_bound_writable(ws, state):
    args = jail.bwrap_args(ws, state)
    assert (str(ws), str(ws)) in _pairs(args, "--bind")


def test_existing_system_paths_are_bound_read_only(ws, state, tmp_path):
    """System paths go in as --ro-bind, never --bind.

    Asserted against a path that exists on the *host running the tests* rather
    than /usr, so this stays meaningful on the Windows dev box where none of the
    posix system paths exist and bwrap_args correctly skips them all.
    """
    fake_system = tmp_path / "usr"
    fake_system.mkdir()

    args = jail.bwrap_args(ws, state, extra_robinds=(str(fake_system),))

    assert (str(fake_system), str(fake_system)) in _pairs(args, "--ro-bind")
    assert (str(fake_system), str(fake_system)) not in _pairs(args, "--bind")


def test_missing_system_path_is_skipped_not_bound(ws, state):
    """A bind of a missing source is a hard bwrap error, so absent paths are skipped."""
    args = jail.bwrap_args(ws, state, extra_robinds=("/definitely/not/here",))
    assert "/definitely/not/here" not in args


def test_masked_dir_becomes_empty_tmpfs(ws, state):
    masked = [{"relpath": "secrets", "type": "dir", "tier": "user"}]
    args = jail.bwrap_args(ws, state, masked)
    assert str(ws / "secrets") in _single(args, "--tmpfs")


def test_masked_file_is_overmounted_with_an_empty_file(ws, state):
    empty = jail.ensure_empty_file(state)
    masked = [{"relpath": ".env", "type": "file", "tier": "default"}]

    args = jail.bwrap_args(ws, state, masked, empty_file=empty)

    assert (str(empty), str(ws / ".env")) in _pairs(args, "--ro-bind")
    assert empty.stat().st_size == 0


def test_floor_path_is_overmounted_inside_the_jail(ws, state):
    """Invariant 5 leg 4: the jail hides the floor independently of the docker mask.

    This is the leg v1 could not deliver -- with the jail on, a floor path is
    unreadable even if the docker overlay were disabled or misconfigured.
    """
    empty = jail.ensure_empty_file(state)
    masked = [{"relpath": "id_rsa", "type": "file", "tier": "floor"}]

    args = jail.bwrap_args(ws, state, masked, empty_file=empty)

    assert (str(empty), str(ws / "id_rsa")) in _pairs(args, "--ro-bind")


def test_masked_overmounts_come_after_the_workspace_bind(ws, state):
    """Later mounts layer on top -- same reasoning as the docker mask (§11.1).

    If a masked overmount were emitted before the workspace bind, the workspace
    would be mounted over it and the path would be readable again.
    """
    empty = jail.ensure_empty_file(state)
    masked = [{"relpath": ".env", "type": "file", "tier": "floor"}]

    args = jail.bwrap_args(ws, state, masked, empty_file=empty)

    workspace_at = args.index(str(ws))
    overmount_at = max(i for i, a in enumerate(args) if a == str(ws / ".env"))
    assert overmount_at > workspace_at


def test_harness_namespace_keeps_the_network(ws, state):
    """The harness makes the model API calls; dropping egress is the shell's job."""
    args = jail.bwrap_args(ws, state)
    assert "--unshare-net" not in args
    assert "--unshare-user" in args


def test_unshare_net_is_available_for_the_nested_case(ws, state):
    args = jail.bwrap_args(ws, state, unshare_net=True)
    assert "--unshare-net" in args


def test_state_dir_is_not_bound_when_absent(ws):
    """A nested/shell jail passes state_dir=None and must not bind it at all.

    This is invariant 17a's mechanism: the shell's namespace binds only the
    workspace, so it cannot reach <state-dir>/denials.jsonl and truncate the
    record of its own escape attempt.
    """
    args = jail.bwrap_args(ws, None)
    assert not any("state" in target for _, target in _pairs(args, "--bind") if target != str(ws))


def test_jail_is_off_by_default():
    """Invariant 35: opt-in. Enabling it relaxes seccomp, so it is never silent."""
    assert jail.jail_enabled({}) is False
    assert jail.jail_enabled({"DEEPAGENTS_JAIL": "0"}) is False
    assert jail.jail_enabled({"DEEPAGENTS_JAIL": "1"}) is True
    assert jail.jail_enabled({"DEEPAGENTS_JAIL": "true"}) is True


def test_reexec_is_a_noop_when_disabled(ws, state, monkeypatch):
    """No jail requested -> returns normally, never execs."""
    monkeypatch.delenv(jail.JAIL_ENV, raising=False)
    called = []
    monkeypatch.setattr(jail.os, "execvp", lambda *a: called.append(a))

    jail.maybe_reexec(ws, state)

    assert called == []


def test_reexec_is_a_noop_when_already_jailed(ws, state, monkeypatch):
    """Idempotence: the child must not re-exec itself into a second namespace."""
    monkeypatch.setenv(jail.JAIL_ENV, "1")
    monkeypatch.setenv(jail.JAILED_MARKER, "1")
    called = []
    monkeypatch.setattr(jail.os, "execvp", lambda *a: called.append(a))

    jail.maybe_reexec(ws, state)

    assert called == []


def test_reexec_fails_closed_when_the_jail_cannot_be_built(ws, state, monkeypatch):
    """Invariant 32: never degrade to unjailed after promising a boundary."""
    monkeypatch.setenv(jail.JAIL_ENV, "1")
    monkeypatch.delenv(jail.JAILED_MARKER, raising=False)
    monkeypatch.setattr(jail, "preflight", lambda: ["bwrap cannot create a user namespace here"])
    monkeypatch.setattr(jail.os, "execvp", lambda *a: pytest.fail("must not exec when unavailable"))

    with pytest.raises(jail.JailUnavailable, match="user namespace"):
        jail.maybe_reexec(ws, state)


def test_snapshot_is_read_not_re_resolved(ws, state):
    """Invariant 9: the jail overmounts what the launcher froze, not a fresh scan."""
    (ws / "secrets").mkdir()
    (ws / ".env").write_text("K=v\n", encoding="utf-8")
    (state / "mask-snapshot.txt").write_text(
        "floor .env\nuser secrets\n", encoding="utf-8"
    )

    entries = jail.masked_from_snapshot(state, ws)

    by_rel = {e["relpath"]: e for e in entries}
    assert by_rel[".env"]["type"] == "file"
    assert by_rel[".env"]["tier"] == "floor"
    assert by_rel["secrets"]["type"] == "dir"


def test_snapshot_unescapes_spaces(ws, state):
    """§9.3 percent-escapes spaces to keep the relpath a single trailing token."""
    (ws / "my secret").write_text("x", encoding="utf-8")
    (state / "mask-snapshot.txt").write_text("user my%20secret\n", encoding="utf-8")

    entries = jail.masked_from_snapshot(state, ws)

    assert entries[0]["relpath"] == "my secret"


def test_missing_snapshot_is_empty_not_an_error(ws, state):
    """A run before any mask-scan must not crash the jail."""
    assert jail.masked_from_snapshot(state, ws) == []


def test_vanished_snapshot_path_defaults_to_file(ws, state):
    """Safe direction: an empty-file overmount, not a skipped bind."""
    (state / "mask-snapshot.txt").write_text("floor gone.pem\n", encoding="utf-8")

    entries = jail.masked_from_snapshot(state, ws)

    assert entries[0]["type"] == "file"
