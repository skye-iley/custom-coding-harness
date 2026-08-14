# Narrowed AppArmor profile — `deepagent-userns`

Generated artifact. **Do not hand-edit** — regenerate with
`python3 -m harness apparmor-sync`. A hand-edit is detected: the profile carries
a `holder-profile-sha256:` header that `apparmor.verify_profile` recomputes.

Two files live here:

| File | What it is |
|---|---|
| `docker-default.rendered` | moby's `docker-default` profile, rendered **unmodified**. The diff base. |
| `deepagent-userns` | the same profile with **only** its `deny mount,` line narrowed. What you load. |

`diff docker-default.rendered deepagent-userns` is the whole review: one line
becomes the seven measured rules (plus a comment). `verify_profile` asserts
exactly that property, so it cannot silently become more.

## Why this exists

M4 slice H runs the agent's file and shell tools inside a `bubblewrap` mount
namespace (`DEEPAGENTS_JAIL=1`). Building that namespace requires `mount`
syscalls. **Two independent policy gates stand in the way, and both must allow**
(a third, the kernel's own procfs restriction, is neither seccomp nor an LSM and
is covered at the end of this file):

1. **seccomp** — Docker's default syscall filter blocks unprivileged
   user-namespace creation. Solved by `../seccomp/userns.json` (slice H).
2. **AppArmor** — an LSM policy Docker applies as `docker-default`, containing a
   literal `deny mount,`. **Not** affected by any seccomp change.

With only #1 fixed, bwrap creates its namespace and then dies at its first
mount:

```
bwrap: Failed to make / slave: Permission denied
```

Note where that lands: *past* `unshare`. That is the fingerprint
`jail.classify_bwrap_failure` uses to tell the two failures apart, so nothing
sends an operator to re-check a seccomp profile that is already correct.

**Nothing works around it from inside the container.** AppArmor denies by
*profile*, not by uid or capability. A process in a user namespace holds
`CAP_SYS_ADMIN` over that namespace — which is what makes unprivileged bwrap
work at all — but stays confined by `docker-default`. Root cannot override an
LSM denial, and neither can a setuid-root `bwrap` (that would fix the half that
is not failing).

## What is relaxed, and why each rule

Upstream's single `deny mount,` is replaced by exactly these. **Every rule here is backed by a
kernel denial that demanded it** — measured 2026-08-14 on Ubuntu (kernel `7.0.0-29-generic`, Docker
29.7.2). See `docs/milestones/complete/milestone4.1.md` §13.1a for the round-by-round log.

| Rule | What needs it |
|---|---|
| `mount options=(rw, silent, rslave) -> /,` | bwrap's first act after `unshare`: `mount(NULL, "/", NULL, MS_SILENT\|MS_SLAVE\|MS_REC, NULL)`, so mount events in the jail do not propagate back to the host namespace. `silent` is **not optional**: AppArmor's `options=` is an exact flag-set match, and omitting `MS_SILENT` denies with `info="failed flags match"`. |
| `mount fstype=tmpfs,` | `--tmpfs /tmp`, `--dev` (which builds a tmpfs `/dev`), and the empty-directory overmounts that hide masked directories inside the jail. |
| `mount options=(rw, bind),` | every `--bind` — principally the workspace, mounted read-write so the agent's edits still land live. Measured to need **no** `silent`, unlike the propagation mounts. |
| `mount options=(rw, rbind),` | the recursive form, used for the read-only system binds (`/usr`, `/bin`, `/lib`, `/opt`, …). |
| `mount options in (ro, silent, remount, bind, nosuid, nodev, noexec, noatime, relatime, nodiratime, strictatime),` | the second half of `--ro-bind`: Linux cannot create a read-only bind in one call, so bwrap binds then remounts read-only — re-supplying the **source mount's** existing flags. Those depend on the host's storage driver and on each bind source (`nosuid, nodev, relatime` on the measured host), so `=` would need one rule per combination and cannot converge. `in` (subset match) is the correct operator. **`rw` is deliberately absent** so this stays the read-only remount rule rather than becoming a general bind grant. |
| `pivot_root,` | how bwrap swaps the assembled tree in as `/`. Denied by the stock profile because it is a mount-family operation. |
| `mount options=(rw, silent, rprivate) -> /oldroot/,` | bwrap makes the old root rprivate before detaching it, so unmount events do not reach the parent namespace. This runs **after** all setup ops, which is why it was the last denial to surface — and why it was missing from the rule set originally derived by reading bwrap's syscall sequence. |

