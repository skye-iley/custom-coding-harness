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
5. Floor redundant. **v1: enforced ≥2 ways independently** — docker mask always emits it, and the resolver drops any negation/allow of it. **The 3rd leg (file backend explicitly refuses a floor path) is NOT built in v1** — `agent.py:_resolve_path` only checks for workspace escape (`pathguard.validate_path`), it has no floor/mask awareness; a floor file simply reads empty because the docker overlay changed the real fs. The backend-refusal leg (and bwrap never-binds-it) is **aspirational — lands with slice H**. So in v1 no single layer failure exposes the floor across the two built legs, but the "≥3 independent" redundancy is not yet real. *(Tested: `TestFloorRedundancy` documents the two working legs and marks the 3rd aspirational.)*
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
14. Honest scope. Guard covers file tools only. Shell tool bounded by container root + mask, not the guard. Never described as sandboxing the shell.

permission_denied interrupt (completes M3 S4)

> **v1: BUILT, audit-only (§11.3).** `cli._should_audit_path_denials` wires `hitl.make_path_denied_handler`
> as `on_path_denied` when HITL is on and the `permission_denied` system interrupt is enabled. Every
> `PathGuardDenied` v1 can produce is a true workspace escape (pathguard has no floor/mask awareness) —
> never approvable by design (invariant 16) — so the handler does not raise a `GraphInterrupt` or offer
> a choice; it audits directly (`audit.record_interrupt`, `resolved_by="system"`) and always denies.
> Invariants 15, 17, 18 are built and tested. Invariant 16 remains structurally-present-but-unreachable:
> the "approve an in-bounds masked exception" flow needs a denial type that doesn't exist until the
> bwrap file-tool jail (H) makes a masked-read denial explicit.

15. Fail-closed. Headless / non-TTY / no-default → deny (default=False). Never hang, never default-allow. **Built.** No interrupt suspends the graph for this denial type (nothing to decide), so there is nothing to hang on; the pathguard itself is always fail-closed — a denial raises `PathGuardDenied` — and the audit-only handler never returns `True`. *(`test_hitl.py::test_path_denied_handler_always_denies`.)*
16. *(deferred)* Approvable scope bounded. Operator may approve one-off only for in-bounds, non-floor path. Escape (realpath outside workspace) never approvable. *(Nothing is approvable in v1 — structurally satisfied but not exercised: `hitl.make_path_denied_handler` has no code path that returns `True`.)*
17. Audited without leaking. Every denial recorded via audit.record_interrupt — path/tier/op in meta, never file contents. **Built.** `record_interrupt` now persists `meta` (previously silently dropped for every interrupt kind, not just this one — fixed alongside D); `op`/`reason` are recorded, `tier` is omitted (pathguard has no tier concept — every v1 denial is escape-type, not floor-tiered). *(`test_audit.py::test_meta_is_persisted`, `test_hitl.py::test_path_denied_handler_audits_the_denial`.)*
18. Off-HITL = plain refusal. No .harness-config.yaml → denial is a normal refused tool result (PathGuardDenied), byte-for-byte a blocked call in M3. **Built.** Identical on-HITL vs. off-HITL except for the audit line — this slice adds visibility, not a behavior change. *(`test_cli.py::test_should_audit_path_denials_off_when_hitl_off`.)*

git-pr (secret-handling hazard)

19. Masked never committed. git-pr staging excludes the resolved mask set. A masked-empty .env is never staged → origin's real secret file untouched. (The one hazard the earlier draft missed.)

State isolation

20. Authoritative config agent-unreachable. <state>/agentignore + floor + snapshot live in state dir, outside the workspace mount. Agent file/shell tools (rooted at workspace) cannot read or edit them.
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