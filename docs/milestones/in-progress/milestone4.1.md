# Milestone 4.1 — LSM Parity (Slice J: vendored AppArmor profile)

> **Status:** 🚧 In-progress — **built on `feat/milestone_4`, one step short of done.** Everything in
> §15's PR plan has landed *except* step 4, the live-host measurement (§13.1): the dev machine is
> Docker Desktop/WSL2, which loads no AppArmor policy and therefore **structurally cannot** verify
> this slice. Per §1 and §4, the milestone does not close until the profile is exercised on a real
> AppArmor-confined host and the measurement records the LSM it ran under. CI's
> `apparmor-load-probe` job (§10) is the vehicle for that measurement and is deliberately
> non-gating until it reports. **Until then the mount rule set in §6 remains derived, not
> confirmed** — the doc, the module docstring, and `apparmor/README.md` all say so. Follow-up to
> `docs/milestones/in-progress/milestone4.md` — it builds **slice J** (§0 table, §4, §11.6, §16
> fork 10, invariant 38), the one M4 slice left unbuilt after A–H landed on `feat/milestone_4`.
>
> **Why it is its own milestone doc rather than a §11.6 expansion:** J is gated on a
> *measurement M4 never took* (a real AppArmor-confined host), it introduces **host state** the
> harness has never needed before (a kernel-loaded LSM profile, installed as root, outside Docker's
> reach), and it ships after M4's own PR6. Sizing it as a slice-shaped patch is what produced the
> original miss — H merged calling itself verified on the strength of a Docker Desktop measurement
> that structurally could not see this gate.
>
> **Read first:** `milestone4.md` §11.4 (the shipped jail), §11.6 (the problem statement this doc
> turns into a build), §16 fork 10 (the pinned decision), `milestone4_invariants.md` 37–38, and
> `deepagent-image/seccomp/README.md` (the syscall-filter twin whose shape J mirrors).
>
> This document is implementation-ready. §1–§4 are the *what/why*; §5 onward is the *how*: module
> layout (§5), the profile contract (§6), the generator algorithm (§7), enforcement wiring (§8),
> distribution (§9), CI (§10), knobs (§11), the trade (§12), gotchas (§13), open forks (§14), the PR
> plan (§15), tests (§16), and the invariants it adds (§17).

---

## 0. One-paragraph summary

M4 slice H shipped a bubblewrap fs jail behind `DEEPAGENTS_JAIL=1`, gated by a vendored **narrow
seccomp** profile. seccomp is only **one of two** independent kernel gates. On any host running
AppArmor — Ubuntu/Debian Docker, i.e. most Linux container hosts, and GitHub-hosted runners — Docker
also applies its generated `docker-default` profile, whose literal `deny mount,` blocks bwrap at its
first mount, *after* `unshare` has already succeeded. No seccomp change touches this, and AppArmor
confinement is not shed by entering a user namespace, so nothing works around it from inside. M4.1
vendors `docker-default` with **only the `mount` rule narrowed** — the same shape `seccomp-sync`
already uses for the syscall filter — plus the install/preflight/doctor wiring that makes a
kernel-loaded profile a checkable property instead of an operator convention.

---

## 1. Goal & Definition of Done

Make `DEEPAGENTS_JAIL=1` work on a stock AppArmor-confined Linux Docker host **without dropping the
LSM**, and make the profile's narrowness a CI-checked regression guard rather than a claim.

**Done when:**

- A vendored `deepagent-image/apparmor/deepagent-userns` exists: moby's `docker-default` template
  rendered, with its single `deny mount,` replaced by exactly the mount rules bwrap needs (§6) and
  **every other rule byte-identical to upstream**.
- `python3 -m harness apparmor-sync --check` verifies the committed artifact **offline**, and fails
  on a permissive `mount,` catch-all, a widened rule set, a missing rule, a removed `deny` line, or a
  renamed profile. It runs in the host test tier, so a weakened profile fails CI (invariant 38).
- `scripts/install-apparmor-profile.sh` loads the profile with `apparmor_parser`, is idempotent, has
  a documented uninstall, and refuses cleanly on a host with no AppArmor.
- With the profile loaded, `run-docker` selects it **by default** on Linux; with it *not* loaded, the
  launcher **aborts before `docker run`** with the exact install command — never falls back to
  `unconfined`, never launches unjailed while the operator believes otherwise.
- `harness doctor`, with the jail on: our profile in force → pass; `docker-default` (or any other
  profile) → error naming the install script; complain-mode → error; committed artifact drifted →
  error; jail off → skipped entirely.
- **Measured on a real AppArmor-confined host** (§13.1): `bwrap --unshare-all` runs, a masked path
  reads 0 bytes inside the jail with the docker mask off, the workspace stays writable, `/project` is
  read-only — i.e. `milestone4.md` §11.4's gate table reproduced under `deepagent-userns`, with the
  LSM named in the record.
