"""Namespace guard — denylist tripwire for the syscalls slice H's seccomp relaxation exposes.

**Why this exists.** Turning on the fs jail (`DEEPAGENTS_JAIL=1`) requires running
the container under `seccomp/userns.json`, which re-permits five syscalls Docker's
default profile blocks: ``clone`` (with ``CLONE_NEW*`` flags), ``unshare``,
``mount``, ``umount2`` and ``pivot_root``. That filter is applied by `docker run`
to the **whole container** — there is no way to grant it to the harness process
alone — so enabling the inner jail necessarily hands the agent's *shell tool* the
same five syscalls (milestone4.md §11.5, `seccomp/README.md`).

This module is the compensating layer: a denylist over the shell tool's command
string for the common, easily-recognised ways a process reaches those syscalls.

**Be honest about what this is.** It is a **tripwire, not containment.** A
command-string denylist is phrasing-blind — exactly the caveat M3 already records
for `review_triggers`, where a denied ``rm -rf`` came back as ``rmdir``. Anything
that compiles a C file, base64-decodes a payload, indirects through a variable, or
calls the syscall from a language runtime this does not pattern-match will sail
straight through. The real boundary remains the container plus (when on) the jail's
bind set. What this buys is that the *casual* and *scripted* attempts — which is
what an opportunistic prompt-injected agent actually emits — are refused and, more
importantly, **recorded**, so an escape attempt leaves evidence instead of a
silence.

Pure/stdlib and imports no harness sibling, same acyclic rule as `pathguard`.
"""

from __future__ import annotations

import os
import re
import shlex

#: Binaries whose whole purpose is to create/enter a namespace, change the root,
#: or drive a container runtime. None has a legitimate use inside a coding agent's
#: workspace shell, which is what keeps the false-positive rate low enough to
#: refuse on rather than merely warn.
DENIED_BINARIES = frozenset({
    # direct users of the five relaxed syscalls
    "unshare", "nsenter", "setns", "pivot_root", "switch_root",
    "mount", "umount", "chroot",
    # sandbox builders (bwrap is what the harness itself uses; the agent has no
    # business invoking it, and `sandbox-exec` is our own wrapper around it)
    "bwrap", "bubblewrap", "sandbox-exec", "firejail", "proot", "systemd-nspawn",
    # container runtimes / CLIs — all of them nest namespaces for a living
    "docker", "podman", "nerdctl", "ctr", "runc", "crun",
    "lxc-start", "lxc-execute", "lxc-attach", "singularity", "apptainer",
    # capability/namespace manipulation helpers
    "capsh", "setpriv", "losetup",
})

#: Command prefixes that wrap another command. Skipped so `sudo mount ...` and
#: `timeout 5 unshare ...` resolve to the real binary rather than to the wrapper.
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "nohup", "time", "timeout", "stdbuf", "nice",
    "ionice", "setsid", "command", "exec", "builtin", "eval", "xargs", "watch",
})

#: High-signal identifiers scanned anywhere in the command, to catch the
#: interpreter one-liner route (`python -c "...ctypes...unshare(...)"`) that never
#: puts a denied *binary* in command position. Deliberately narrow: each of these
#: is rare enough in ordinary prose/code that matching it is not a coin flip.
#: Bare `mount` is NOT here — it is common English, and the binary check above
#: already covers the executable form.
_TOKEN_PATTERNS = (
    (re.compile(r"\bpivot_root\b"), "pivot_root"),
    # No leading \b: the flag form `-DCLONE_NEWUSER` has a word char before the
    # C, so an anchored boundary would miss exactly the compile-time route.
    (re.compile(r"CLONE_NEW[A-Z]+\b"), "CLONE_NEW* namespace flag"),
    (re.compile(r"\bunshare\s*\("), "unshare() call"),
    (re.compile(r"\bsetns\s*\("), "setns() call"),
    (re.compile(r"\bos\.unshare\b"), "os.unshare()"),
    # x86_64 syscall numbers for unshare / pivot_root / setns, as used in a raw
    # `syscall(...)` one-liner.
    (re.compile(r"\bsyscall\s*\(\s*(?:272|155|308)\b"), "raw syscall() to a namespace syscall"),
)