**One rule was removed by measurement, and its absence is a result.** A
`mount fstype=proc -> /proc/,` rule shipped in the first measured set on the theory
that bwrap's `--proc` needed it. Fork J6 deleted it and re-measured (2026-08-14,
same host): the jail still passes 5/5 and `dmesg` logs no proc denial, so the rule
was authorizing nothing — bwrap mounts procfs at `newroot/proc` pre-pivot, which the
bind rules above already cover. The mount *is* governed, just not by AppArmor: the
kernel's own `mount_too_revealing()` check is what gates it, which is a different
gate entirely (see "This profile is necessary but not sufficient" below). Do not
re-add the rule without a denial that demands it.

**Everything else in `docker-default` survives byte-for-byte** — all nine `deny`
rules (`/proc/sysrq-trigger`, `/proc/kcore`, the `/proc/sys` write denials,
`/sys/firmware`, `/sys/kernel/security`, `/sys/devices/virtual/powercap`, the
`/sys/fs/cgroup` shape), the signal peers, and the `ptrace … peer=` restriction.
`verify_profile` checks the critical ones by name so a quiet deletion cannot
hide inside a large regeneration diff.

### Does this grant privilege?

No more than being able to *ask*. AppArmor is a mediation layer above the
kernel's own checks: permitting a mount rule stops AppArmor from pre-empting the
call, it does not hand the process capabilities. Outside a user namespace, an
unprivileged `mount` still fails with `EPERM` exactly as before. Inside one,
bwrap is doing what unprivileged bwrap is designed to do.

The residual risk is the same one `../seccomp/README.md` names, and it is the
reason the jail is opt-in: a container that can create user namespaces has
reachable kernel userns code, historically a source of local privilege-escalation
CVEs. This profile does not change that trade — it only stops the LSM from
blocking the jail on hosts where an LSM is loaded.

## Why not `apparmor=unconfined`

Because that drops the **whole** profile to fix one line — categorically the
trade `docs/milestones/complete/milestone4.md` §16 fork 7 already rejected in
its seccomp form (`seccomp=unconfined`). The honest accounting:

- **Largely, but not wholly, redundant.** Docker independently applies OCI
  `maskedPaths`/`readonlyPaths` over most of the same `/proc` targets, and the
  kernel checks a write to `/proc/sysrq-trigger` against `CAP_SYS_ADMIN` **in the
  initial user namespace** — which a nested-userns process does not hold.
- **Not zero, for one specific reason.** bwrap mounts a *fresh* procfs inside the
  jail, which does **not** inherit Docker's masks. Inside the jail those
  init-userns capability checks are the only thing left. That is a thinner
  backstop than the layered one, and it is the argument for this profile.
  *(Fork J5 — now built — leans on this same fact in the opposite direction: if the
  jail's procfs never had Docker's masks, then `systempaths=unconfined` takes away
  something the jailed process was not getting anyway. Both readings are correct;
  note that the fact is doing double duty, and that J5's residual — the pre-re-exec
  window and anything outside the jail — is not covered by it. Since the launchers
  now pass `systempaths=unconfined` under the jail by default, that residual is
  live on every jailed run, not hypothetical.)*
- **Categorically wider than what the operator signed up for.** Fork 7 framed the
  trade as *five relaxed syscalls*. `unconfined` makes it *five syscalls and an
  entire LSM off*.

So `DEEPAGENTS_JAIL_APPARMOR=unconfined` stays reachable — it is the escape hatch
when you cannot load a profile on the daemon's host — but it is never a launcher
default, and both `run-docker` and `harness doctor` say what it gave up.

## Install / uninstall

Unlike a seccomp profile, this **cannot** be passed to `docker run` as a file. It
must be compiled into the host kernel first:

```bash
sudo deepagent-image/scripts/install-apparmor-profile.sh              # load (enforce)
     deepagent-image/scripts/install-apparmor-profile.sh --status     # loaded? which sha?
sudo deepagent-image/scripts/install-apparmor-profile.sh --uninstall  # remove
```

Then `run-docker` selects it automatically when `DEEPAGENTS_JAIL=1` on a Linux
engine, and **probes that the daemon will accept it before launching** — if the
profile is not loaded, the launcher aborts with the install command rather than
starting a container whose jail is going to die inside.

**Load it on the machine running `dockerd`.** A remote daemon, a Colima/Lima VM,
or a WSL distro each has its own kernel; loading it on the CLI host does nothing
for those. This is why the launcher's check asks the *daemon* (a throwaway
`docker run`) instead of reading `/sys/kernel/security/apparmor/profiles`
locally, which would need root and would lie in exactly those setups.

### Stale loads

The kernel reports a loaded profile's **name**, never its content, and there is
no route from inside a container to the loaded rules. So a host can carry an old
`deepagent-userns` while this repo's copy has moved on, and nothing in-container
can detect it. Mitigation, not a fix: `--status` prints both the recorded and
on-disk sha, and **you re-run the installer after every `apparmor-sync`**.

### Complain mode is not enforcement