- `milestone4.md` §1/§11.6/§0, `milestone4_invariants.md` 37–38, `deepagent-image/CLAUDE.md`, and
  `ENV_VARS.md` no longer describe the jail's reach as "no-LSM hosts plus `unconfined` opters."

**Explicitly NOT done-when:** CI running the jail gate as a hard red/green. That depends on whether a
GitHub-hosted runner will load a profile at all, which is **unmeasured** (§10, fork J2). Pinning the
gate before measuring it is the exact error this milestone exists to correct.

## 2. Why this, and why now

1. **H is unusable where it matters most.** The jail's whole value proposition is a real allow-list
   boundary. On Ubuntu/Debian Docker it does not start. Today's only remedy —
   `DEEPAGENTS_JAIL_APPARMOR=unconfined` — buys the inner boundary by dropping an entire outer LSM,
   which is *categorically* the trade `milestone4.md` §16 fork 7 already rejected in its seccomp form
   (`seccomp=unconfined`). Shipping that as the practical default would quietly invert a pinned
   decision.
2. **The diagnostic half is already built and is load-bearing.** `jail.classify_bwrap_failure`,
   `jail.apparmor_confinement`, `jail.apparmor_hint`, doctor's LSM block, and the smoke gate's
   skip-on-LSM-denial all exist and correctly *name* the problem (invariant 37). M4.1 is the other
   half — the fix the diagnostics point at. Every message they print currently ends at a slice that
   does not exist.
3. **It is small and bounded.** One new stdlib module mirroring an existing one, one host script, two
   launcher edits, one doctor branch, one test file. The schedule risk is the live-host iteration
   (§13.1), not the code.

## 3. Scope

**In scope:** the generator + offline verifier (`harness/apparmor.py`), the vendored profile and its
README, the host install/uninstall script, launcher default + preflight probe, the doctor branch, CI's
host-tier check, tests, and the doc corrections.

**Out of scope — and each must stay out loudly, not silently:**

- **SELinux (RHEL/Fedora/CentOS).** A third LSM with a different mechanism (`container_t` type
  enforcement, `--security-opt label=`). Untested, unaddressed. J's merge must not imply it works;
  `apparmor/README.md` and `CLAUDE.md` say so explicitly.
- **Rootless Docker / Podman.** Different profile application and naming. Untested.
- **Making the jail on by default.** `DEEPAGENTS_JAIL` stays `0` — fork 7's userns-attack-surface
  trade is unchanged by this milestone. J changes *where the jail can run*, not *whether it is opted
  into*.
- **Dropping the seccomp relaxation.** A setuid-root `bwrap` would let the jail build with real
  privilege and skip the syscall relaxation — but AppArmor's `deny mount,` applies regardless of uid,
  so it fixes the half that is not failing (`milestone4.md` §11.6). Named here only so nobody
  re-derives it as a shortcut.

## 4. Background — the two-gate problem, precisely

seccomp and the LSM are **independent gates; an operation must pass both.**

From moby `v28.0.1/profiles/apparmor/template.go` — the same tag `harness/seccomp.py` already pins —
the generated `docker-default` profile carries:

```
  network,
  capability,
  file,
  umount,
  deny mount,                            # <-- the one that blocks the jail
  deny @{PROC}/sysrq-trigger rwklx,
  deny @{PROC}/kcore rwklx,
  deny /sys/firmware/** rwklx,
  ptrace (trace,read,tracedby,readby) peer=<profile>,
```

`umount` is permitted; `mount` is denied outright. bwrap's second operation after `unshare` is
`mount(NULL, "/", NULL, MS_SLAVE|MS_REC, NULL)`, so the observed failure is:

```
bwrap: Failed to make / slave: Permission denied
```

**The fingerprint** (already implemented, invariant 37): an LSM denial gets *past* `unshare` and fails
at the first mount; a seccomp/userns refusal fails *at* `unshare` with `No permissions to create new
namespace`. Misreading the first as the second sends an operator to re-check a seccomp profile that
is already correct.

**Why nothing escapes it from inside:** AppArmor denies by **profile**, not by uid or capability. A
process in a user namespace holds `CAP_SYS_ADMIN` over that namespace — which is what makes
unprivileged bwrap work at all — but remains confined by `docker-default`. Root cannot override an LSM
denial either.

**Why it survived to CI:** every slice-H measurement was taken on Docker Desktop/WSL2, whose LinuxKit
VM loads **no AppArmor policy**. With zero LSM confinement, seccomp was the only gate, so the seccomp
fix was sufficient *there*. The dev host is the unusual environment; an Ubuntu server and the CI
runner are the normal one. Standing rule from `milestone4.md` §11.4: **any boundary measurement must
name the LSM it ran under.**

## 5. Module & file layout

All new Python is stdlib-only in the **harness venv** stack, so it joins the host test tier
(two-stack rule).

**New**

