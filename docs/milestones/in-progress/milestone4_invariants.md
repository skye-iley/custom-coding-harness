# Milestone 4 — Invariants

> Test-facing companion to `milestone4.md` (same folder). Kept **separate** while M4 is in-progress
> so these checkable properties drive testing without the planning/implementation prose around them.
> On completion this folds into `milestone4.md` as a section and the standalone file is dropped
> (see the milestone lifecycle in `docs/README.md`).

Milestone 4 = real trust boundary. Core goal: agent cannot read/exfil workspace secrets, cannot escape workspace, and boundary cannot silently regress. Invariants below = properties that MUST hold for that goal. Grouped, each phrased as checkable assertion.

Floor (designated-secret tier — the inviolable core)

1. Floor never readable. Designated-secret floor path reads empty to every process in container (file tools, shell, subprocesses) — not just file backend.
2. Floor never negatable. No .agentignore !-negation, allow-list entry, or #!mode: allow can expose a floor path. Resolver drops such entries + warns.
3. Floor never approvable. permission_denied interrupt never offers approve for a floor path. Hard-deny, no offer.
4. Floor always emitted. Floor mask mounts applied even in allow mode, even with bwrap off, even when the rest of masking is minimized.
5. Floor redundant. **With slice H on (`DEEPAGENTS_JAIL=1`) the ≥3-leg claim is real**: (1) docker mask always emits it, (2) the resolver drops any negation/allow of it, (3) the jailed worker explicitly refuses a floor path, (4) `jail.bwrap_args` never binds one — so a floor path is unreadable even if the docker overlay were disabled or misconfigured. **With the jail off (the default), v1's two legs still stand and no more** — see the v1 note below, which remains accurate for that configuration. *(Tested: `test_jail.py` for leg 4, `test_fsjail.py` for leg 3.)*
5v1. *(jail off — the default)* **v1: enforced ≥2 ways independently** — docker mask always emits it, and the resolver drops any negation/allow of it. **The 3rd leg (file backend explicitly refuses a floor path) is NOT built in v1** — `agent.py:_resolve_path` only checks for workspace escape (`pathguard.validate_path`), it has no floor/mask awareness; a floor file simply reads empty because the docker overlay changed the real fs. The backend-refusal leg (and bwrap never-binds-it) is **aspirational — lands with slice H**. So in v1 no single layer failure exposes the floor across the two built legs, but the "≥3 independent" redundancy is not yet real. *(Tested: `TestFloorRedundancy` documents the two working legs and marks the 3rd aspirational.)*
6. Floor unreachable via alias. Symlink whose realpath targets a floor path is itself masked (no re-exposure through a link).

Mask (pattern-default + general tier)

7. Masked reads empty. Any masked path (.env, *.pem, etc.) reads present-but-empty to every process — via real mounted-fs change, not tool-layer filter.
8. Unmasked byte-identical. A non-masked source file is byte-for-byte unchanged vs. no-mask run.
9. Frozen at launch. Mask set computed once host-side before docker run. No runtime edit to in-workspace .agentignore, and no refresh_workspace, can unmask the current session.
10. Mask matches what agent sees. Scan runs against MountWorkspace (ephemeral copy when -Ephemeral), not the real tree.

Path guard (defense-in-depth)

11. commonpath, not startswith. Sibling escape (/workspace-evil vs /workspace) refused. ../ traversal, absolute path, in-workspace symlink-out all refused.
12. Guard on resolved path. Check runs after virtual-mode de-nesting + super()._resolve_path, so symlink-out is caught.
13. Legit ops never blocked. Real path under workspace passes (commonpath == base). Guard has zero false positives → stays always-on without breaking removable contract.
14. Honest scope. Guard covers file tools only. Shell tool bounded by container root + mask, not the guard. Never described as sandboxing the shell. **Slice H closes this gap when on**: `execute` routes through `sandbox-exec`, so shell and file tools share one bind whitelist and the shell can no longer reach the state dir by absolute path (verified: inside the jail `/project` lists only `workspace`). The guard stays in front regardless — the jail is the boundary, the guard remains the cheap defense-in-depth layer, and keeping it means a denial still raises at the seam slice D wired. **With the jail off, this invariant reads exactly as written above.**

permission_denied interrupt (completes M3 S4)

> **v1: BUILT, audit-only (§11.3).** `cli._should_audit_path_denials` wires `hitl.make_path_denied_handler`
> as `on_path_denied` when HITL is on and the `permission_denied` system interrupt is enabled. Every
> `PathGuardDenied` v1 can produce is a true workspace escape (pathguard has no floor/mask awareness) —
> never approvable by design (invariant 16) — so the handler does not raise a `GraphInterrupt` or offer
> a choice; it audits directly (`audit.record_interrupt`, `resolved_by="system"`) and always denies.
> The denial is surfaced on two channels with different gating — an always-on operator stderr line
> (17b) and a HITL-gated structured record written **outside the workspace** so the agent cannot
> truncate it (17a). Invariants 15, 17, 17a, 17b, 18 are built and tested. Invariant 16 remains
> structurally-present-but-unreachable:
> the "approve an in-bounds masked exception" flow needs a denial type that doesn't exist until the
> bwrap file-tool jail (H) makes a masked-read denial explicit.

