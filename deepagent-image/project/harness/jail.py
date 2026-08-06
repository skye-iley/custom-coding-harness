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


class JailUnavailable(RuntimeError):
    """The jail was requested but cannot be built. Always fatal -- never degrade."""


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
        problems.append(
            f"bwrap cannot create a user namespace here ({hint}). The container most "
            "likely lacks the narrow seccomp profile -- run it with "
            "--security-opt seccomp=deepagent-image/seccomp/userns.json "
            "(see seccomp/README.md)."
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
