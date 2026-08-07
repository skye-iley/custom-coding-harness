"""Namespace guard — the denylist backstop for slice H's seccomp relaxation.

Host-runnable (stdlib only): `nsguard` imports no harness sibling.
"""

import pytest

from _bootstrap import _load

nsguard = _load("nsguard")


# --- the thing it exists to catch -------------------------------------------

@pytest.mark.parametrize("command", [
    "unshare -Ur /bin/sh",
    "nsenter -t 1 -m -u -i -n -p sh",
    "mount -o bind /project/state /tmp/x",
    "umount /project/workspace",
    "pivot_root . old",
    "chroot /tmp/root /bin/sh",
    "bwrap --unshare-all --bind / / sh",
    "sandbox-exec exec -- sh",
    "docker run --privileged -v /:/host alpine",
    "podman run --rm -it alpine",
    "runc run foo",
    "capsh --print",
    "setpriv --reuid 0 sh",
])
def test_denied_binaries_are_caught(command):
    hit = nsguard.scan(command)
    assert hit is not None, f"{command!r} should trip the guard"


def test_absolute_path_to_a_denied_binary_is_caught():
    # basename matching -- an absolute path must not launder the call.
    assert nsguard.scan("/usr/bin/unshare -Ur sh") is not None


@pytest.mark.parametrize("command", [
    "sudo mount -o bind /a /b",
    "sudo -n unshare -Ur sh",
    "env -i unshare -Ur sh",
    "timeout 5 unshare -Ur sh",
    "nohup nsenter -t 1 -m sh",
])
def test_wrapper_prefixes_do_not_launder_the_call(command):
    assert nsguard.scan(command) is not None


@pytest.mark.parametrize("command", [
    "ls && unshare -Ur sh",
    "ls; mount -o bind /a /b",
    "true || nsenter -t 1 -m sh",
    "echo hi | unshare -Ur sh",
    "ls\nunshare -Ur sh",
])
def test_later_segments_are_scanned_not_just_the_first(command):
    # The obvious bypass: hide the real command behind a benign first one.
    assert nsguard.scan(command) is not None


def test_env_assignment_prefix_is_skipped():
    assert nsguard.scan("FOO=bar BAZ=qux unshare -Ur sh") is not None


@pytest.mark.parametrize("command,expect", [
    ("python3 -c \"import ctypes; ctypes.CDLL('libc.so.6').unshare(0x10000000)\"", "unshare("),
    ("python3 -c 'import os; os.unshare(os.CLONE_NEWUSER)'", None),  # matches CLONE_NEW* or os.unshare
    ("gcc -DCLONE_NEWUSER x.c", "CLONE_NEWUSER"),
    ("perl -e 'syscall(272, 0x10000000)'", "syscall("),
    ("python3 -c 'setns(fd, 0)'", "setns("),
])
def test_interpreter_one_liners_are_caught(command, expect):
    # The route that never puts a denied *binary* in command position.
    hit = nsguard.scan(command)
    assert hit is not None
    if expect:
        assert expect.split("(")[0] in hit[0] or expect in hit[0]


# --- and what it must NOT catch ---------------------------------------------

@pytest.mark.parametrize("command", [
    "ls -la",
    "pytest tests/ -v",
    "git commit -m 'fix the mount point docs'",
    "echo 'mountains are tall'",
    "python3 -m pip install requests",
    "grep -r paramount .",
    "cat README.md",
    "npm run build",
    # 'mount' as prose, not as a command -- the binary check is position-aware
    # and bare `mount` is deliberately absent from the token patterns.
    "echo 'you should mount the volume first'",
])
def test_ordinary_commands_are_not_flagged(command):
    assert nsguard.scan(command) is None, f"{command!r} is a false positive"


def test_empty_command_is_clean():
    assert nsguard.scan("") is None
    assert nsguard.scan("   ") is None


def test_unlexable_command_still_scans():
    # An unbalanced quote must not make shlex raise and the guard scan nothing.
    assert nsguard.scan("unshare -Ur 'sh") is not None


# --- mode resolution ---------------------------------------------------------

def test_default_tracks_the_jail():
    # The guard compensates for a relaxation only applied when the jail is on,
    # so with the jail off it is inert -- that is what preserves M3 parity.
    assert nsguard.guard_mode(env={}, jail_on=False) == nsguard.MODE_OFF
    assert nsguard.guard_mode(env={}, jail_on=True) == nsguard.MODE_BLOCK


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF"])
def test_env_can_disable_even_under_the_jail(raw):
    assert nsguard.guard_mode(env={"DEEPAGENTS_NS_GUARD": raw}, jail_on=True) == nsguard.MODE_OFF


@pytest.mark.parametrize("raw", ["1", "true", "block", "on"])
def test_env_can_force_on_with_the_jail_off(raw):
    assert nsguard.guard_mode(env={"DEEPAGENTS_NS_GUARD": raw}, jail_on=False) == nsguard.MODE_BLOCK


def test_warn_mode_is_distinct_from_block():
    assert nsguard.guard_mode(env={"DEEPAGENTS_NS_GUARD": "warn"}, jail_on=True) == nsguard.MODE_WARN
    assert nsguard.guard_mode(env={"DEEPAGENTS_NS_GUARD": "warn"}, jail_on=False) == nsguard.MODE_WARN


def test_denied_exception_carries_the_match_not_the_command():
    # The audit record must never persist the command string (it can carry
    # workspace content); the exception is the shape that feeds it.
    exc = nsguard.NamespaceGuardDenied("unshare", "'unshare' creates or enters a namespace")
    assert exc.match == "unshare"
    assert "unshare" in str(exc)
