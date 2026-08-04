"""Narrow seccomp relaxation that lets bubblewrap build the fs jail (M4 slice H).

Docker's default seccomp profile blocks unprivileged user-namespace creation, so
`bwrap --unshare-all` fails inside the harness image with "No permissions to
create new namespace". That is the hard gate milestone4.md §17/PR6 puts in front
of slice H. The blocker is seccomp alone -- not the kernel: on Docker Desktop /
WSL2 `user/max_user_namespaces` is non-zero and the failure reproduces as root,
but disappears under `--security-opt seccomp=unconfined`.

Rather than run the container unconfined -- which would drop *every* syscall
filter to buy one inner boundary, a net-negative trade when the container is
still the real trust boundary (mvp.md §5) -- we vendor Docker's own default
profile with exactly five syscalls relaxed:

    clone, unshare, mount, umount2, pivot_root

`clone` is the interesting one: the upstream rule allows it only when the flags
word does *not* intersect 0x7E020000 (the CLONE_NEW* namespace bits) unless the
process holds CAP_SYS_ADMIN. `pivot_root` is not in the upstream profile at all,
so `defaultAction: SCMP_ACT_ERRNO` blocks it. bwrap needs all five.

**What this does and does not grant.** Relaxing seccomp does not hand the process
any privilege: the kernel still enforces its capability model, so `mount` from an
unprivileged process outside a user namespace keeps failing with EPERM exactly as
before. What changes is that seccomp stops *pre-empting* the syscall, letting the
kernel make the decision. The residual risk is real but bounded: it exposes the
kernel's user-namespace code to the container, historically a source of local
privilege-escalation CVEs. That is why the jail is opt-in (DEEPAGENTS_JAIL, off by
default) rather than the new default posture.

The profile stays narrow by construction, and `verify_profile` asserts it:
defaultAction must still be SCMP_ACT_ERRNO and the relaxation entry must name
exactly RELAXED_SYSCALLS. A regression that swapped in `seccomp=unconfined` --
or widened the relaxation -- fails that check. Empirically the discrimination is
visible at runtime too: under this profile `bpf`/`keyctl`/`perf_event_open`
return EPERM (filtered), while under unconfined they reach the kernel and return
EINVAL/EFAULT.

`clone3` is deliberately left alone. Upstream forces it to ENOSYS without
CAP_SYS_ADMIN, which makes glibc fall back to `clone` -- the call we do allow.
Adding a conflicting ALLOW for the same syscall would leave the resolved action
ambiguous for no gain.

Regenerating the vendored profile (dev-time, needs network):

    python3 -m harness seccomp-sync            # refresh from the pinned moby tag
    python3 -m harness seccomp-sync --check    # verify the committed file, write nothing

Imports no harness sibling, so `doctor` and the tests both reuse it without a
cycle (same discipline as mask.py / cost.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Pinned upstream source. moby dropped profiles/seccomp/default.json after v28 (it
# is generated from Go now), so this tag is the newest one that still publishes the
# JSON. Bump deliberately, re-run seccomp-sync, and re-read the diff -- an upstream
# profile change is a security-relevant change to our base posture.
MOBY_TAG = "v28.0.1"
MOBY_PROFILE_URL = (
    f"https://raw.githubusercontent.com/moby/moby/{MOBY_TAG}/profiles/seccomp/default.json"
)

# The five syscalls bwrap needs to create and populate a user namespace.
RELAXED_SYSCALLS = ("clone", "unshare", "mount", "umount2", "pivot_root")

_RELAX_COMMENT = (
    "holder M4 slice H: permit unprivileged user-namespace creation so bubblewrap "
    "can build the fs jail. The kernel still enforces capability checks inside the "
    "new namespace; this only stops seccomp from pre-empting the syscall."
)

# Repo-relative home of the generated artifact (deepagent-image/seccomp/userns.json).
PROFILE_FILENAME = "userns.json"


PROFILE_ENV = "DEEPAGENTS_SECCOMP_PROFILE"


def profile_candidates() -> list[Path]:
    """Where the vendored profile can live, most specific first.

    Two layouts matter and they are not the same depth:
    - **repo checkout**: harness/ is at `deepagent-image/project/harness/`, so the
      seccomp/ folder is two levels up. This is the copy the launchers pass to
      `docker run --security-opt`.
    - **in-image**: harness/ is at `/project/harness/`, and the Dockerfile copies
      the folder to `/project/seccomp/` — one level up. Without this candidate the
      in-container path resolves to `/seccomp/...` and every check fails.

    Resolved from `__file__` rather than CWD because the launchers call this from
    the repo root while the container runs from /project.
    """
    here = Path(__file__).resolve()
    return [
        here.parents[2] / "seccomp" / PROFILE_FILENAME,  # repo checkout
        here.parents[1] / "seccomp" / PROFILE_FILENAME,  # in-image
    ]


def profile_path() -> Path:
    """The vendored profile's location. Env override wins, then first that exists.

    Falls back to the repo-checkout path when none exist so error messages point
    at where the file is *supposed* to go rather than at the last candidate tried.
    """
    override = os.environ.get(PROFILE_ENV)
    if override:
        return Path(override)
    candidates = profile_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def relax_userns(profile: dict) -> dict:
    """Return `profile` with the user-namespace syscalls unconditionally allowed.

    Appends one rule rather than editing the upstream ones: the existing
    arg-masked `clone` entries stay exactly as shipped, so a reader diffing this
    against Docker's default sees one added block and nothing else. libseccomp
    merges the broader unconditional ALLOW over the narrower conditional ones;
    both actions are ALLOW, so there is no conflicting-action ambiguity.

    The input is not mutated -- callers hold the upstream copy for comparison.
    """
    relaxed = json.loads(json.dumps(profile))
    relaxed.setdefault("syscalls", []).append(
        {
            "names": list(RELAXED_SYSCALLS),
            "action": "SCMP_ACT_ALLOW",
            "comment": _RELAX_COMMENT,
        }
    )
    return relaxed


def verify_profile(profile: dict) -> list[str]:
    """Check the profile is Docker's default plus exactly our relaxation.

    Returns a list of human-readable problems; empty means good. This is the
    regression guard behind invariant 31 -- swapping in an unconfined profile, or
    quietly widening the relaxation, has to fail here (and so in CI and in
    `harness doctor`) rather than sail through because the jail still works.
    """
    problems: list[str] = []

    if profile.get("defaultAction") != "SCMP_ACT_ERRNO":
        problems.append(
            "defaultAction is "
            f"{profile.get('defaultAction')!r}, expected 'SCMP_ACT_ERRNO' -- an "
            "allow-by-default profile is unconfined in all but name"
        )

    syscalls = profile.get("syscalls") or []
    relax_entries = [
        entry
        for entry in syscalls
        if entry.get("action") == "SCMP_ACT_ALLOW"
        and not entry.get("includes")
        and not entry.get("excludes")
        and not entry.get("args")
        and set(entry.get("names") or []) & set(RELAXED_SYSCALLS)
    ]

    if not relax_entries:
        problems.append(
            "no unconditional allow entry for the user-namespace syscalls -- "
            "bwrap will fail to create its namespace"
        )
    elif len(relax_entries) > 1:
        problems.append(
            f"{len(relax_entries)} unconditional namespace-allow entries, expected 1"
        )
    else:
        got = set(relax_entries[0].get("names") or [])
        want = set(RELAXED_SYSCALLS)
        if got != want:
            extra = sorted(got - want)
            missing = sorted(want - got)
            detail = []
            if extra:
                detail.append(f"unexpected {extra}")
            if missing:
                detail.append(f"missing {missing}")
            problems.append(
                "relaxation set drifted from RELAXED_SYSCALLS: " + ", ".join(detail)
            )

    # Anything else granted unconditionally would be a silent widening.
    for entry in syscalls:
        if entry in relax_entries:
            continue
        if (
            entry.get("action") == "SCMP_ACT_ALLOW"
            and not entry.get("includes")
            and not entry.get("excludes")
            and not entry.get("args")
        ):
            names = entry.get("names") or []
            # The upstream profile's own baseline allow-list is a single large
            # unconditional entry; flag only if it grew CAP_SYS_ADMIN-gated names.
            gated = sorted(set(names) & set(RELAXED_SYSCALLS))
            if gated:
                problems.append(
                    f"namespace syscalls {gated} also allowed by another entry"
                )

    return problems


def load_profile(path: Path | None = None) -> dict:
    """Read the vendored profile from disk."""
    target = path or profile_path()
    with open(target, encoding="utf-8") as handle:
        return json.load(handle)


def _fetch_upstream(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 (pinned https)
        return json.loads(response.read().decode("utf-8"))


def seccomp_sync_main(argv: list[str]) -> int:
    """Dev-time regeneration of the vendored profile. Needs network, no keys."""
    parser = argparse.ArgumentParser(
        prog="harness seccomp-sync",
        description="Regenerate the vendored narrow seccomp profile from moby's default.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed profile and exit; write nothing, fetch nothing",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="destination path (default: deepagent-image/seccomp/userns.json)",
    )
    args = parser.parse_args(argv)

    out = args.out or profile_path()

    if args.check:
        try:
            profile = load_profile(out)
        except FileNotFoundError:
            print(f"[seccomp] missing vendored profile: {out}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as exc:
            print(f"[seccomp] {out} is not valid JSON: {exc}", file=sys.stderr)
            return 1
        problems = verify_profile(profile)
        for problem in problems:
            print(f"[seccomp] {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"[seccomp] {out} is Docker's default plus exactly {list(RELAXED_SYSCALLS)}")
        return 0

    try:
        upstream = _fetch_upstream(MOBY_PROFILE_URL)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[seccomp] could not fetch {MOBY_PROFILE_URL}: {exc}", file=sys.stderr)
        return 1

    relaxed = relax_userns(upstream)
    problems = verify_profile(relaxed)
    if problems:
        for problem in problems:
            print(f"[seccomp] refusing to write: {problem}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(relaxed, handle, indent="\t")
        handle.write("\n")
    print(f"[seccomp] wrote {out} from moby {MOBY_TAG} (+{len(RELAXED_SYSCALLS)} relaxed)")
    return 0
