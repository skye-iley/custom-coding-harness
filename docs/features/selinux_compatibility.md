# SELinux compatibility for the bwrap jail — pre-release check

> **Status: PLANNED, unmeasured.** Not part of Milestone 4.1 and not a blocker for it. This is a
> **pre-release compatibility item**: before the harness is offered to anyone who is not on
> Ubuntu/Debian or Docker Desktop, someone has to run `DEEPAGENTS_JAIL=1` on a RHEL/Fedora host and
> record what happens.
>
> **What is true today:** SELinux is *not confirmed to work, and not confirmed to fail.* M4.1 fork J4
> closed the reporting gap only — the harness detects an SELinux context, refuses to report it as an
> AppArmor profile, routes a mount denial to a known-gap message, and has `doctor` warn. That is
> honesty about an unknown, not coverage.

## 1. Why this is open

Slice J's whole premise (`docs/milestones/complete/milestone4.1.md` §2) is that an *inferred*
boundary is worth less than no claim at all. Slice H shipped "verified" on Docker Desktop/WSL2, a host
class that loads no LSM policy and therefore structurally could not see the AppArmor gate. M4.1 fixed
that for AppArmor by measuring on a live host — 7 rules, four of them wrong as derived (§13.1a).

The same argument applies unchanged to the third host class. AppArmor's `docker-default` blocks bwrap
with a literal `deny mount,`; SELinux is a different mechanism (type enforcement over `container_t`,
not a path/operation deny list), so **the AppArmor result predicts nothing here** — neither a failure
nor a pass. Guessing either way is the mistake this milestone exists to stop repeating.

## 2. What is genuinely unknown

1. **Does bwrap build the namespace under `container_t`?** Container SELinux policy governs mount,
   `pivot_root`, and the labels a process may transition to. Whether the stock `container-selinux`
   policy permits an unprivileged nested user namespace's mounts is an empirical question.
2. **Does the kernel's third gate behave the same?** `mount_too_revealing()` is not an LSM check, so it
   should fire identically — but `--security-opt systempaths=unconfined` interacts with a
   *differently labelled* `/proc`, and "should" is the word this doc exists to remove.
3. **Is `--security-opt label=disable` sufficient, and what does it cost?** It is the escape hatch the
   run-time surface names (marked UNVERIFIED). It drops SELinux labelling for the whole container —
   categorically the same all-or-nothing shape as `apparmor=unconfined`, which M4.1 §12 rejected as a
   default. If a narrower policy module works, that is strictly better.
4. **Podman/rootless overlap.** RHEL's default container engine is Podman, which is also untested and
   applies profiles differently. The two gaps will be hit by the same operator on the same host.

## 3. How to measure it

Same protocol as `milestone4.1.md` §13.8, with the LSM-specific tooling swapped:

```bash
# Fedora or RHEL/Alma/Rocky VM, SELinux enforcing (`getenforce` = Enforcing), Docker CE installed.
sestatus                                          # confirm enforcing, note policy version
docker run --rm deepagent-harness cat /proc/self/attr/current   # expect system_u:system_r:container_t:s0:c…

# 1. Does the jail start at all, with the two flags the launcher already passes?
DEEPAGENTS_JAIL=1 JAIL_CHECK=1 ./deepagent-image/scripts/smoke.sh

# 2. If it fails, read the denials — ausearch, NOT dmesg | grep apparmor
sudo ausearch -m AVC -ts recent | audit2allow            # what was denied, and by which rule
sudo ausearch -m AVC -ts recent | audit2allow -M deepagent-userns   # candidate module, DO NOT ship unread

# 3. The control, for whichever result: with the masks kept, it must still fail at --proc.
DEEPAGENTS_JAIL_SYSTEMPATHS=default DEEPAGENTS_JAIL=1 JAIL_CHECK=1 ./deepagent-image/scripts/smoke.sh
sudo ausearch -m AVC -ts recent | wc -l                  # a procfs failure logs no AVC at all
```

**Record the host the way `milestone4.md` §11.4's standing rule requires:** distro + version, kernel,
Docker version, `sestatus` output, and the container's own context. A measurement that does not name
its LSM is the defect M4.1 was written to correct.

## 4. Outcomes and what each obliges

| Result | What ships |
|---|---|
| Jail starts under stock policy | Record it (with the `ausearch` evidence that nothing was denied), flip `doctor`'s warning to an info on that host class, and add the host to the CI/measurement table. Nothing to vendor. |
| Denied, and a *narrow* policy module fixes it | Vendor it under the same contract the AppArmor artifact carries: generated not hand-written, reproducible from a pinned upstream, verified offline by a `verify_profile` twin, installed by a script the harness never runs itself. A hand-rolled `audit2allow` dump pasted into the repo is exactly the "derived, not measured" failure again. |
| Denied, and only `label=disable` clears it | Ship it as an announced opt-in knob (the treatment `DEEPAGENTS_JAIL_APPARMOR=unconfined` gets), never a launcher default — it is the whole-mechanism drop M4.1 §12 argues against. |
| Denied with no workable path | Say so plainly in `apparmor/README.md`, `deepagent-image/CLAUDE.md` and `harness doctor`: the jail does not run on SELinux hosts. An honest documented gap is a supported answer; a silent one is not. |

## 5. Acceptance criteria

- A measurement exists, on a named host, in a doc — not in a commit message and not in someone's
  memory.
- `harness doctor` on an SELinux host reports something derived from that measurement rather than the
  current "untested" warning.
- If anything is vendored, it is generated + verified offline + CI-guarded, like
  `apparmor/deepagent-userns` and `seccomp/userns.json`.
- Until all of the above: **no claim of SELinux support anywhere in the docs or the run-time surface.**

## 6. Where the current behaviour lives

- `harness/jail.py` — `selinux_confinement()`, `_selinux_context_from()`, `selinux_hint()`,
  `lsm_hint()`; `_profile_and_mode_from` refuses to read a context as an AppArmor profile (the two
  LSMs share `/proc/self/attr/current`).
- `harness/doctor.py` — the SELinux warning branch.
- `scripts/jail-check.py` — the `lsm` skip prints the SELinux hint when a context is present.
- Tests: `tests/test_jail.py` SELinux cases, `tests/test_doctor.py::
  test_doctor_reports_selinux_as_an_untested_gap_not_an_apparmor_problem`.
- Rationale + the misdiagnosis this replaced: `milestone4.1.md` §14 fork J4; invariant 44 in
  `docs/milestones/complete/milestone4.md` §19.
