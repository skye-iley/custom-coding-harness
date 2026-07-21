# Milestone 4 — Real Trust Boundary (Workspace Visibility + Path Guard)

> **Status:** ⬜ Planned. Successor to `docs/milestones/complete/milestone3.md` (HITL). Promotes the
> `design_doc.md` **§2 Workspace Visibility & Secret Masking** + **Path Guard** designs, backed by the
> **§10 security verification suite**, into a built, tested slice — and pulls in the two `design_doc.md`
> §12 operational items this work structurally needs (**§12.1 CI**, **§12.2 `harness doctor`**). The
> mechanics of *which paths the agent sees* are fully specified in
> **`docs/features/workspace_visibility.md`** — this milestone schedules the build, fixes scope
> (v1 vs. deferred), and wires the policy into the harness's existing seams (HITL, git lifecycle,
> config validation). Read §2 (design_doc) and the feature plan first.

---

## 0. Planned slices

Ordered by leverage; the config resolver (A) is built once and feeds every enforcement layer, so
later layers slot in without reworking policy (feature plan §7).

| Slice | Scope | Source | State |
|-------|-------|--------|-------|
| **A** policy + resolver + scanner | `.agentignore` gitignore-parity matcher, 3-tier policy, designated-secret floor, pre-flight scan container | feature plan §3/§5 | v1 |
| **B** docker mount-mask | deny-list empty-overlay mounts; always-on floor enforcer | feature plan §4.1 | v1 |
| **C** path-guard middleware | in-process `validate_path` (`commonpath`, not `startswith`) on the file-tool backend; defense-in-depth | design_doc §2 | v1 |
| **D** `permission_denied` interrupt | a masked/guarded denial escalates into the HITL spine — **completes the M3 S4 follow-up** | design_doc §9 + M3 | v1 |
| **E** `harness doctor` | pre-flight config validation (registry + `.agentignore` + floor coherence) | §12.2 | v1 (support) |
| **F** CI pipeline | run host + image + **security** suites on push/PR; script-parity lint | §12.1 | v1 (support) |
| **G** §10 security verification suite | tests that *prove* the boundary holds (floor never leaks, no traversal escape) | §10 | v1 (support) |
| **H** bwrap fs-tool jail | allow-list boundary; route **all** fs tools through `sandbox-exec`; verify nested userns | feature plan §4.2 | **stretch** |
| **I** overlayfs view | tool-agnostic true allow-list + upper-diff write-back | feature plan §4.3 | deferred v2 |

---

## 1. Goal & Definition of Done