- `deepagent-image/project/harness/apparmor.py` — generator + offline verifier. **Imports no harness
  sibling** (same acyclic discipline as `seccomp.py` / `mask.py`), so `doctor` and the tests both
  reuse it without a cycle.
- `deepagent-image/apparmor/deepagent-userns` — the vendored, committed, diffable artifact.
- `deepagent-image/apparmor/README.md` — counterpart to `seccomp/README.md`: what each mount rule is
  for, the does-not-grant-privilege argument per rule, residual risk, the install/uninstall flow, and
  the SELinux/rootless exclusions.
- `deepagent-image/scripts/install-apparmor-profile.sh` — host-side, root, Linux-only. **No `.ps1`
  twin** (same exception class as `jail-check.py`): AppArmor does not exist on the Windows host path.
  Do **not** add it to `check-parity.sh`'s `pairs` array.
- `deepagent-image/project/tests/test_apparmor.py` — the invariant-38 regression guard.
- `deepagent-image/project/tests/fixtures/apparmor_template_v28.0.1.go` — a pinned copy of upstream's
  `template.go` so the extractor is tested offline.

**Touched**

- `harness/cli.py` — `dispatch()`: add `apparmor-sync` beside `seccomp-sync`.
- `harness/doctor.py` — the LSM block at `doctor.py:228-255` (§8.3).
- `deepagent-image/Dockerfile` — copy `apparmor/` into the image the way `seccomp/` is copied, so
  in-container `doctor` can verify the shipped artifact.
- `scripts/run-docker.sh` + `scripts/run-docker.ps1` — profile default + preflight probe (§8.1),
  kept in sync.
- `scripts/smoke.sh` — same selection logic for the `JAIL_CHECK=1` path.
- `scripts/check-parity.sh` + `.ps1` — add a semantic marker for the new preflight (§8.2).
- `.github/workflows/ci.yml` — host-tier test list (§10).
- `deepagent-image/seccomp/README.md` — its "until slice J" paragraph becomes "see `apparmor/`".
- Docs per §1's last bullet.

## 6. The profile contract

```python
PROFILE_NAME = "deepagent-userns"
PROFILE_FILENAME = "deepagent-userns"          # no extension — apparmor convention
PROFILE_ENV = "DEEPAGENTS_APPARMOR_PROFILE"    # path override, mirrors seccomp.PROFILE_ENV

RELAXED_MOUNT_RULES = (
    "mount options=(rw, rslave) -> /,",        # the MS_SLAVE remount that fails today
    "mount fstype=tmpfs,",                     # --tmpfs /tmp, and dir-type mask overmounts
    "mount options=(rw, bind),",               # --bind / --ro-bind
    "mount options=(rw, rbind),",
    "mount options=(ro, remount, bind),",      # the read-only half of --ro-bind
    "mount fstype=proc -> /proc/,",            # --proc /proc
    "pivot_root,",
)
```

**This rule set is derived from bwrap's syscall sequence, not yet measured.** §13.1 is the process for
closing that; any rule added there must arrive with a per-rule justification in
`apparmor/README.md`, because each one widens the profile.

`profile_path()` mirrors `seccomp.profile_path()` exactly: `PROFILE_ENV` override wins, then the first
existing of the **repo-checkout** candidate (`__file__.parents[2]/"apparmor"/…`, the copy launchers
reference) and the **in-image** candidate (`parents[1]/"apparmor"/…`), falling back to the
repo-checkout path so an error message points at where the file belongs rather than the last candidate
tried.

## 7. Generator algorithm (`apparmor-sync`)

The one structural difference from `seccomp.py`, and the bulk of the work: **moby ships seccomp as
finished JSON but AppArmor as a Go `text/template`.** There is no rendered `docker-default` to
download; it is generated per-container by the daemon.

```
python3 -m harness apparmor-sync            # regenerate from the pinned tag (network, dev-time)
python3 -m harness apparmor-sync --check    # verify the committed artifact, no network, no write
python3 -m harness apparmor-sync --out PATH # destination override
```

`MOBY_TAG = "v28.0.1"` — duplicated as a module constant rather than imported from `seccomp` (acyclic
rule). Bump deliberately, re-run both syncs, re-read both diffs: an upstream profile change is a
security-relevant change to the base posture.

**Sync pipeline:**

1. **Fetch** `https://raw.githubusercontent.com/moby/moby/{MOBY_TAG}/profiles/apparmor/template.go`
   (urllib, 30s timeout, pinned https).
2. **Extract** the backtick-quoted base-template constant (currently `baseTemplate`). Pin the
   extraction with a test asserting the extracted text contains both `deny mount,` and `{{.Name}}`. If
   moby restructures the file, sync **fails loudly** rather than writing a half-profile.
