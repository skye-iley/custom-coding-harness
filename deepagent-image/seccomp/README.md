# Narrow seccomp profile — `userns.json`

Generated artifact. **Do not hand-edit** — regenerate with
`python3 -m harness seccomp-sync` (see below).

## What this is

Docker's default seccomp profile, with exactly five syscalls relaxed:

```
clone   unshare   mount   umount2   pivot_root
```

Those five are what `bubblewrap` needs to create and populate a user namespace.
Without them `bwrap --unshare-all` fails inside the harness image with:

```
bwrap: No permissions to create new namespace, likely because the kernel does
       not allow non-privileged user namespaces.
```

which is the hard gate `docs/milestones/in-progress/milestone4.md` §17/PR6 puts
in front of slice H (the bwrap fs jail).

## ⚠️ This profile is only ONE of the two gates

**seccomp and the host's LSM are independent, and an operation must pass both.**
Fixing the syscall filter does nothing about AppArmor. On Ubuntu/Debian — most
Linux Docker hosts — Docker also applies a generated `docker-default` AppArmor
profile carrying a literal `deny mount,`, so with this profile correctly applied
bwrap still fails:

```
bwrap: Failed to make / slave: Permission denied
```

Note where that lands: **past** `unshare`, at the first mount. That is the
fingerprint distinguishing the two failures, and `jail.classify_bwrap_failure`
uses it so nothing misreports an AppArmor denial as a seccomp problem. AppArmor
denies by profile rather than by uid or capability, and confinement is **not**
shed by entering a user namespace — so no userns work, and not even a setuid
`bwrap`, escapes it from inside.

**That second gate is slice J, and it is built:** `../apparmor/deepagent-userns`
is `docker-default` with only its `deny mount,` narrowed, loaded into the host
kernel by `../scripts/install-apparmor-profile.sh` and selected automatically by
`run-docker`. See `../apparmor/README.md` — including its caveat that the rule
set has not yet been exercised on a real AppArmor host. The blunt fallback,
`DEEPAGENTS_JAIL_APPARMOR=unconfined`, still works everywhere at the cost of
dropping the **whole** profile rather than one rule. Docker Desktop/WSL2 needs
nothing (no LSM policy is loaded) — which is exactly why this gap survived to CI:
every slice-H measurement was taken there.

## Why not just `seccomp=unconfined`

Because the container is still the real trust boundary
(`docs/milestones/complete/mvp.md` §5). Running it unconfined would drop *every*
syscall filter in order to buy one inner boundary — a net-negative trade if the
inner jail ever fails open. This profile keeps the rest of Docker's filter
exactly as shipped.

The difference is observable at runtime:

| syscall | under `userns.json` | under `seccomp=unconfined` |
|---|---|---|
| `bpf` | `EPERM` (filtered) | `EINVAL` (reached the kernel) |
| `keyctl` | `EPERM` (filtered) | `EINVAL` |
| `perf_event_open` | `EPERM` (filtered) | `EFAULT` |

## What the relaxation does and does not grant

It grants **no privilege**. The kernel still enforces its capability model, so
`mount` from an unprivileged process outside a user namespace keeps failing with
`EPERM` exactly as before. What changes is that seccomp stops *pre-empting* the
syscall and lets the kernel decide.

The residual risk is real but bounded: it exposes the kernel's user-namespace
code to the container, historically a source of local privilege-escalation CVEs.
**That is why the jail is opt-in** (`DEEPAGENTS_JAIL`, off by default) rather
than the new default posture — enabling it is an operator's deliberate choice to
trade a little outer-boundary attack surface for a real inner boundary.

## The relaxation is container-wide — and what we do about it

`--security-opt` is a **`docker run` flag**. There is no mechanism to grant the
relaxed profile to the harness process alone, so switching the jail on hands the
**agent's shell tool** the same five syscalls. That is inherent to the design,
not an oversight.

The compensating layer is `harness/nsguard.py` (milestone4.md §11.5, invariant
36): a denylist on the shell tool's command string covering the common routes —
`unshare`/`nsenter`/`mount`/`chroot`/`bwrap`/container runtimes in command
position (basename-matched, wrapper- and `&&`-segment-aware), plus `CLONE_NEW*`,
`unshare(`, `setns(` and raw `syscall(272|155|308)` anywhere, which is the
interpreter-one-liner route. A hit is refused, printed to stderr regardless of
HITL, and recorded to `<state-dir>/denials.jsonl` (match and reason only — never
the command string). It defaults on exactly when the jail is on; `DEEPAGENTS_NS_GUARD`
overrides (`warn` = record without refusing, `0` = off).

**It is a tripwire, not containment.** A string denylist is phrasing-blind:
anything compiled from source, base64-decoded, or indirected through a variable
passes straight through. Do not read it as making the relaxation safe — the
boundary is still the container plus the jail's bind set. Its value is that the
casual attempt is refused and, more importantly, leaves *evidence*.

Two upstream rules explain why all five are needed:

- `clone` is allowed upstream only when the flags word does **not** intersect
  `0x7E020000` (the `CLONE_NEW*` namespace bits), unless the process holds
  `CAP_SYS_ADMIN`.
- `pivot_root` is **not in the upstream profile at all**, so
  `defaultAction: SCMP_ACT_ERRNO` blocks it.

`clone3` is deliberately left alone: upstream forces it to `ENOSYS` without
`CAP_SYS_ADMIN`, which makes glibc fall back to `clone` — the call we do allow.
Adding a conflicting `ALLOW` for the same syscall would leave the resolved action
ambiguous for no gain.

## How it is generated

`harness/seccomp.py` fetches moby's `profiles/seccomp/default.json` at a **pinned
tag** and appends one rule. It appends rather than edits, so a reader diffing
this file against Docker's default sees one added block and nothing else.

```bash
python3 -m harness seccomp-sync            # refresh from the pinned moby tag (needs network)
python3 -m harness seccomp-sync --check    # verify the committed file; writes nothing, fetches nothing
```

`--check` is the regression guard: it asserts `defaultAction` is still
`SCMP_ACT_ERRNO` and that the relaxation entry names *exactly* `RELAXED_SYSCALLS`.
Swapping in an unconfined profile, or quietly widening the relaxation, fails there
instead of sailing through because the jail still works.

In CI that guard runs as `tests/test_seccomp.py` (`test_committed_profile_is_narrow`
and `test_committed_profile_still_gates_the_dangerous_syscalls`), which assert the
same properties against the committed artifact from the stdlib-only host tier.
`seccomp-sync --check` stays the equivalent one-shot for local use, in an
environment that has the harness runtime deps installed — importing the `harness`
package pulls in `cli` and therefore `dotenv`/`langgraph`.

## Maintenance

The pinned tag is `MOBY_TAG` in `harness/seccomp.py`. moby stopped publishing
`default.json` after **v28** (it is generated from Go source now), so v28.0.1 is
the newest tag that still ships the JSON.

Bump it **deliberately** and read the resulting diff — an upstream profile change
is a security-relevant change to this image's base posture, not a routine
dependency bump.
