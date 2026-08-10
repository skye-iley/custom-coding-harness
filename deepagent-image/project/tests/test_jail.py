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


# --- LSM / AppArmor diagnosis (M4 slice J, invariant 37) ----------------------
#
# The point of these: an AppArmor mount denial and a seccomp/userns refusal need
# OPPOSITE remedies, and are distinguishable only by how far bwrap got. Reporting
# the first as the second sends an operator to re-check a profile that is already
# correct -- the dead end this milestone's own CI hit (milestone4.md §11.6).


def test_lsm_mount_denial_is_not_classified_as_a_userns_problem():
    # Verbatim stderr from ubuntu-latest under docker-default, with the vendored
    # seccomp profile applied. `unshare` succeeded; the first mount was denied.
    err = "bwrap: Failed to make / slave: Permission denied"
    assert jail.classify_bwrap_failure(err) == "lsm"


def test_userns_refusal_still_classified_as_userns():
    err = "bwrap: No permissions to create new namespace, likely because the kernel does not allow it"
    assert jail.classify_bwrap_failure(err) == "userns"


@pytest.mark.parametrize(
    "err",
    [
        "bwrap: Can't mount proc on /newroot/proc: Permission denied",
        "bwrap: Unable to mount source on /newroot/usr: Permission denied",
        "bwrap: Failed to mount tmpfs: Permission denied",
    ],
)
def test_other_mount_phase_denials_are_lsm(err):
    assert jail.classify_bwrap_failure(err) == "lsm"


def test_mount_failure_without_permission_denied_is_not_lsm():
    # ENOENT-shaped mount failures are a bad bind path, not a policy denial --
    # classifying them as `lsm` would make the gate skip a real regression.
    err = "bwrap: Can't mount proc on /newroot/proc: No such file or directory"
    assert jail.classify_bwrap_failure(err) != "lsm"


def test_unrecognized_failure_is_unknown_not_silently_skipped():
    assert jail.classify_bwrap_failure("bwrap: something nobody predicted") == "unknown"
    assert jail.classify_bwrap_failure("") == "unknown"


def test_apparmor_confinement_reads_the_profile_name(tmp_path, monkeypatch):
    attr = tmp_path / "apparmor_current"
    attr.write_text("docker-default (enforce)")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))
    assert jail.apparmor_confinement() == "docker-default"


def test_apparmor_confinement_treats_unconfined_as_absent(tmp_path, monkeypatch):
    attr = tmp_path / "apparmor_current"
    attr.write_text("unconfined")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))
    assert jail.apparmor_confinement() is None


def test_apparmor_confinement_is_none_when_no_lsm_present(tmp_path, monkeypatch):
    # Non-Linux and hosts where neither attr file exists. Not an error.
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(tmp_path / "nope"))
    monkeypatch.setattr(jail, "_LEGACY_ATTR_PATH", str(tmp_path / "also-nope"))
    assert jail.apparmor_confinement() is None


def test_empty_apparmor_attr_does_not_fall_through_to_the_legacy_one(tmp_path, monkeypatch):
    """Regression: measured on Docker Desktop/WSL2, where the jail actually works.

    /proc/self/attr/apparmor/current exists but is EMPTY (AppArmor compiled in, no
    profile on this task) while the legacy shared /proc/self/attr/current reports
    NUL-terminated `kernel`. Falling through reported 'confined by AppArmor profile
    "kernel"', which would make doctor hard-error on the one host class slice H was
    verified on.
    """
    specific = tmp_path / "apparmor_current"
    specific.write_text("")
    legacy = tmp_path / "current"
    legacy.write_bytes(b"kernel" + bytes(1))  # NUL-terminated, as the real attr file is
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(specific))
    monkeypatch.setattr(jail, "_LEGACY_ATTR_PATH", str(legacy))
    assert jail.apparmor_confinement() is None


def test_legacy_kernel_sentinel_is_not_a_profile(tmp_path, monkeypatch):
    legacy = tmp_path / "current"
    legacy.write_bytes(b"kernel" + bytes(1))  # NUL-terminated, as the real attr file is
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(tmp_path / "nope"))
    monkeypatch.setattr(jail, "_LEGACY_ATTR_PATH", str(legacy))
    assert jail.apparmor_confinement() is None


def test_nul_terminated_profile_name_is_cleaned(tmp_path, monkeypatch):
    """str.strip() does not remove NULs -- the raw attr files are NUL-terminated."""
    attr = tmp_path / "apparmor_current"
    attr.write_bytes(b"docker-default (enforce)" + bytes(1))
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))
    assert jail.apparmor_confinement() == "docker-default"


def test_apparmor_confinement_falls_back_to_the_legacy_attr_path(tmp_path, monkeypatch):
    legacy = tmp_path / "current"
    legacy.write_text("docker-default (enforce)")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(tmp_path / "nope"))
    monkeypatch.setattr(jail, "_LEGACY_ATTR_PATH", str(legacy))
    assert jail.apparmor_confinement() == "docker-default"


def test_apparmor_hint_names_both_remedies():
    hint = jail.apparmor_hint()
    assert "DEEPAGENTS_JAIL_APPARMOR=unconfined" in hint
    assert "§11.6" in hint
    # Must not send the operator back to the seccomp profile: it is already correct
    # in this failure mode, and that misdirection is the whole point of invariant 37.
    assert "seccomp=" not in hint


# --- confinement detail: profile + enforcement mode (M4 slice J, invariant 40) --


def test_apparmor_confinement_detail_reports_the_mode(tmp_path, monkeypatch):
    """The mode is the difference between an enforced boundary and a logged one."""
    attr = tmp_path / "apparmor_current"
    attr.write_text("deepagent-userns (enforce)")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))

    assert jail.apparmor_confinement_detail() == ("deepagent-userns", "enforce")


def test_apparmor_confinement_detail_surfaces_complain_mode(tmp_path, monkeypatch):
    """A complain-mode profile logs violations and ALLOWS them.

    bwrap would run and the LSM would be enforcing nothing, so doctor has to be
    able to tell this apart from a real load rather than seeing only the name.
    """
    attr = tmp_path / "apparmor_current"
    attr.write_text("deepagent-userns (complain)")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))

    assert jail.apparmor_confinement_detail() == ("deepagent-userns", "complain")


def test_apparmor_confinement_detail_keeps_child_profiles_intact(tmp_path, monkeypatch):
    """AppArmor reports sub-profiles as `parent//child`; callers match the parent."""
    attr = tmp_path / "apparmor_current"
    attr.write_text("deepagent-userns//bwrap (enforce)")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))

    profile, mode = jail.apparmor_confinement_detail()

    assert profile == "deepagent-userns//bwrap"
    assert profile.split("//")[0] == "deepagent-userns"
    assert mode == "enforce"


def test_apparmor_confinement_detail_is_empty_when_unconfined(tmp_path, monkeypatch):
    attr = tmp_path / "apparmor_current"
    attr.write_text("unconfined")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))

    assert jail.apparmor_confinement_detail() == (None, None)


def test_apparmor_confinement_detail_survives_a_missing_mode_suffix(tmp_path, monkeypatch):
    """Not every kernel/version appends "(mode)". A bare name must still parse."""
    attr = tmp_path / "apparmor_current"
    attr.write_text("docker-default")
    monkeypatch.setattr(jail, "_APPARMOR_ATTR_PATH", str(attr))

    assert jail.apparmor_confinement_detail() == ("docker-default", None)