3. **Render** with a *restricted* stdlib evaluator that supports **only** the directives this template
   actually uses, and **raises on any other construct** — so an upstream template that grows a new
   directive fails rather than renders something subtly wrong:

   | Directive | Substitution |
   |---|---|
   | `{{.Name}}` | `PROFILE_NAME` |
   | `{{range $v := .Imports}}…{{$v}}…{{end}}` | `#include <tunables/global>` |
   | `{{range $v := .InnerImports}}…{{$v}}…{{end}}` | `#include <abstractions/base>` |
   | `{{.DaemonProfile}}` | `unconfined` (what an ordinary distro dockerd reports as its own confinement) |

   > **Corrected against the real file.** An earlier revision of this section assumed
   > `{{if ge .Version N}}…{{end}}` conditionals and a `PROFILE_APPARMOR_VERSION` pin. moby
   > `v28.0.1`'s `template.go` has **no version conditionals** — it uses `range` over the two import
   > lists plus a `.DaemonProfile` field. The renderer implements what upstream actually contains;
   > the version pin is dropped. What survives from the original intent is the *fail-loud* property:
   > sync asserts at least one `range` was evaluated, so an upstream change that stops emitting
   > includes cannot silently drop them.
4. **`relax_mount(rendered) -> str`** — replace the single `deny mount,` line (indentation preserved)
   with `RELAXED_MOUNT_RULES`, each at the same indent. Every other line passes through
   byte-identical. Raise if the input contains zero or more than one `deny mount,` line: both mean the
   upstream shape moved and a blind edit would be wrong.
5. **`verify_profile(text) -> list[str]`** on the result before writing — refuse to write a profile
   that would fail its own check (same guard `seccomp_sync_main` uses).
6. **Write** `deepagent-image/apparmor/deepagent-userns` with LF newlines and a trailing newline, plus
   a leading `# GENERATED by 'python3 -m harness apparmor-sync' from moby <TAG> — do not hand-edit`
   comment and a `# holder-profile-sha256: <hash-of-rules>` line (§9 uses it for stale-load
   diagnostics; `verify_profile` recomputes and checks it).

**Two artifacts, not one.** Sync writes both `apparmor/docker-default.rendered` (upstream, rendered,
**unmodified**) and `apparmor/deepagent-userns` (upstream + our relaxation). This replaces the
originally-specified "pin a `BASELINE_RULES` tuple into the module at sync time", which would have
meant self-modifying source. Committing the baseline as a file gives a strictly stronger and simpler
offline check — *the shipped profile is exactly `relax_mount(baseline)`* — and makes the review
literally `diff` the two files: one line becomes seven.

**`verify_profile` must be offline and structural** — it is what runs in `doctor`, in CI, and in the
host test tier where there is neither network nor an AppArmor kernel. Checks:

- profile name is `PROFILE_NAME`;
- **`profile == relax_mount(vendored baseline)`** — anything differing outside the mount rule fails;
- the mount rule set is **exactly** `RELAXED_MOUNT_RULES` — no extras, none missing, none reordered;
- **no bare `mount,`** catch-all, and `deny mount,` is gone;
- no critical `deny` line removed — `deny @{PROC}/sysrq-trigger rwklx`, `deny @{PROC}/kcore rwklx`,
  `deny /sys/firmware/** rwklx`, `deny /sys/kernel/security/** rwklx`,
  `deny /sys/devices/virtual/powercap/** rwklx`;
- the recorded `# holder-profile-sha256:` matches the body — "do not hand-edit" enforced, not requested.

`verify_baseline` separately checks the diff base is really stock docker-default, so a weakened
baseline cannot be used to launder a widened profile. A test also asserts the committed baseline is
byte-reproducible from the pinned template fixture.

Returns a list of human-readable problems; empty means good. Same contract as
`seccomp.verify_profile`, so `doctor` treats both identically.

**Header vs. body gotcha (found in build):** `#include <tunables/global>` starts with `#` but is
AppArmor's include directive and the profile's **first functional line**, not a comment. The
header/body split must exclude it, or it drops out of both the hash and the baseline comparison.

## 8. Enforcement wiring

### 8.1 Launchers — `run-docker.{sh,ps1}` (and `smoke.sh`)

Today: jail on + `DEEPAGENTS_JAIL_APPARMOR` unset → pass nothing → `docker-default` applies → the
container starts and **bwrap fails inside**, with a good diagnostic but a wasted launch. J moves the
decision before `docker run`.

New selection logic, when `DEEPAGENTS_JAIL` is on:

| `DEEPAGENTS_JAIL_APPARMOR` | Behaviour |
|---|---|
| unset, **Linux engine** | default to `deepagent-userns`; probe it (below); abort with the install command if not loaded |
| unset, non-Linux engine (Docker Desktop/WSL2, macOS) | pass nothing, announce nothing — no LSM in force |
| `unconfined` | unchanged: `--security-opt apparmor=unconfined` + the existing "what you gave up" announcement |
| any other value | unchanged: passed through as a host-loaded profile name, still probed |