Turn the harness's trust boundary from *aspirational* into *real and tested*. Today the trust
boundary is the Docker container alone (`docs/milestones/complete/mvp.md` §5): the workspace is a
**whole-tree bind mount**, so every file the user's own repo carries — `.env`, `id_rsa`,
`.aws/credentials` — is readable by the agent's file **and** shell tools. A prompt-injected or
misbehaving agent can exfiltrate them onto the mounted workspace or into a commit. M3 HITL already
concedes this gap: its pause gate is *phrasing-blind triage* ("for a hard destructive-action
guarantee, use the planned fs jail (§2)"), and a denied `rm -rf` was observed re-issued as `rmdir`.
The design opens on a dual-container/bwrap security story the code does not yet have.

M4 supplies the missing floor: a **policy** the user controls (`.agentignore`), an **inviolable
designated-secret tier** enforced redundantly, an always-on **docker mask** that hides those paths
from *every* tool, an in-process **path guard** against traversal, and a **security test suite** run
in **CI** that proves the boundary can't silently regress.

**Done when:**
- A designated secret in the workspace is **empty/unreadable to the agent's file and shell tools** at
  run time, and **no** `.agentignore` `!`-negation or allow-list entry can expose it (resolver
  rejects it; test asserts it).
- A masked path (`.env`, `*.pem`, etc.) reads empty; an unmasked source file is byte-for-byte
  unchanged (removable-seam contract: no `.agentignore` + floor-off ⇒ behaviour identical to M3).
- A path-guard traversal attempt (`../`, absolute, symlink-out) is refused, and — under HITL — surfaces
  as a `permission_denied` interrupt rather than a silent failure or crash.
- `harness doctor` flags an `.agentignore`/registry misconfiguration pre-flight, keyless.
- CI runs the host, image, and security tiers on every push/PR; a deliberately weakened floor **fails
  CI**.
- The mask/guard is **frozen at launch** — agent runtime edits to any in-workspace `.agentignore`
  cannot unmask the current session; a protection-*reduction* between runs warns loudly.

## 2. Why this milestone now

M3 matured the *runtime loop* (memory, cost, HITL, resilience, headless). The single largest
remaining vision-vs-reality gap is the **trust boundary**: §2 is 🟡 (bwrap installed-not-wired, path
guard snippet-only, visibility unbuilt) and §10 is ⬜ (risk analysis is design-only, nothing proves
the boundary). Two forces make this the natural successor to M3 specifically:

1. **M3 leans on it.** HITL's destructive-action guarantee is explicitly deferred to "the planned fs
   jail (§2)", and M3 left the **`permission_denied` system interrupt recognized but unenforced** —
   noted as riding on "the §2/§10 path-guard / NetJail gates" (M3 §0 / S4). M4 builds exactly those
   gates, so it *completes* an M3 follow-up rather than opening an unrelated front (slice D).
2. **It compounds.** The config resolver (A) is enforcement-agnostic and feeds docker-mask now and
   bwrap/overlayfs later; CI (F) and doctor (E) protect **every** future milestone, not just this one.

## 3. Scope — v1 vs. deferred

**In v1 (slices A–G):** the always-on, buildable-today floor — policy + resolver + scanner, docker
mount-mask (deny-list), path-guard middleware, `permission_denied` interrupt wiring, plus the support
tier (`doctor`, CI, security suite) that makes the floor validated and regression-proof. This slice
needs **no host-userns dependency** and works on Docker Desktop (Windows primary) today.

**Stretch (slice H — bwrap):** the *real* allow-list boundary (feature plan §4.2). Gated behind a
**nested-userns-in-docker verification** (design_doc §2 ~L87–91) that may need `--security-opt`
tweaks and is unverified — so it is a stretch layer, not a v1 blocker. Its hard requirement: route
**all** fs-touching tools (shell **and** the in-process deepagents file tools) through the jail, and
enforce that invariant in `agent.py` so a new tool can't silently reopen the bypass.

**Deferred v2 (slice I — overlayfs, and `hide` mode):** tool-agnostic true-absence view with
upper-diff write-back — changes today's live-write model (feature plan §4.3/§6). Out of M4.

**Sequencing hedge:** ship A→B→C→D (the floor) first — it delivers real secret-hiding across all
tools immediately. E/F/G harden it. H is attempted only after B–G are green and userns verifies.

## 4. Slices

### A — Policy + resolver + scanner *(feature plan §3, §5)*
One Python gitignore-parity matcher (vendored stdlib, no pip dep), run as a **read-only pre-flight
container** in the harness venv (`python3 -m harness.mask_scan`), emitting a flat resolved list the
`.ps1`/`.sh` launchers parse — **no matcher logic reimplemented in shell**. Three tiers (designated
secrets / pattern defaults / general `.agentignore`), deny-list default + strict allow-list mode.
Authoritative config lives in the **state dir** (outside the mount, agent-unreachable); in-workspace
`.agentignore` is convenience/advisory and **snapshotted** each launch to detect protection
reduction. Canonicalize paths to kill symlink re-exposure. Append-only `mask_add` agent tool (raise
protection only; no `mask_remove`). Pure/host-testable.

### B — Docker mount-mask (v1 enforcer) *(feature plan §4.1)*
For each masked path, append `-v <emptyFile|emptyDir>:/project/workspace/<rel>:ro` **after** the base
workspace mount so docker layers empties on top. Portable empty temp file + dir (**not `/dev/null`** —
Windows-host breaks). Covers **every** process (it changes the real mounted fs), whole-tree
deny-list, "present-but-empty" (`mask` mode). **Always-on floor enforcer** — applied even in
allow-list mode and even with bwrap off. Frozen host-side before `docker run`. Not sandboxing — the
container is still the boundary; do not describe it as a sandbox.

### C — Path-guard middleware *(design_doc §2, L229–246)*
In-process pre-flight on the file-tool backend (`_WorkspaceShellBackend` in `agent.py`):
`os.path.commonpath([realpath(target), realpath(base)]) == base` — **`commonpath`, not
`startswith`** (`startswith("/workspace")` also matches `/workspace-evil`, a sibling escape). Prefer
`O_NOFOLLOW` / resolve-and-hold-fd over re-derive-after-check to narrow the TOCTOU window.
**Defense-in-depth only** — the mask (and later bwrap bind-whitelist) is the real boundary; the guard
is a racy guard rail (design_doc §2 caveat), not the security claim. Pure/host-testable; regression
test the sibling-escape and traversal cases.

### D — `permission_denied` interrupt *(completes M3 S4)*
A mask/guard denial that occurs mid-turn escalates into the **existing HITL spine** as the
`permission_denied` system interrupt M3 recognized-but-didn't-enforce. Reuses `interrupt.py` /
`hitl.run_interrupt_loop`; the operator can approve a one-off exception (never for a designated-secret
floor path — that stays inviolable) or deny (halt, per `on_deny`). **Off-HITL** (no
`.harness-config.yaml`) the denial is a plain refused tool result, exactly as a blocked call is
today. Note the acyclic-import guard: the interrupt must originate from the guard/backend seam, not
from `cost.py` (same constraint that pushed `missing_price` to a separate reader — M3 S4).

### E — `harness doctor` *(support; §12.2)*
`python3 -m harness doctor` (via `cli.dispatch`, beside `sync-models`), pure/stdlib, keyless. M4 adds
`.agentignore`/floor coherence to the §12.2 checks (registry `default_model` resolves, `rate_table`
has pricing, `.mcp.json`/`hooks.json`/`workflow.md` parse): validate that the resolved policy is
well-formed, the designated-secret floor is present, and **no** allow-list/`!` entry targets a floor
path. Reuse the real resolver so validation can't drift from runtime. Collect-and-summarize, exit
non-zero on any error.

### F — CI pipeline *(support; §12.1)*
New `.github/workflows/ci.yml`, no source change: (1) host-tier `pytest` (stdlib modules), (2)
`docker build --target test` + full image-tier suite, (3) keyless smoke turn (exits 0 by design),
(4) script-parity lint (`scripts/*.ps1` ↔ `*.sh`). **Add the §10 security suite (G) to tiers 1–2** so
the boundary is checked on every push. Optional tiny `scripts/check-parity.{sh,ps1}`.

### G — Security verification suite *(support; §10)*
The tests that turn "boundary" into a *proven* boundary — the §10 slice this milestone finally makes
real. At minimum: designated-secret-floor-never-leaks (resolver + emitted mount set), no
path-traversal / sibling-directory escape (guard), mask-emission correctness from a scan list (both
shells), snapshot protection-reduction warning, and `mask_add` raises-only. Host-runnable/stdlib per
the suite conventions (`deepagent-image/CLAUDE.md` → Test suite layout). Every floor/guard behaviour
ships with a regression test ("every bug fix ships with a regression test").

### H — bwrap fs-tool jail *(stretch; feature plan §4.2)*
Wire `scripts/sandbox-exec.sh`; **route all fs-touching tools** (shell + deepagents read/write/edit/
ls/glob) through the agent's bwrap namespace so the same allow-list bind-whitelist gates both; binds
sourced from the resolved policy; designated secrets never bound; enforce the "all fs tools route
through the jail" invariant in `agent.py`. **Verify nested userns first**; do not claim sandboxing
until wired + verified. NetJail already established the "grant it or it silently breaks" discipline
for egress; the bind-whitelist is its filesystem analogue.

## 5. What we pull in — and leave out

Pulled in because the security work structurally needs them (mirrors M3 pulling in P1/P2):
- **§12.1 CI** — a security suite nobody runs is not a boundary; CI is the regression guarantee, and
  it protects every future milestone besides.
- **§12.2 `harness doctor`** — a bad allow-list is a silent hole; pre-flight validation is the config
  half of the boundary. M4 extends the §12.2 check set rather than owning all of it.
- **§10 security verification suite** — the first concrete slice of the design-only §10.

Deliberately **left out** (adjacent, not this milestone): §12.6 skills/memories, §12.7 `usage.jsonl`
sink + §8 telemetry-to-PR, §13 file-read middleware, §7 compression, §5 multi-agent funnel, §11
benchmarking, and the bwrap **stretch** if userns doesn't verify in the M4 window. The overlayfs view
and `hide` mode are explicitly deferred v2 (feature plan §4.3/§6).

## 6. Open forks to pin (feature plan §8)

- **Default on vs. off.** Recommend **on** (mask-by-default; secrets in a mounted repo are the common
  case) — but confirm, since it changes the removable-seam default.
- **`.agentignore` name** — confirm vs. `.workspaceignore`/`.mountignore`.
- **Config mode syntax** — deny-vs-allow switch + per-entry visibility mode directive (keep gitignore
  pattern lines clean; header directive, not per-line flags).
- **`permission_denied` approvable scope** — operator may grant a one-off exception for a
  *pattern-default* or general path, but **never** for a designated-secret floor path. Pin the UX.
- **Cross-platform** — keep `.ps1`/`.sh` pairs in sync (scan-list parsing, mask-mount emission);
  NetJail's `.txt` allowlists set the precedent for host-side declared policy.

## 7. Tests (host-runnable, stdlib)

Per feature plan §9 + the suite conventions: scanner gitignore parity (negation, `**`, nesting,
last-match-wins) + symlink canonicalization + mode classification; **designated-secret-floor
invariant** (a `!`/allow-list entry targeting a floor path must not expose it — regression test);
mask-mount emission from a scan list (both shells); snapshot/protection-reduction warning between
runs; path-guard sibling-escape + traversal + symlink-out refusal; `doctor` flags a floor-negation
misconfig and passes clean on the shipped config; `mask_add` raises-only. Graph-side
`permission_denied` suspend/resume is image-only (smoke), like the other HITL dispatch paths.

---

**Cross-refs:** `design_doc.md` §2 (visibility, path guard, secret provisioning), §9 (interrupt
spine), §10 (security verification), §12.1/§12.2; `docs/features/workspace_visibility.md` (authoritative
mechanics); `docs/milestones/complete/milestone3.md` §0/S4 (the `permission_denied` follow-up this
completes); `deepagent-image/CLAUDE.md` (Gotchas: NetJail allowlist discipline, shell-env allowlist,
test-suite layout).