15. Fail-closed. Headless / non-TTY / no-default → deny (default=False). Never hang, never default-allow. **Built.** No interrupt suspends the graph for this denial type (nothing to decide), so there is nothing to hang on; the pathguard itself is always fail-closed — a denial raises `PathGuardDenied` — and the audit-only handler never returns `True`. *(`test_hitl.py::test_path_denied_handler_always_denies`.)*
16. Approvable scope bounded. Operator may approve one-off only for in-bounds, non-floor path. Escape (realpath outside workspace) never approvable. **Reachable as of slice H, deferred while the jail is off.** v1 had no approvable denial at all — a masked file was present-but-empty at the mount layer, never an explicit denial — so `hitl.make_path_denied_handler` had no code path returning `True`. Under the jail a masked read *is* an explicit denial, so the approvable case exists and H carries the interactive approve branch (milestone4.md §11.4). A floor path or a traversal-out-of-workspace stays hard-deny with no offer (§16 fork 4, unchanged). *(With the jail off the v1 reading still holds: nothing is approvable.)*
17. Audited without leaking. Every denial recorded via audit.record_interrupt — path/tier/op in meta, never file contents. **Built.** `record_interrupt` now persists `meta` (previously silently dropped for every interrupt kind, not just this one — fixed alongside D) and scrubs it **recursively**, so a nested dict/list is not a way around the §10 backstop; `op`/`reason`/`audit_only` are recorded, `tier` is omitted (pathguard has no tier concept — every v1 denial is escape-type, not floor-tiered). *(`test_audit.py::test_meta_is_persisted`, `::test_meta_scrub_reaches_nested_values`, `test_hitl.py::test_path_denied_handler_audits_the_denial`, `::test_path_denied_record_is_marked_audit_only`.)*
17a. **Denial record is agent-unreachable.** The record of an escape *attempt* never lands where the escaping agent can truncate it: it goes to `<state-dir>/denials.jsonl` (`audit.denials_path`), not the in-workspace `.agent_telemetry/interrupts.jsonl`, which is in-bounds for the path guard and freely writable by the agent's own file tools. This is invariant 20 applied to the audit trail. **Built**, with **one standing limit**: it closes the **file**-tool tamper path only — the shell tool is container-root-bounded rather than guard-covered (invariant 14), so it can still reach the state dir by absolute path until slice H. The other limit is now **closed**: the sink resolves through `archive.state_dir`, whose `<workspace>/.deepagents` fallback (when `DEEPAGENTS_STATE_DIR` is unset) would put the record back in the agent's reach. That fallback is already unreachable in practice for this sink — HITL-on implies `run-docker`, since the gitignored, un-`COPY`ed config file only reaches `/project` by bind-mount, and `run-docker` always sets the var — but it rested on an unchecked launcher convention until invariant 20a. *(`test_hitl.py::test_path_denied_record_lands_outside_the_workspace`.)*
17b. **A denial is never silent to the operator.** `agent._resolve_path` prints `[harness] path-guard DENIED — …` to stderr on every refusal, **ungated by HITL**. The structured record (17/17a) is HITL-gated, but the default posture is HITL-off, where the only other trace is the tool-error string the *model* reads back and can route around. stdout stays clean (headless JSON contract). An audit-write failure is likewise reported, not swallowed. **Built.** *(`test_agent.py::test_backend_reports_denial_on_stderr`, `::test_backend_stays_quiet_when_a_handler_approves`, `test_hitl.py::test_path_denied_handler_never_raises_on_audit_failure`.)*
18. Off-HITL = plain refusal. No .harness-config.yaml → denial is a normal refused tool result (PathGuardDenied), byte-for-byte a blocked call in M3. **Built.** The *refusal* is identical on-HITL vs. off-HITL; HITL adds only the structured record. The always-on stderr line (17b) means a denial is not byte-for-byte *silent* vs. M3 — deliberate, and scoped to an event M3 could not produce at all (the guard is M4-new, zero false positives per invariant 13). *(`test_agent.py::test_backend_denies_escape_without_a_handler` — asserts the backend's actual behaviour with `on_path_denied=None`; `test_cli.py::test_should_audit_path_denials_off_when_hitl_off` covers only the wiring predicate.)*

git-pr (secret-handling hazard)

19. Masked never committed. git-pr staging excludes the resolved mask set. A masked-empty .env is never staged → origin's real secret file untouched. (The one hazard the earlier draft missed.)

State isolation

20. Authoritative config agent-unreachable. <state>/agentignore + floor + snapshot live in state dir, outside the workspace mount. Agent file/shell tools (rooted at workspace) cannot read or edit them.
20a. **"Outside the workspace" is asserted, not assumed.** Invariant 20 (and 17a) hold only if the state dir actually resolves outside the workspace — but `archive.state_dir` falls back to `<workspace>/.deepagents` whenever `DEEPAGENTS_STATE_DIR` is unset, which in-container is the whole isolation silently gone. **Built:** `doctor.state_dir_inside_workspace` (realpath + `commonpath`, so `<ws>-state` reads as the sibling it is) → **error** when `DEEPAGENTS_IN_CONTAINER=1`, turning a launcher convention into a CI-checkable property. Off-container the same layout is the documented bare-host default with no boundary to protect → **info**, so doctor does not fail every legitimate host run. *(`test_doctor.py::test_in_container_state_dir_inside_workspace_is_an_error`, `::test_bare_host_state_dir_inside_workspace_is_not_an_error`, `::test_state_dir_sibling_is_not_inside`.)*
21. mask_add raise-only. Agent tool can only raise protection (next run), writes state-dir config. No mask_remove. Cannot unmask current session.

Regression / config integrity

22. Weakened floor fails CI — **partial in v1.** A floor **negation/allow** (a `!`/allow entry targeting a floor path) → doctor **error** → non-zero → CI red (built + tested: `test_doctor_detects_floor_negation`). A floor **removal** (the `#!floor:` block deleted entirely) → doctor **warning** (visible in the summary) but **rc 0 — does NOT fail CI**: doctor cannot know a floor "should" be present when the block is gone, and a legitimately floor-less workspace must still pass. So negation-weakening is caught; deletion-weakening is only surfaced, not blocked. A hard "floor is required → error" gate belongs in a seeded security regression test, not doctor semantics (tracked TODO in `test_doctor_warns_missing_floor`).
23. doctor reuses runtime resolver. doctor validates via the real mask.resolve — validation cannot drift from enforcement.
24. Protection reduction is loud. A path masked last launch but absent now → loud warning (snapshot diff). Never silent.
25. CI keyless. Every tier runs with no provider keys. Security suite runs on every push/PR (host + image tiers).

Contract / structural

26. Removable seam. DEEPAGENTS_MASK=0 + no .harness-config.yaml ⇒ harness byte-for-byte M3 (scan, mounts, floor, snapshot all off).
27. Two-stack. Resolver/scanner/doctor run in harness venv, stdlib only, no pip dep (pathspec re-implemented). Never touch workspace conda env.
28. Acyclic imports. mask.py imports no harness sibling. permission_denied originates from guard/backend seam (may import interrupt), never from cost.py.
29. Script parity. .ps1 ↔ .sh scan-parse + mask-emit identical, enforced by check-parity in CI.
30. Every guard behaviour has a regression test. Each floor/guard property ships with a test that fails on buggy code, passes on fix.

Jail (slice H — opt-in, `DEEPAGENTS_JAIL=1`)

31. Seccomp relaxation stays narrow. The vendored profile is Docker's default with **exactly** `clone`, `unshare`, `mount`, `umount2`, `pivot_root` relaxed. `defaultAction` remains `SCMP_ACT_ERRNO`; a swap to `seccomp=unconfined`, or a widened relaxation set, fails `seccomp.verify_profile` → `seccomp-sync --check` → CI. Observable at runtime: under the profile `bpf`/`keyctl`/`perf_event_open` return EPERM (filtered); under unconfined they reach the kernel (EINVAL/EFAULT). *(`test_seccomp.py`.)*
32. Jail fails closed. If the jail cannot be built (bwrap missing, userns refused, worker non-zero exit) the op is **refused** — never a silent fallback to in-process IO, which would downgrade the boundary invisibly mid-session. *(`test_agent.py` jail-unavailable case.)*
33. All fs tools route through the jail. `build_agent` asserts every fs-touching method the backend exposes is overridden by the jailed subclass, compared against `LocalShellBackend`'s method set. A deepagents upgrade adding a new fs method, or a new MCP fs tool wired past the backend, fails **loudly at construction** rather than silently reopening the bypass. Same discipline as the existing `_resolve_path` upstream-API guard. *(`test_agent.py`.)*
34. Jailed semantics match unjailed. Because the per-call worker is stdlib-only (importing deepagents costs ~2.2 s, milestone4.md §16 fork 8), the thin method bodies are reimplemented client-side. A differential test asserts the jailed backend returns results identical to an unjailed `LocalShellBackend` across a matrix of inputs, so drift is a checked property rather than a hope. *(`test_agent.py` differential cases.)*
35. Jail is opt-in and removable. `DEEPAGENTS_JAIL` defaults to `0`; off, no seccomp profile is passed, no bwrap is spawned, and the harness behaves exactly as slices A–G. Enabling it is a deliberate operator trade — the relaxation exposes kernel user-namespace surface (`seccomp/README.md`).