**The probe** — ask the daemon, not the filesystem, and **in this order**:

```bash
# 1. What confines an ordinary container here?  Empty / unconfined / kernel ⇒ no LSM ⇒ pass nothing.
docker run --rm "$IMAGE" sh -c 'cat /proc/self/attr/apparmor/current 2>/dev/null \
                              || cat /proc/self/attr/current 2>/dev/null || true'
# 2. Only if an LSM IS in force: will the daemon accept our profile?
docker run --rm --security-opt "apparmor=$profile" "$IMAGE" true
```

Asking the daemon is the host-agnostic part: reading `/sys/kernel/security/apparmor/profiles` would
(a) need root and (b) **lie for remote or VM-backed daemons**, because the profile must be loaded on
the machine running `dockerd`, not the machine running the CLI.

**The order is not a performance choice — it is a correctness one, and it was measured during the
build.** A daemon with no AppArmor support *accepts* `--security-opt apparmor=<anything>` and silently
ignores it (confirmed on Docker Desktop/WSL2: the probe for a profile loaded on **no** machine
returned exit 0). Probing first therefore "succeeds" against a profile that does not exist and makes
the launcher announce a boundary that is not there — the exact class of false assurance this milestone
exists to eliminate. So: establish that an LSM is in force, *then* ask for the profile.

On probe failure, abort **fail-closed** with both remedies, in this order:

```
[jail] FATAL: DEEPAGENTS_JAIL is on and this host is AppArmor-confined, but the
       'deepagent-userns' profile is not loaded on the Docker daemon's host.
       Load it:  sudo deepagent-image/scripts/install-apparmor-profile.sh
       Or accept the wider trade:  DEEPAGENTS_JAIL_APPARMOR=unconfined
       (that drops ALL of docker-default, not just its deny-mount rule)
       Or disable the jail:  DEEPAGENTS_JAIL=0
```

**Never** fall back to `unconfined` automatically. That would silently re-make the trade fork 10
rejected, on behalf of an operator who did not choose it.

Engine detection reuses `scripts/lib/hostmap.sh`'s existing native-Linux-vs-Desktop logic — do not add
a second detector.

### 8.2 Parity

`check-parity.{sh,ps1}` compares line counts plus a `markers` list that must appear in **both**
`run-docker.{ps1,sh}`. Add:

```
"apparmor=deepagent-userns"
"install-apparmor-profile"
```

so a one-sided edit to the preflight fails CI, exactly as the mask markers already do.

### 8.3 `harness doctor` — `doctor.py:228-255`

The current block errors on **any** confinement. Replace with:

| Condition (jail on) | Verdict |
|---|---|
| confinement is `deepagent-userns` (or a `deepagent-userns//child` sub-profile), **enforce** mode | **info** — LSM gate satisfied |
| confinement is `deepagent-userns` in **complain** mode | **error** — a complain-mode profile logs instead of enforcing; treating it as satisfied would report a boundary that is not there |
| confinement is any other profile (`docker-default`, …) | **error** — current message, with `install-apparmor-profile.sh` promoted to remedy #1 and `unconfined` demoted to #2 |
| no confinement + `DEEPAGENTS_JAIL_APPARMOR=unconfined` | **warning** — unchanged, names what was dropped |
| no confinement, nothing requested | **info** — unchanged |
| any of the above | **also** run `apparmor.verify_profile` on the committed artifact → **error** on drift (invariant 38, the LSM twin of the existing seccomp check) |
| jail off | skipped entirely — unchanged |

`jail.apparmor_confinement()` must tolerate AppArmor's real attr formats before this branch can rely
on it: `profile (enforce)`, `profile (complain)`, and child profiles `parent//child`. Verify the
existing parsing covers all three and extend it if not — the mode suffix in particular is the
difference between the pass and the error row above.

## 9. Distribution — the genuinely hard part

A seccomp profile is a **file whose path** is handed to the daemon at `docker run`; it needs no host
state. An AppArmor profile is not: it must be compiled into the host kernel as root **before** the
container starts, and `--security-opt apparmor=deepagent-userns` merely *references* an already-loaded
profile by name. That is why J is a milestone with an open fork and not a launcher flag.

`scripts/install-apparmor-profile.sh`:

```
install-apparmor-profile.sh                # apparmor_parser -r -W apparmor/deepagent-userns
install-apparmor-profile.sh --uninstall    # apparmor_parser -R apparmor/deepagent-userns
install-apparmor-profile.sh --status       # loaded? which sha256?
```

Requirements:

- **Refuse cleanly, rc 2, with an explanation** when `/sys/kernel/security/apparmor` is absent —
  "this host has no AppArmor; the jail needs nothing here" is a success condition for the operator,
  not a failure.
- **Refuse** when `apparmor_parser` is missing, naming the package (`apparmor` / `apparmor-utils`).
- **Require root; print the exact `sudo` line rather than self-escalating.** A script that elevates
  itself is a worse habit than one that tells you what to run.