`apparmor_parser -C` loads a profile in complain mode: violations are logged and
**allowed**. bwrap would then run and the LSM would be enforcing nothing. Load it
with `-r` (what the installer does). `harness doctor` reports complain mode as an
error, because it is the failure that looks most like success.

## Scope

- **AppArmor only.** **SELinux hosts (RHEL/Fedora/CentOS) are a third
  environment and are untested** — different mechanism (type enforcement,
  `--security-opt label=`), not addressed here. Do not read this profile's
  existence as coverage for them.

  Untested, but **not silent** (M4.1 fork J4). The harness detects an SELinux
  context (`jail.selinux_confinement`), refuses to report it as an AppArmor
  profile — the two share `/proc/self/attr/current`, and reading a context as a
  profile *name* used to make `doctor` demand an AppArmor profile on a host with
  no AppArmor — routes a mount denial there to a known-gap message instead of
  AppArmor instructions, and has `doctor` emit a **warning**: the jail may work
  here or may not, and nobody has measured it. `--security-opt label=disable` is
  the usual escape hatch, named and marked **unverified**; it drops SELinux
  labelling for the whole container, which is wider than the one rule this
  profile narrows. Measuring it (`ausearch -m AVC` where this doc says
  `dmesg | grep apparmor="DENIED"`) is what would close the gap.
- **Rootless Docker and Podman are untested** — profile application and naming
  differ.
- Docker Desktop / WSL2 / macOS need nothing: their container VM loads no
  AppArmor policy at all. That is also why this gap survived to CI — every
  slice-H measurement was taken there.

## This profile is necessary but not sufficient

Loading `deepagent-userns` closes the AppArmor gate. It does **not** make
`DEEPAGENTS_JAIL=1` start on a stock Ubuntu/Debian host, because there is a
**third** gate, independent of both seccomp and AppArmor:

```
bwrap: Can't mount proc on /newroot/proc: Operation not permitted
```

`EPERM`, and `dmesg` shows no denial — this is the kernel's `mount_too_revealing()`
check, not an LSM. Mounting a fresh procfs from a non-initial user namespace is
refused while the procfs already visible in the mount namespace is covered by
submounts, and Docker's `maskedPaths` / `readonlyPaths` are exactly that (13
mounts over `/proc` on the measured host).

The fix is `--security-opt systempaths=unconfined` on the `docker run`, and fork
**J5** now wires it: `run-docker.{sh,ps1}` and `smoke.{sh,ps1}` pass it from the
same `DEEPAGENTS_JAIL=1` block that passes seccomp and this profile, and say what
they gave up. `DEEPAGENTS_JAIL_SYSTEMPATHS=default` keeps Docker's masks — which
is how the **LSM-only control** is reproduced (bwrap must then die at `--proc`
with zero `deepagent-userns` denials), no script edit required. `harness doctor`
reports the container's covering `/proc` mounts, and `jail.classify_bwrap_failure`
names this failure `procfs` rather than blaming either profile.

✅ **Measured 2026-08-14** on the same Ubuntu VM, with the launcher supplying the
flag and nothing added by hand: `JAIL_CHECK=1 ./scripts/smoke.sh` passes 5/5. The
control (`DEEPAGENTS_JAIL_SYSTEMPATHS=default`) fails at `--proc` with the
`procfs` reason and **zero** `deepagent-userns` denials in `dmesg` — which is what
establishes that the flag is the thing that moved, rather than something else
having changed. `DEEPAGENTS_JAIL=1` now starts end-to-end on a stock
Ubuntu/Debian host.

Do **not** "fix" this by binding the container's `/proc` and dropping
`--unshare-pid`. The harness and the agent's shell run as the same uid, so
`/proc/<harness-pid>/environ` hands the shell every provider API key —
`_agent_shell_env`'s allowlist governs what the shell *inherits*, not what it can
read out of another process. The nested shell jail's `--unshare-pid` + fresh
`--proc` is what closes that, and it can only mount a fresh procfs if the outer
jail's procfs is itself uncovered. See `milestone4.1.md` §13.7.

## Regenerating

```bash
python3 -m harness apparmor-sync            # refetch moby's template, re-render, rewrite both files
python3 -m harness apparmor-sync --check    # verify the committed files (offline, no kernel needed)
```

`MOBY_TAG` is pinned in `harness/apparmor.py` to the same tag
`harness/seccomp.py` uses. Bump both together, re-run both syncs, and **read both
diffs**: an upstream profile change is a security-relevant change to our base
posture.

Upstream ships this as a Go `text/template`, not a finished file, so the sync
renders it with a deliberately restricted evaluator that **raises on any
construct it does not recognize**. If a future moby release adds a directive, the
sync fails loudly instead of quietly emitting a profile whose meaning nobody
checked.
