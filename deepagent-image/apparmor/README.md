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
becomes seven (plus a comment). `verify_profile` asserts exactly that property,
so it cannot silently become more.

## Why this exists

M4 slice H runs the agent's file and shell tools inside a `bubblewrap` mount
namespace (`DEEPAGENTS_JAIL=1`). Building that namespace requires `mount`
syscalls. **Two independent kernel gates stand in the way, and both must allow:**

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

Upstream's single `deny mount,` is replaced by exactly these:

| Rule | What needs it |
|---|---|
| `mount options=(rw, rslave) -> /,` | bwrap's first act after `unshare`: `mount(NULL, "/", NULL, MS_SLAVE\|MS_REC, NULL)`, so mount events in the jail do not propagate back to the host namespace. This is the operation that fails today. |
| `mount fstype=tmpfs,` | `--tmpfs /tmp`, `--dev` (which builds a tmpfs `/dev`), and the empty-directory overmounts that hide masked directories inside the jail. |
| `mount options=(rw, bind),` | every `--bind` — principally the workspace, mounted read-write so the agent's edits still land live. |
| `mount options=(rw, rbind),` | the recursive form, used for the read-only system binds (`/usr`, `/bin`, `/lib`, `/opt`, …). |
| `mount options=(ro, remount, bind),` | the second half of `--ro-bind`: Linux cannot create a read-only bind in one call, so bwrap binds then remounts read-only. |
| `mount fstype=proc -> /proc/,` | `--proc /proc`. bwrap mounts a **fresh** procfs inside the jail rather than inheriting the container's. |
| `pivot_root,` | how bwrap swaps the assembled tree in as `/`. Denied by the stock profile because it is a mount-family operation. |

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
trade `docs/milestones/in-progress/milestone4.md` §16 fork 7 already rejected in
its seccomp form (`seccomp=unconfined`). The honest accounting:

- **Largely, but not wholly, redundant.** Docker independently applies OCI
  `maskedPaths`/`readonlyPaths` over most of the same `/proc` targets, and the
  kernel checks a write to `/proc/sysrq-trigger` against `CAP_SYS_ADMIN` **in the
  initial user namespace** — which a nested-userns process does not hold.
- **Not zero, for one specific reason.** bwrap mounts a *fresh* procfs inside the
  jail, which does **not** inherit Docker's masks. Inside the jail those
  init-userns capability checks are the only thing left. That is a thinner
  backstop than the layered one, and it is the argument for this profile.
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
- **Rootless Docker and Podman are untested** — profile application and naming
  differ.
- Docker Desktop / WSL2 / macOS need nothing: their container VM loads no
  AppArmor policy at all. That is also why this gap survived to CI — every
  slice-H measurement was taken there.

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