- **Idempotent** — `-r` replaces an existing load.
- Print the profile's `holder-profile-sha256` on install and on `--status`, so a stale load is
  diagnosable by eye.
- Loud about *where*: "this loads the profile into **this** machine's kernel; if your Docker daemon
  runs elsewhere (remote host, Colima/Lima VM, WSL distro), load it there instead."
- Document uninstall in `apparmor/README.md`. A loaded profile is host state the harness created;
  shipping no removal path is a footgun.

## 10. CI

**Step 1 — now, unconditional.** Add `tests/test_apparmor.py` to the host-tier pytest list in
`.github/workflows/ci.yml`. This is the invariant-38 guard and needs no AppArmor on the runner: it
asserts the **committed artifact** passes `verify_profile`, exactly as
`test_seccomp.py::test_committed_profile_is_narrow` does today. A swap to a permissive `mount,`, a
widened rule set, or a hand-edited profile fails the build.

**Step 2 — measure on the record, then decide.** GitHub-hosted `ubuntu-24.04` runners have AppArmor
loaded and permit `sudo`, so loading the profile is *plausible* — but `milestone4.md` §11.6 records it
as **unverified**, and this milestone exists because someone previously inferred a gate instead of
measuring it.

**Shipped as an explicit measurement job, `apparmor-load-probe`, with `continue-on-error: true`.** It
prints what confines a container by default, loads the profile with the install script, checks the
daemon accepts it, and runs `jail-check.py` under it. It is deliberately **not a gate** — it exists to
put the answer in a CI log rather than in someone's assumption. The next edit to `ci.yml` is driven by
what it reports:

- **If it loads and the jail starts:** fold the install step into the `smoke` job and pin
  `JAIL_CHECK=1` there, converting today's rc-77 skip into a real red/green gate — the strongest
  regression guard available for this milestone, because it exercises the jail on the host class that
  broke it. Then delete `apparmor-load-probe`.
- **If it does not:** delete the job and record *why* beside the existing skip comment. An honest
  documented skip beats a gate that passes for the wrong reason.

## 11. Config knobs (delta to `milestone4.md` §13)

| Knob | Where | Default **after J** | Effect |
|---|---|---|---|
| `DEEPAGENTS_JAIL_APPARMOR` | launcher (host-side) | unset → **`deepagent-userns` on a Linux engine**; nothing on Docker Desktop/macOS | Was: unset → pass nothing → jail fails inside the container on an AppArmor host. Now: the narrowed profile is the default and is **probed before launch**. `unconfined` still available as the explicitly wider opt-in. Any other value = a host-loaded profile name, also probed. |
| `DEEPAGENTS_APPARMOR_PROFILE` | container/host env | unset | Path override for the vendored artifact, mirroring `DEEPAGENTS_SECCOMP_PROFILE`. Test/dev seam. |

`DEEPAGENTS_JAIL` is untouched — still `0`. **Removable contract unchanged:** with the jail off, none
of this code runs, no profile is selected, no probe fires, and the harness behaves exactly as slices
A–G (invariants 26 / 35).

## 12. The trade, stated honestly

**What the narrowed profile costs vs. stock `docker-default`:** exactly the seven mount rules in §6 —
`rslave` on `/`, tmpfs, bind/rbind, the ro-remount, proc, and `pivot_root`. Every other rule, including
all four denies and the ptrace peer restriction, survives byte-identical and is asserted to
(`verify_profile`).

**What `apparmor=unconfined` costs, for contrast** (`milestone4.md` §11.6's accounting, retained
because operators will still reach for it):

- Largely — but not wholly — redundant with other protections. Docker independently applies OCI
  `maskedPaths`/`readonlyPaths` covering most of the same `/proc` targets, and the kernel checks a
  write to `/proc/sysrq-trigger` against `CAP_SYS_ADMIN` **in the initial user namespace**, which a
  nested-userns process does not hold.
- **Not zero**, for one specific reason: bwrap mounts a **fresh** procfs (`--proc /proc`) inside the
  jail, which does **not** inherit Docker's masks. Inside the jail, those init-userns capability
  checks are the only thing left. That is a thinner backstop than the layered one — and it is the
  whole argument for J.
- Categorically wider than fork 7 pinned: "five relaxed syscalls" becomes "five syscalls **and an
  entire LSM off**."

**What J does not change:** the jail still requires the seccomp relaxation, still exposes kernel
user-namespace attack surface, and is therefore still opt-in. J narrows *where* it can run, not *what*
it costs to run it.

## 13. Gotchas

### 13.1 The mount rule set is derived, not measured — this is the schedule risk

§6's seven rules come from reading bwrap's syscall sequence (`unshare` → `mount(MS_SLAVE|MS_REC, /)`
→ binds → `--proc` → `--dev` → `pivot_root` → `umount`), not from a passing run. `--dev` in
particular builds a tmpfs plus device binds and may need a rule the list lacks.