#: Shell operators that separate one command from the next. Split on these so
#: `ls && unshare -Ur sh` is scanned as two segments, not one.
_SEPARATORS = re.compile(r"(?:\|\||&&|[;&|\n]|\$\(|`)")

GUARD_ENV = "DEEPAGENTS_NS_GUARD"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_BLOCK = "block"


class NamespaceGuardDenied(PermissionError):
    """Raised when the shell command matches the namespace denylist.

    Carries the matched token so the audit record can say *what* tripped without
    persisting the whole command (which may contain workspace content).
    """

    def __init__(self, match: str, reason: str):
        self.match = match
        self.reason = reason
        super().__init__(
            f"namespace guard: refused — {reason} ({match!r}). The container's "
            "seccomp profile permits namespace syscalls only so the harness can "
            "build its own fs jail; the agent shell has no legitimate use for them."
        )


def guard_mode(env=None, jail_on: bool = False) -> str:
    """Resolve the guard's mode: ``block`` | ``warn`` | ``off``.

    Default tracks the jail, which is the point: the guard compensates for a
    seccomp relaxation that is only applied when ``DEEPAGENTS_JAIL=1``, so with
    the jail off there is nothing to compensate for and the shell behaves exactly
    as in M3/slices A–G. That keeps the removable contract (milestone4.md §13)
    intact — this module adds no always-on behaviour change.

    ``DEEPAGENTS_NS_GUARD`` overrides in both directions: ``0``/``false``/``off``
    disables it even under the jail, ``warn`` logs without refusing (the escape
    hatch when a denylist entry collides with real work), and ``1``/``true``/
    ``block`` forces it on even with the jail off.
    """
    env = os.environ if env is None else env
    raw = (env.get(GUARD_ENV) or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return MODE_OFF
    if raw == "warn":
        return MODE_WARN
    if raw in {"1", "true", "yes", "block", "on"}:
        return MODE_BLOCK
    return MODE_BLOCK if jail_on else MODE_OFF


def _segments(command: str):
    """Split a command line into individually-scannable segments."""
    return [seg for seg in _SEPARATORS.split(command) if seg and seg.strip()]


def _candidate_binaries(segment: str) -> list[str]:
    """Best-effort: the executable(s) a segment would actually run.

    Skips leading ``VAR=value`` assignments and known wrapper commands (plus the
    wrapper's own flags), so `sudo -n mount` resolves to ``mount``. Falls back to
    a whitespace split when the segment does not lex as valid shell — an unbalanced
    quote must not make the guard silently scan nothing.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()

    found: list[str] = []
    after_wrapper = False
    for token in tokens:
        if not token:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue  # env assignment prefix
        name = os.path.basename(token.strip("'\"")).lower()
        if name in _WRAPPERS:
            # Past a wrapper, scan EVERY remaining token rather than just the
            # next one: a wrapper's own positional args sit between it and the
            # real command (`timeout 5 unshare ...`), so "the token right after
            # the wrapper" is not reliably the binary.
            after_wrapper = True
            continue
        if token.startswith("-"):
            continue  # a flag, not the command
        found.append(name)
        if not after_wrapper:
            break  # no wrapper seen: argv[0] is the command, full stop
    return found


def scan(command: str) -> tuple[str, str] | None:
    """Scan a shell command for namespace-syscall use.

    Returns ``(match, reason)`` on a hit, or ``None`` when the command is clean.
    Pure — no IO, no env reads — so it is trivially testable and callable from
    either the sync or async execute path.
    """
    if not command or not command.strip():
        return None

    for segment in _segments(command):
        for name in _candidate_binaries(segment):
            if name in DENIED_BINARIES:
                return name, f"{name!r} creates or enters a namespace"

    for pattern, label in _TOKEN_PATTERNS:
        hit = pattern.search(command)
        if hit:
            return hit.group(0), label

    return None
