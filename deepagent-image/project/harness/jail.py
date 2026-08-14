"""Bubblewrap fs jail for the agent's tools (M4 slice H, milestone4.md §11.4).

The harness **re-execs itself into a bwrap mount namespace at startup**. Every
tool in the process then inherits that namespace: the in-process deepagents file
tools (read/write/edit/ls/glob/grep) see a filesystem where masked and
designated-secret paths are *physically absent*, with upstream deepagents code
running completely untouched. The shell tool goes one further and runs in a
**nested** jail via `scripts/sandbox-exec.sh`, which binds only the workspace --
so the shell finally loses the reach into the state dir that invariant 17a
documented as its one standing limit.

Why re-exec instead of jailing each tool call: a per-call jailed worker cannot
import deepagents (`import deepagents.backends.local_shell` measures ~2.2 s vs
~10 ms for bare `python3 -S`), so it would have to reimplement the file-tool
method bodies and carry permanent drift risk against upstream. Re-exec has no
worker, no protocol, no reimplementation and no per-op cost. See milestone4.md
§16 fork 8.

**The state dir stays bound here, and that is fine.** The harness needs
`checkpoints.sqlite`. It does not re-open invariant 17a because the file tools
still cannot reach the state dir -- `pathguard.validate_path` already refuses
absolute paths outside the workspace -- and the shell can no longer reach it at
all. Net, the jail *closes* 17a's shell gap rather than widening anything.

**Fail closed.** If the jail is requested but cannot be built, the harness aborts
at startup rather than continuing unjailed: silently degrading would leave the
operator believing in a boundary that is not there.

Off by default (`DEEPAGENTS_JAIL`, milestone4.md §13). Enabling it needs the
narrow seccomp relaxation in `deepagent-image/seccomp/userns.json`, which exposes
kernel user-namespace surface -- a deliberate operator trade, not a silent
default.

Imports no harness sibling (same acyclic discipline as mask.py / cost.py), so
`doctor` and the tests both reuse it without a cycle.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Set on the child so the re-exec happens exactly once and cannot loop.
JAILED_MARKER = "DEEPAGENTS_JAILED"
JAIL_ENV = "DEEPAGENTS_JAIL"

# Read-only system binds. /opt carries both python stacks (the harness venv at
# /opt/venv and Miniforge at /opt/conda), so it is not optional.
_SYSTEM_ROBINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")


# Where the kernel exposes this process's AppArmor confinement. The first path is
# the modern per-LSM location and is AUTHORITATIVE when it exists -- it is empty
# exactly when AppArmor is compiled in but this task carries no profile. The second
# is the legacy location shared with other LSMs, consulted only as a fallback.
_APPARMOR_ATTR_PATH = "/proc/self/attr/apparmor/current"
_LEGACY_ATTR_PATH = "/proc/self/attr/current"

# Values that mean "nothing is confining this process". `kernel` is what the shared
# legacy attr reports on a host with no AppArmor policy loaded (measured on Docker
# Desktop/WSL2, where the jail works fine) -- treating it as a profile name would
# make doctor hard-error on exactly the host class slice H was verified on.
_UNCONFINED_VALUES = frozenset({"unconfined", "kernel"})


def _read_attr(path: str) -> str | None:
    """Contents of an LSM attr file, or None when it does not exist / is unreadable.

    An existing-but-empty file returns "" (not None): for the AppArmor-specific
    path that is a meaningful answer -- no profile -- and must not fall through to
    the legacy one.
    """
    try:
        with open(path) as fh:
            # These are NUL-terminated, and `str.strip()` does not remove NULs.
            return fh.read().replace("\0", "").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _profile_from(raw: str) -> str | None:
    profile, _ = _profile_and_mode_from(raw)
    return profile


def _profile_and_mode_from(raw: str) -> tuple[str | None, str | None]:
    """Split an attr value into (profile, mode).

    Format is "<profile> (<mode>)", e.g. "docker-default (enforce)". The mode
    matters for slice J: a profile loaded in *complain* mode logs violations and
    allows them, so bwrap would run and the LSM would be enforcing nothing --
    a pass that reports a boundary which is not there (invariant 40).
    """
    if not raw:
        return None, None
    head, _, tail = raw.partition(" (")
    profile = head.strip()
    mode = tail.rstrip(")").strip().lower() or None
    if not profile or profile in _UNCONFINED_VALUES:
        return None, None
    return profile, mode


class JailUnavailable(RuntimeError):
    """The jail was requested but cannot be built. Always fatal -- never degrade."""


def apparmor_confinement() -> str | None:
    """This process's AppArmor profile, or None when AppArmor is not in force.

    "unconfined" is reported as None: an unconfined process is not constrained by
    AppArmor, so for diagnostic purposes it is the same as AppArmor being absent.
    """
    specific = _read_attr(_APPARMOR_ATTR_PATH)
    if specific is not None:
        # Authoritative, including when empty: AppArmor is present and this task
        # has no profile. Do NOT fall through -- the legacy shared attr reports
        # `kernel` here, which would read as a confinement that does not exist.
        return _profile_from(specific)

    legacy = _read_attr(_LEGACY_ATTR_PATH)
    if legacy is not None:
        return _profile_from(legacy)
    return None


def apparmor_confinement_detail() -> tuple[str | None, str | None]:
    """(profile, mode) for this process, or (None, None) when unconfined.

    Same resolution order as `apparmor_confinement`, which is the name-only
    view kept for callers that do not care about enforcement mode. Two shapes
    the kernel really emits and this must survive: a mode suffix
    ("deepagent-userns (enforce)") and a child profile ("parent//child"), which
    is reported as-is -- slice J's doctor check matches on the parent segment.
    """
    specific = _read_attr(_APPARMOR_ATTR_PATH)
    if specific is not None:
        return _profile_and_mode_from(specific)
    legacy = _read_attr(_LEGACY_ATTR_PATH)
    if legacy is not None:
        return _profile_and_mode_from(legacy)
    return None, None


# bwrap's mount-phase failure fingerprints. "Failed to make / slave" is the first
# one it can hit, and the one docker-default produces; the others cover the same
# denial surfacing at a later bind/pivot.
_MOUNT_PHASE_MARKERS = (
    "failed to make / slave",
    "failed to make / rslave",
    "can't mount",
    "unable to mount",
    "failed to mount",
    "pivot_root",
)


def classify_bwrap_failure(stderr: str) -> str:
    """Why bwrap could not build the namespace:
    'userns' | 'lsm' | 'procfs' | 'unknown'.

    The distinction is not cosmetic (milestone4.md §11.6, invariant 37). There are
    **three independent gates**, all of which must allow, and each needs a
    different remedy:

      userns  the `unshare` itself was refused. Docker's default seccomp profile
              blocks unprivileged userns creation. Fix: pass seccomp/userns.json.
      lsm     `unshare` SUCCEEDED and the first mount was denied. Docker's
              generated `docker-default` AppArmor profile carries a literal
              `deny mount,`, which no seccomp change affects and which entering a
              user namespace does not shed. Fix: slice J's profile, or
              DEEPAGENTS_JAIL_APPARMOR=unconfined.
      procfs  seccomp AND the LSM both allowed, and the kernel still refused the
              *fresh* procfs mount: `mount_too_revealing()` (fs/namespace.c) bars
              it from a non-initial user namespace while the visible procfs is
              covered by submounts -- which is exactly what Docker's OCI
              maskedPaths/readonlyPaths are. Fix: `--security-opt
              systempaths=unconfined` (milestone4.1.md §13.7, fork J5).

    `procfs` and `lsm` are told apart by **errno, not phase**: both fail at a
    mount, but the LSM denial is EACCES ("Permission denied") and the procfs gate
    is EPERM ("Operation not permitted"), with no AppArmor denial logged. That is
    the only signal there is -- an operator who reads "Operation not permitted" as
    an LSM problem goes and re-checks a profile that is already correct, which is
    the dead end the §13.1 measurement spent a round on.
    """
    text = (stderr or "").lower()
    if "no permissions to create new namespace" in text or "unshare" in text:
        return "userns"
    if any(marker in text for marker in _MOUNT_PHASE_MARKERS):
        if "permission denied" in text:
            return "lsm"
        if "operation not permitted" in text and "proc" in text:
            return "procfs"
    if "namespace" in text:
        return "userns"
    return "unknown"


# Where the kernel reports this mount namespace's mounts. Read rather than
# assumed: whether the container's procfs is covered is a property of how *this*
# container was started (`--security-opt systempaths=unconfined` or not), and a
# forwarded flag would only report what the launcher believes.
_MOUNTINFO_PATH = "/proc/self/mountinfo"


def procfs_covering_mounts(mountinfo: str | None = None) -> list[str]:
    """Mount points *underneath* /proc, i.e. what makes procfs "too revealing".

    Docker's default `maskedPaths`/`readonlyPaths` install 13 of these
    (/proc/kcore, /proc/sys, ...). While any exist, the kernel refuses a fresh
    `--proc` mount from a non-initial user namespace, independently of seccomp
    and AppArmor (milestone4.1.md §13.7).

    `mountinfo` is injectable so the parse is host-testable; the default reads
    /proc/self/mountinfo. An unreadable file yields [] -- "no evidence of
    covering mounts" -- because the caller uses this to *explain* a failure, and
    a missing file must not manufacture a diagnosis.
    """
    if mountinfo is None:
        try:
            with open(_MOUNTINFO_PATH) as fh:
                mountinfo = fh.read()
        except OSError:
            return []
    covered: list[str] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        # mountinfo: id parent major:minor root MOUNT-POINT options...
        if len(fields) < 5:
            continue
        target = fields[4]
        if target.startswith("/proc/"):
            covered.append(target)
    return covered


def procfs_hint() -> str:
    """Operator-facing remedy for the third gate, naming what is covering procfs."""
    covered = procfs_covering_mounts()
    detail = (
        f" This container's procfs carries {len(covered)} covering mount(s) "
        f"(e.g. {', '.join(covered[:3])})."
        if covered
        else ""
    )
    return (
        "the user namespace was created and the mounts were allowed, then the kernel "
        "refused bwrap's fresh /proc: mount_too_revealing() bars a new procfs from a "
        "non-initial user namespace while the visible one is covered by submounts, "
        "which is what Docker's maskedPaths/readonlyPaths are." + detail + " Neither "
        "seccomp nor AppArmor is the problem here (no LSM denial is logged). Relaunch "
        "with --security-opt systempaths=unconfined -- run-docker/smoke pass it "
        "automatically when DEEPAGENTS_JAIL=1 unless "
        "DEEPAGENTS_JAIL_SYSTEMPATHS=default overrides it (milestone4.1.md §13.7)."
    )


def apparmor_hint() -> str:
    """Operator-facing remedy for an LSM mount denial, naming the live profile."""
    profile = apparmor_confinement()
    named = f"AppArmor profile '{profile}'" if profile else "an AppArmor profile"
    return (
        f"the user namespace was created, then the first mount was denied by {named} "
        "-- Docker's default profile carries `deny mount,`, which the seccomp profile "
        "has no bearing on. Either load the narrowed profile (milestone4.md §11.6, "
        "slice J) or relaunch with DEEPAGENTS_JAIL_APPARMOR=unconfined, which works "
        "everywhere but drops the whole profile rather than one rule."
    )


def jail_enabled(env: dict[str, str] | None = None) -> bool:
    """True when the operator opted into the jail. Default off (§13)."""
    source = os.environ if env is None else env
    return (source.get(JAIL_ENV) or "0").strip().lower() in ("1", "true", "yes", "on")


def already_jailed(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return bool(source.get(JAILED_MARKER))


def bwrap_args(
    workspace: str | Path,
    state_dir: str | Path | None,
    masked: list[dict] | None = None,
    *,
    empty_file: str | Path | None = None,
    unshare_net: bool = False,
    extra_robinds: tuple[str, ...] = (),
    project_root: str | Path | None = None,
) -> list[str]:
    """Build the bwrap argument list for the harness's own namespace.

    `masked` is the resolved `MaskResult.masked` list from `mask.resolve` -- the
    same §9.3 contract the docker mask consumes, so policy is resolved once and
    stays enforcement-agnostic. Each entry needs `relpath` and `type`.

    Masked entries are overmounted *inside* the jail: a masked dir becomes an
    empty tmpfs, a masked file an empty read-only bind. This is what makes the
    floor's fourth leg (invariant 5) independent of the docker mask -- with the
    jail on, a floor path is unreadable even if the docker overlay were disabled
    or misconfigured.

    Ordering is deliberate and stable: system binds, then the workspace, then the
    state dir, then the masked overmounts *last* so they layer on top of the
    workspace bind (same "later mount wins" reasoning as the docker mask, §11.1).
    A stable list also lets the tests assert the whole argv.
    """
    workspace = Path(workspace)
    args: list[str] = []

    for path in (*_SYSTEM_ROBINDS, *extra_robinds):
        # A bind of a missing source is a hard bwrap error, so skip absent ones
        # (/lib64 does not exist on some arches).
        if Path(path).exists():
            args += ["--ro-bind", path, path]

    # The harness's own root (/project in-container): harness/ itself plus
    # AGENTS.md, providers/, workflows/, .mcp.json -- all of which the re-exec'd
    # process still has to import and read. Read-only: the agent has no business
    # writing here, and the workspace/state binds below layer read-write on top
    # of it, so this must come first.
    if project_root is not None:
        project_root = Path(project_root)
        if project_root.exists():
            args += ["--ro-bind", str(project_root), str(project_root)]

    args += ["--bind", str(workspace), str(workspace)]

    if state_dir is not None:
        state_dir = Path(state_dir)
        args += ["--bind", str(state_dir), str(state_dir)]

    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    args += [
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
    ]
    # The harness itself makes the model API calls, so its namespace keeps the
    # network. Dropping egress is the *shell*'s nested jail's job -- that is
    # exactly the install/exec split sandbox-exec already implements.
    if unshare_net:
        args += ["--unshare-net"]

    for entry in masked or []:
        rel = entry["relpath"] if isinstance(entry, dict) else entry.relpath
        kind = entry["type"] if isinstance(entry, dict) else entry.type
        target = str(workspace / rel)
        if kind == "dir":
            args += ["--tmpfs", target]
        elif empty_file is not None:
            args += ["--ro-bind", str(empty_file), target]

    args += ["--chdir", str(Path.cwd())]
    return args


def masked_from_snapshot(
    state_dir: str | Path, workspace: str | Path
) -> list[dict]:
    """Read the mask set `mask-scan` froze at launch (§9.2 mask-snapshot.txt).

    Deliberately reads the **frozen** snapshot rather than re-running
    `mask.resolve`: invariant 9 says the mask set is computed once host-side
    before `docker run`, so the jail must overmount exactly what the launcher
    resolved, not a fresh resolution that could differ.

    The snapshot's `<tier> <relpath>` grammar has no dir/file column (git-pr
    consumes the same file, §15.1, and does not need one), so the type is
    stat'd here instead of widening that contract. A path that has since
    vanished stats as a file and gets an empty-file overmount, which is the
    safe direction.
    """
    path = Path(state_dir) / "mask-snapshot.txt"
    entries: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return entries

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        tier, rel = parts
        rel = rel.replace("%20", " ")  # §9.3 escapes spaces to keep the tail single-token
        target = Path(workspace) / rel
        entries.append(
            {
                "relpath": rel,
                "type": "dir" if target.is_dir() else "file",
                "tier": tier,
            }
        )
    return entries


def ensure_empty_file(state_dir: str | Path) -> Path:
    """A zero-byte file to overmount masked *files* with, inside the jail.

    Lives in the state dir (already bound, and outside the workspace so the
    agent cannot swap it for something non-empty). Not /dev/null -- the same
    portability reason §11.1 gives for the docker mask.
    """
    target = Path(state_dir) / ".empty"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.touch()
    return target


def preflight() -> list[str]:
    """Check the jail can actually be built here. Returns problems; empty = good.

    Run before re-exec so the failure is a clear startup message rather than a
    bwrap error deep in an exec. The userns probe is the real check -- bwrap
    being installed says nothing about whether seccomp will let it unshare
    (that is exactly the gate milestone4.md §17/PR6 set for this slice).
    """
    problems: list[str] = []

    if not shutil.which("bwrap"):
        problems.append(
            "bubblewrap (bwrap) is not installed in the image -- cannot build the fs jail"
        )
        return problems

    # The probe must bind enough for the test binary to actually load: `true` is a
    # dynamically linked ELF, so a namespace with only /usr yields
    # "execvp true: No such file or directory" (a missing loader) which reads
    # exactly like a userns refusal and is not one. Bind the system paths the real
    # jail binds, and run through /bin/true by absolute path.
    probe_binds: list[str] = []
    for path in _SYSTEM_ROBINDS:
        if Path(path).exists():
            probe_binds += ["--ro-bind", path, path]

    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-all", *probe_binds, "--proc", "/proc", "/bin/true"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"could not run the bwrap userns probe: {exc}")
        return problems

    if probe.returncode != 0:
        detail = (probe.stderr or "").strip().splitlines()
        hint = detail[0] if detail else f"exit {probe.returncode}"
        kind = classify_bwrap_failure(probe.stderr or "")
        if kind == "lsm":
            # Do NOT blame seccomp here: the profile is almost certainly correct
            # and the operator would go re-check it for nothing (invariant 37).
            problems.append(f"bwrap cannot build the jail here ({hint}). {apparmor_hint()}")
        elif kind == "userns":
            problems.append(
                f"bwrap cannot create a user namespace here ({hint}). The container most "
                "likely lacks the narrow seccomp profile -- run it with "
                "--security-opt seccomp=deepagent-image/seccomp/userns.json "
                "(see seccomp/README.md)."
            )
        elif kind == "procfs":
            # The third gate. Blaming either of the other two here is the same
            # class of dead end invariant 37 exists to prevent, one layer further
            # in: both profiles are correct and the kernel still says no.
            problems.append(f"bwrap cannot build the jail here ({hint}). {procfs_hint()}")
        else:
            confined = apparmor_confinement()
            extra = f" This process is confined by AppArmor profile '{confined}'." if confined else ""
            problems.append(
                f"bwrap cannot build the jail here ({hint}).{extra} Check all three gates: "
                "the narrow seccomp profile (--security-opt seccomp=deepagent-image/seccomp/"
                "userns.json), the host LSM (milestone4.md §11.6), and the kernel's procfs "
                "restriction (--security-opt systempaths=unconfined, milestone4.1.md §13.7)."
            )

    return problems


def maybe_reexec(
    workspace: str | Path,
    state_dir: str | Path | None,
    masked: list[dict] | None = None,
    *,
    empty_file: str | Path | None = None,
) -> None:
    """Re-exec this process inside the jail, once. No-op when the jail is off.

    Returns normally in two cases: the jail is disabled, or we are already the
    jailed child. Otherwise it does not return at all -- `os.execv` replaces the
    process.

    Raises `JailUnavailable` when the jail was asked for but cannot be built.
    Callers must treat that as fatal (`cli.main` aborts): continuing unjailed
    after promising a boundary is the one outcome worse than not having it.
    """
    if not jail_enabled() or already_jailed():
        return

    problems = preflight()
    if problems:
        raise JailUnavailable("; ".join(problems))

    args = bwrap_args(
        workspace,
        state_dir,
        masked,
        empty_file=empty_file,
        project_root=Path.cwd(),
    )

    child_env_marker = f"{JAILED_MARKER}=1"
    # bwrap --setenv keeps the marker out of the parent's environ, so a failed
    # exec cannot leave this process looking jailed.
    argv = [
        "bwrap",
        *args,
        "--setenv",
        JAILED_MARKER,
        "1",
        "--",
        sys.executable,
        "-m",
        "harness",
        *sys.argv[1:],
    ]
    print(
        f"[harness] fs jail: re-exec into bwrap ({child_env_marker})",
        file=sys.stderr,
    )
    os.execvp("bwrap", argv)