**Process:** on a live Ubuntu host with the profile loaded, run the jail and read the kernel's own
denials —

```bash
sudo dmesg | grep 'apparmor="DENIED"'
sudo grep apparmor /var/log/audit/audit.log      # where auditd is running
```

Add **only** what the denials demand, one rule at a time, each with a justification line in
`apparmor/README.md`. Resist the temptation to paste a broad `mount,` to get unblocked — that is
`unconfined` wearing a costume, and `verify_profile` is written to reject it.

### 13.2 Stale profile drift is undetectable from inside the container

A host can carry an old `deepagent-userns` while the repo's artifact has moved on.
`/proc/self/attr/apparmor/current` gives the **name**, never the content, and there is no in-container
route to the loaded rules. **Accept it; mitigate:** the install script and `--status` print the
`holder-profile-sha256`, and `apparmor/README.md` says re-run install after every `apparmor-sync`.
Do not invent an in-container content check — there isn't one, and a fake one is worse than a
documented limit.

**Do not reimplement the hash in shell.** The first cut of `install-apparmor-profile.sh` computed it
with `awk`/`sha256sum` and disagreed with the generator immediately — over exactly the `#include`
header/body question in §7. A stale-load diagnostic that reports a different number than the tool
that wrote the file is worse than no diagnostic. The script shells out to
`harness.apparmor.body_sha256`: one implementation, in Python, called from shell — the same rule
slice A applies to the mask matcher.

### 13.3 Load the profile where the daemon runs

Docker Desktop, Colima/Lima, WSL2, and remote daemons all put `dockerd` somewhere other than the shell
you typed in. `apparmor_parser` on the CLI host does nothing for them. §8.1's probe is what makes this
survivable — it asks the daemon — but the install script must say it too (§9).

### 13.4 Complain mode is not enforcement

`apparmor_parser -C` (or a profile loaded in complain mode by other tooling) logs violations and
allows them. bwrap would then run, the jail would appear to work, and the LSM would be enforcing
nothing. §8.3 makes this an error rather than a pass.

### 13.5 Nested profiles and the shell's jail

The shell tool's nested `sandbox-exec` bwrap runs **inside** the harness's already-jailed process, so
it needs the same mount rules — which it has, since AppArmor applies the profile to the whole
container, not per-invocation. No extra rule set. Verify in §13.1's live run that the nested jail also
starts (the smoke gate's state-dir-unreachable check exercises it).

### 13.6 NetJail

The probe container (§8.1) and the install script need no network beyond the local daemon socket, and
`apparmor-sync` is dev-time only. **No `netjail/allowed-domains.txt` entry is required** — stated so
nobody adds one defensively.

## 14. Open forks

- **J1 — profile name.** `deepagent-userns` (proposed, matches `seccomp/userns.json`'s framing).
  Alternative: version-suffixed (`deepagent-userns-1`) so a stale load is visible by name, at the cost
  of an install/uninstall churn on every profile change and a launcher default that moves. **Recommend
  the stable name + sha256 reporting (§13.2).** Confirm before writing §8.1.
- **J2 — CI gate (§10 step 2).** Blocked on measurement, not on a decision. Whichever way it goes gets
  recorded in `ci.yml`.
- **J3 — does the harness ever install the profile itself?** No, proposed: the launcher aborts with
  the command rather than running `sudo` on the operator's behalf. Silent root actions from a tool
  whose whole point is a trust boundary would be self-undermining. **Recommend keeping install
  strictly manual.**
- **J4 — SELinux.** Out of scope (§3), but worth a tracked follow-up stub so the next person hitting
  `RHEL + DEEPAGENTS_JAIL=1` finds a known-gap note instead of silence.

## 15. PR plan

Single PR (**M4 PR7** in `milestone4.md` §17's numbering), on `feat/milestone_4` or a branch off it,
in this order — each step leaves the tree green:

1. ✅ `harness/apparmor.py` + `tests/test_apparmor.py` + the pinned `template.go` fixture + the
   `cli.dispatch` entry. Pure, host-testable, no artifact yet.
2. ✅ Run `apparmor-sync` → commit `apparmor/deepagent-userns`, `apparmor/docker-default.rendered`,
   `apparmor/README.md`; Dockerfile copy. Now `--check` and the committed-artifact tests are
   meaningful.
3. ✅ `scripts/install-apparmor-profile.sh`.
4. ⬜ **Live-host iteration (§13.1)** — measure on an AppArmor host, adjust `RELAXED_MOUNT_RULES`,
   re-sync, re-commit. *Nothing downstream is trustworthy before this step passes.* **Not done: the
   dev host is Docker Desktop/WSL2 and cannot run it.** Everything below was built against the
   derived rule set and may need one more `apparmor-sync` after the measurement.
5. ✅ Launcher default + probe (`run-docker.{sh,ps1}`, `smoke.{sh,ps1}`) + parity markers.
6. ✅ `doctor` branch + its tests.
7. ✅ CI step 1 (host-tier `test_apparmor.py`); CI step 2 shipped as the non-gating
   `apparmor-load-probe` measurement job.
8. ✅ Doc corrections + `milestone4_invariants.md` 37–38 marked built + invariants 39–41 appended.

## 16. Test matrix

| File | Cases | Tier | State |
|---|---|---|---|
| `tests/test_apparmor.py` (23 cases) | extraction: finds the base template in the pinned fixture, raises on a file without it and on a template missing the deny rule. Rendering: substitutes the pinned params, expands the signal/ptrace peer to *our* profile name, raises on an unknown directive, raises when the imports `range` disappears. Relaxation: replaces exactly the deny line, leaves every other line byte-identical, refuses on zero or >1 deny lines. Verification: rejects stock docker-default, a bare `mount,` catch-all, a widened set, a missing rule, a removed `deny`, a rename, a hand-edit (sha), and a change outside the mount rule (baseline diff). Committed artifacts: profile is narrow, baseline is byte-reproducible from the pinned template, every critical deny survives | host | ✅ 23 pass |
| `tests/test_doctor.py` (add, 4) | our profile in enforce mode → pass, and *not* via the docker-default error path; `parent//child` accepted; complain mode → error; a widened vendored profile → error naming the catch-all. Existing AppArmor cases repointed at `apparmor_confinement_detail` | host | ✅ 25 pass |
| `tests/test_jail.py` (add, 5) | `apparmor_confinement_detail` reports the mode, surfaces `complain`, keeps `parent//child` intact, reads unconfined as `(None, None)`, survives a missing mode suffix | host | ✅ 38 pass |
| `scripts/check-parity.{sh,ps1}` | the three new markers present in both `run-docker` scripts | CI | ✅ both pass |
| launcher preflight | exercised directly against the local daemon: no-LSM host ⇒ selects nothing (not a false-positive profile claim); LSM-in-force + profile absent ⇒ aborts rc 1 with the install command | manual | ✅ both paths |
| smoke (`JAIL_CHECK=1`) | with `deepagent-userns` loaded on an AppArmor host: bwrap unshares, a masked path reads 0 bytes inside the jail with no docker mask, workspace writable, `/project` read-only, state dir unreachable from the shell | image | ⬜ **blocked on §13.1** |

Conventions per `deepagent-image/CLAUDE.md` → "Test suite layout": no keys, no network, all writes to
`tmp_path`, every guard behaviour ships with a regression test.

## 17. Invariants this adds

Append to `milestone4_invariants.md` (37–38 already exist; 38 flips to **built** when this ships):

39. **The launcher never runs the jail with the wrong LSM stance.** With `DEEPAGENTS_JAIL=1` on a
    Linux engine, `run-docker` selects `deepagent-userns`, probes that the **daemon** can apply it,
    and **aborts before `docker run`** when it cannot — naming the install command. It never falls
    back to `apparmor=unconfined` on its own, because that is a categorically wider trade
    (`milestone4.md` §16 fork 7/10) that only an operator may make. *(`check-parity` markers +
    launcher review; the abort path is shell-level.)*
40. **Complain mode is not a pass.** A `deepagent-userns` profile loaded in complain mode logs
    violations and allows them, so the LSM is enforcing nothing. `doctor` reports it as an **error**,
    not as the satisfied gate its name suggests. *(`test_doctor.py`.)*
41. **The vendored profile is reproducible from upstream.** `apparmor-sync` regenerates the committed
    artifact byte-for-byte from moby `MOBY_TAG`'s template plus exactly `RELAXED_MOUNT_RULES`; a
    hand-edit is detected by `verify_profile`'s baseline + sha check. The profile is a generated
    artifact, and "do not hand-edit" is enforced rather than requested. *(`test_apparmor.py`.)*

---

**Cross-refs:** `docs/milestones/in-progress/milestone4.md` §4 (slice J intent), §11.4 (the shipped
jail + the LSM-scope caveat), §11.6 (the problem statement), §13 (knobs), §16 fork 10 (the pinned
decision), §17 PR7; `milestone4_invariants.md` 31 (the seccomp twin), 37–38;
`deepagent-image/seccomp/README.md` + `harness/seccomp.py` (the shape J mirrors);
`deepagent-image/CLAUDE.md` → "bwrap fs jail"; code seams — `harness/jail.py`
(`classify_bwrap_failure`, `apparmor_confinement`, `apparmor_hint`), `harness/doctor.py:228`,
`harness/cli.py` (`dispatch`), `scripts/run-docker.sh:345-385` / `run-docker.ps1:352-400`,
`scripts/smoke.sh:205-222`, `scripts/check-parity.sh` (`markers`).
