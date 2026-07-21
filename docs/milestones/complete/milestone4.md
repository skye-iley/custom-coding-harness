# Milestone 4 — Real Trust Boundary (Workspace Visibility + Path Guard)

> **Status:** ✅ Built (v1 — slices A–G). Stretch H (bwrap fs-tool jail) not yet built;
> deferred v2 (overlayfs view) not yet built. See PR1–PR5 for the shipped commits.
> Promotes the
> `design_doc.md` **§2 Workspace Visibility & Secret Masking** + **Path Guard** designs, backed by the
> **§10 security verification suite**, into a built, tested slice — and pulls in the two `design_doc.md`
> §12 operational items this work structurally needs (**§12.1 CI**, **§12.2 `harness doctor`**). The
> mechanics of *which paths the agent sees* are fully specified in
> **`docs/features/workspace_visibility.md`** — this milestone schedules the build, fixes scope
> (v1 vs. deferred), and wires the policy into the harness's existing seams (HITL, git lifecycle,
> config validation). Read §2 (design_doc) and the feature plan first.
>
> **This document is implementation-ready.** §0–§7 are the *what/why* (scope, slices, done-when).
> §8 onward is the *how* the next engineer needs before writing code: module layout (§8), data
> contracts (§9), the resolver algorithm (§10), enforcement wiring against the real seams (§11), the
> support tier (§12), config knobs + the removable contract (§13), the threat model (§14), integration
> gotchas incl. a git-pr staging hazard the earlier draft missed (§15), pinned fork decisions (§16),
> the PR breakdown (§17), and the test matrix (§18).

---

## 0. Planned slices

Ordered by leverage; the config resolver (A) is built once and feeds every enforcement layer, so
later layers slot in without reworking policy (feature plan §7).

| Slice | Scope | Source | State | PR (§17) |
|-------|-------|--------|-------|----------|
| **A** policy + resolver + scanner | `.agentignore` gitignore-parity matcher, 3-tier policy, designated-secret floor, pre-flight scan container | feature plan §3/§5 | v1 | PR1 |
| **B** docker mount-mask | deny-list empty-overlay mounts; always-on floor enforcer; git-pr staging exclusion | feature plan §4.1 | v1 | PR2 |
| **C** path-guard middleware | in-process `validate_path` (`commonpath`, not `startswith`) on the file-tool backend; defense-in-depth | design_doc §2 | v1 | PR3 |
| **D** `permission_denied` interrupt | a guard denial escalates into the HITL spine — **completes the M3 S4 follow-up** | design_doc §9 + M3 | v1 | PR3 |
| **E** `harness doctor` | pre-flight config validation (registry + `.agentignore` + floor coherence) | §12.2 | v1 (support) | PR4 |
| **F** CI pipeline | run host + image + **security** suites on push/PR; script-parity lint | §12.1 | v1 (support) | PR5 |
| **G** §10 security verification suite | tests that *prove* the boundary holds (floor never leaks, no traversal escape) | §10 | v1 (support) | ships in PR1–3 |
| **H** bwrap fs-tool jail | allow-list boundary; route **all** fs tools through `sandbox-exec`; verify nested userns | feature plan §4.2 | **stretch** | PR6 |
| **I** overlayfs view | tool-agnostic true allow-list + upper-diff write-back | feature plan §4.3 | deferred v2 | — |

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
  unchanged (removable-seam contract: `DEEPAGENTS_MASK=0` ⇒ behaviour identical to M3 — see §13).
- A path-guard traversal attempt (`../`, absolute, symlink-out) is refused, and — under HITL — surfaces
  as an audited `permission_denied` interrupt rather than a silent failure or crash.
- **git-pr never blanks a masked secret**: the resolved mask set is excluded from staging, so a
  masked-empty `.env` is not committed as an emptied file (§15.1).
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

## 4. Slices (intent)

Terse intent per slice; the implementation detail each one needs is in §8–§12, cross-referenced.

### A — Policy + resolver + scanner *(feature plan §3, §5 · detail §9, §10)*
One Python gitignore-parity matcher (`harness/mask.py`, vendored stdlib, no pip dep), run as a
**read-only pre-flight container** in the harness venv (`python3 -m harness mask-scan`), emitting a
flat resolved list (§9.3) the `.ps1`/`.sh` launchers parse — **no matcher logic reimplemented in
shell**. Three tiers (designated secrets / pattern defaults / general `.agentignore`), deny-list
default + strict allow-list mode. Authoritative config lives in the **state dir** (outside the mount,
agent-unreachable); in-workspace `.agentignore` is convenience/advisory and **snapshotted** each
launch to detect protection reduction. Canonicalize paths to kill symlink re-exposure. Append-only
`mask_add` agent tool (raise protection only; no `mask_remove`). Pure/host-testable.

### B — Docker mount-mask (v1 enforcer) *(feature plan §4.1 · detail §11.1, §15.1)*
For each masked path, append `-v <emptyFile|emptyDir>:/project/workspace/<rel>:ro` **after** the base
workspace mount so docker layers empties on top. Portable empty temp file + dir (**not `/dev/null`** —
Windows-host breaks). Covers **every** process (it changes the real mounted fs), whole-tree
deny-list, "present-but-empty" (`mask` mode). **Always-on floor enforcer** — applied even in
allow-list mode and even with bwrap off. Frozen host-side before `docker run`. **Also excludes the
mask set from git-pr staging** (§15.1) so a masked-empty file is never committed. Not sandboxing —
the container is still the boundary; do not describe it as a sandbox.

### C — Path-guard middleware *(design_doc §2, L229–246 · detail §11.2)*
In-process pre-flight on the file-tool backend (`_WorkspaceShellBackend._resolve_path` in
`agent.py`), extracted to a pure `harness/pathguard.py`:
`os.path.commonpath([realpath(target), realpath(base)]) == realpath(base)` — **`commonpath`, not
`startswith`** (`startswith("/workspace")` also matches `/workspace-evil`, a sibling escape). Applied
**after** the parent's virtual-mode resolution, so it also catches an in-workspace symlink whose
target escapes. Prefer `O_NOFOLLOW` / resolve-and-hold-fd over re-derive-after-check to narrow the
TOCTOU window. **Defense-in-depth only** — the mask (and later bwrap bind-whitelist) is the real
boundary; the guard is a racy guard rail (design_doc §2 caveat), not the security claim. Covers the
**file** tools only (read/write/edit/ls/glob); the shell tool runs arbitrary commands and is bounded
by the container root + the docker mask, not this guard. Pure/host-testable.

### D — `permission_denied` interrupt *(completes M3 S4 · detail §11.3)*
A path-guard denial that occurs mid-turn escalates into the **existing HITL spine** as the
`permission_denied` system interrupt M3 recognized-but-didn't-enforce. Raised from the same in-tool
position `ask_human` (S3) already proves works — a `GraphInterrupt` from inside a tool node
propagates to `hitl.run_interrupt_loop`. Reuses `interrupt.py` / `hitl.run_interrupt_loop`; audited
(S7). The operator may approve a one-off exception **only** for an in-bounds, non-floor path; a
traversal-out-of-workspace or a designated-secret floor path is **never** approvable (fail-closed
deny). **Off-HITL** (no `.harness-config.yaml`) the denial is a plain refused tool result (a
`PermissionError` surfaced as the tool's error result), exactly as a blocked call is today. Acyclic
import: the interrupt originates from the guard/backend seam, which may import `interrupt` (imports no
harness sibling); it must **not** originate from `cost.py` (same constraint that pushed `missing_price`
to a separate reader — M3 S4).

### E — `harness doctor` *(support; §12.2 · detail §12.1)*
`python3 -m harness doctor` (via `cli.dispatch`, beside `sync-models`), pure/stdlib, keyless. M4 adds
`.agentignore`/floor coherence to the §12.2 checks (registry `default_model` resolves, `rate_table`
has pricing, `.mcp.json`/`hooks.json`/`workflow.md` parse): validate that the resolved policy is
well-formed, the designated-secret floor is present, and **no** allow-list/`!` entry targets a floor
path. Reuse the real resolver (`mask.resolve`) so validation can't drift from runtime. Collect-and-
summarize, exit non-zero on any error.

### F — CI pipeline *(support; §12.1 · detail §12.2)*
New `.github/workflows/ci.yml`, no source change: (1) host-tier `pytest` (stdlib modules), (2)
`docker build --target test` + full image-tier suite, (3) keyless smoke turn (exits 0 by design),
(4) script-parity lint (`scripts/*.ps1` ↔ `*.sh`). **Add the §10 security suite (G) to tiers 1–2** so
the boundary is checked on every push. Optional tiny `scripts/check-parity.{sh,ps1}`.

### G — Security verification suite *(support; §10 · detail §18)*
The tests that turn "boundary" into a *proven* boundary — the §10 slice this milestone finally makes
real. Ships **inside** the slice PRs it verifies (§17), not as a separate PR. Host-runnable/stdlib per
the suite conventions (`deepagent-image/CLAUDE.md` → Test suite layout). Every floor/guard behaviour
ships with a regression test ("every bug fix ships with a regression test").

### H — bwrap fs-tool jail *(stretch; feature plan §4.2)*
Wire `scripts/sandbox-exec.sh`; **route all fs-touching tools** (shell + deepagents read/write/edit/
ls/glob) through the agent's bwrap namespace so the same allow-list bind-whitelist gates both; binds
sourced from the resolved policy; designated secrets never bound; enforce the "all fs tools route
through the jail" invariant in `agent.py`. **Verify nested userns first** (`bwrap --unshare-all true`
must actually run in the built image, design_doc §2 ~L87–91); do not claim sandboxing until wired +
verified. NetJail already established the "grant it or it silently breaks" discipline for egress; the
bind-whitelist is its filesystem analogue.

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

## 6. Open forks — see §16

The feature plan §8 open forks are **pinned to decisions** in §16 (default on/off, config-file name,
mode syntax, `permission_denied` approvable scope, cross-platform parity). Read §16 before coding.

## 7. Tests — see §18

The full test matrix (file → cases → tier) is §18. Headline: scanner gitignore parity, the
designated-secret-floor invariant, mask-mount emission (both shells), the snapshot/protection-reduction
warning, path-guard sibling-escape + traversal + symlink-out refusal, the git-pr exclusion, `doctor`
misconfig detection, and `mask_add` raises-only — all host-runnable/stdlib. Graph-side
`permission_denied` suspend/resume is image-only (smoke), like the other HITL dispatch paths.

---

# Implementation reference

## 8. Module & file layout

New and touched files. All new Python lives in the **harness venv** stack (`/opt/venv`, stdlib only,
no pip dep — the two-stack rule), so every new module is host-runnable and joins the stdlib test tier.

**New modules**
- `deepagent-image/project/harness/mask.py` — the resolver core (slice A). Pure stdlib: the
  gitignore-parity matcher (§10), the 3-tier policy, the designated-secret floor, canonicalization,
  and `resolve(workspace, state_dir, mode) -> MaskResult`. Imports **no** harness sibling (mirrors
  `archive.py`/`cost.py` acyclic discipline), so `doctor` and the scan CLI both reuse it without a
  cycle.
- `deepagent-image/project/harness/mask_scan.py` — the thin CLI wrapper invoked as
  `python3 -m harness mask-scan` via `cli.dispatch` (canonical, consistent with `-m harness
  sync-models`) **and** runnable directly as `python3 -m harness.mask_scan`. Calls `mask.resolve`,
  prints the §9.3 stdout grammar, writes the snapshot (§9.2), warns on protection reduction. No matcher
  logic here.
- `deepagent-image/project/harness/pathguard.py` — pure `validate_path(target, base)` (slice C, §11.2).
  Imports only `os`/`pathlib` + `interrupt` lazily. Host-testable in isolation.
- `deepagent-image/project/harness/doctor.py` — the `harness doctor` checks (slice E, §12.1). Pure,
  stdlib, reuses `providers._load_providers`, `loaders.*`, the workflow manifest parser, and
  `mask.resolve`.
- `.github/workflows/ci.yml` — CI (slice F, §12.2). No source change.
- `deepagent-image/scripts/check-parity.{sh,ps1}` — optional `.ps1`↔`.sh` drift check (slice F).

**New tests** (host-runnable unless noted)
- `tests/test_mask.py` — resolver parity, floor invariant, emission grammar, snapshot diff.
- `tests/test_pathguard.py` — sibling-escape, traversal, symlink-out, in-bounds pass.
- `tests/test_doctor.py` — floor-negation misconfig fails; clean registry passes; keyless.
- additions to `tests/test_cli.py` (dispatch of `mask-scan`/`doctor`; git-pr exclusion wiring),
  `tests/test_agent.py` (backend guard wiring — image-only), `tests/test_hitl.py`/`test_interrupt.py`
  (`permission_denied` request shape).

**Touched, existing**
- `harness/cli.py` — `dispatch()`: add `mask-scan` and `doctor` subcommands beside `sync-models`
  (§8, `cli.py:1046`).
- `harness/agent.py` — wire `pathguard.validate_path` into `_WorkspaceShellBackend._resolve_path`
  (`agent.py:193`); register the `mask_add` tool beside `recall_past`/`refresh_workspace`
  (`agent.py:202`+); thread the HITL denial callback into `build_agent` (§11.3).
- `scripts/run-docker.ps1` + `scripts/run-docker.sh` — pre-flight scan container + mask-mount emission
  (§11.1), kept in sync.
- `scripts/smoke.{ps1,sh}` — a masked-secret assertion in the keyless smoke turn.
- `workflows/git-pr/` (the step script) — read the resolved mask set from the state dir and add it to
  the staging exclude pathspec (§15.1).
- `deepagent-image/project/.env.example` — document `DEEPAGENTS_MASK` / `DEEPAGENTS_MASK_MODE` (§13).
- `deepagent-image/CLAUDE.md` + `docs/features/workspace_visibility.md` — mark built; note the git-pr
  exclusion and the removable contract.

## 9. Data contracts

### 9.1 `.agentignore` format

Full **gitignore parity** for the pattern lines (`**`, `!` negation, trailing-slash dirs, leading-`/`
anchor, last-match-wins, per-directory nesting), plus a small **header directive block** for the two
things gitignore has no syntax for — deny-vs-allow mode and per-entry visibility mode. Keep the
directives in a header so pattern lines stay clean (feature plan §6; pinned in §16):

```
# .agentignore  — header directives first, then gitignore-style patterns
#!mode: deny            # deny (default) | allow
#!visibility: mask      # mask (default, present-but-empty) | hide (deferred v2 — rejected in v1)

# --- pattern lines (gitignore semantics, relative to THIS file's dir) ---
.env
.env.*
*.pem
secrets/
!secrets/README.md      # negation allowed for pattern-default/general tiers…
```

- A `#!key: value` line in the header sets a directive. An ordinary `#` line is a comment. Directives
  must precede the first pattern; a directive after a pattern is a `SystemExit` (loud, like a bad
  workflow manifest).
- Unknown directive key, or `visibility: hide` in v1, fails loud (`hide` is deferred v2).
- Recursive discovery: every `.agentignore` at any depth is collected; each file's patterns resolve
  **relative to that file's own directory** (gitignore semantics). Root + nested compose,
  last-match-wins across the composed set.

### 9.2 State-dir layout (authoritative config + snapshot)

The state dir is `archive.state_dir(workspace)` — `/project/state` under `run-docker` (mounted from
`project/state/<hash>/`, `DEEPAGENTS_STATE_DIR`), or `<workspace>/.deepagents` bare. It already holds
`checkpoints.sqlite` / `past.sqlite` / `session.env`, and is **outside the workspace mount** so the
agent's file/shell tools (rooted at the workspace) cannot read or edit it. M4 adds:

- `<state>/agentignore` — the **authoritative** config, incl. the **designated-secret floor** block.
  Agent-unreachable. `mask_add(path)` appends here (raise-only). Same `#!floor:` marker tier as below.
- `<state>/mask-snapshot.txt` — the **resolved mask set** from the last launch (sorted `<tier> <rel>`
  lines), written by `mask-scan`. Serves two jobs: protection-reduction detection (§10 step 6) and the
  git-pr staging exclusion (§15.1). Runtime state — lives in the state dir, never committed.

The floor tier is expressed in `<state>/agentignore` with a `#!floor:` directive block whose entries
are exact paths/globs the resolver treats as tier-1 (never negatable):

```
#!floor:
id_rsa
.aws/credentials
```

### 9.3 `mask-scan` stdout grammar

One line per masked node, whitespace-separated, **stable and shell-greppable** (the launchers parse it
on the host with no TOML/JSON dependency — same constraint as the NetJail `.txt` allowlists):

```
<mode> <type> <tier> <relpath>
```

- `mode` ∈ `mask` (v1; `hide` is deferred v2 and never emitted).
- `type` ∈ `file` | `dir` — selects the empty **file** vs empty **dir** overlay source (§11.1).
- `tier` ∈ `floor` | `default` | `user` — provenance for logging, the snapshot, and `doctor`. The
  launcher ignores it for `-v` emission; it is not optional (fixed 4-column contract).
- `relpath` — workspace-relative POSIX path, no leading `/`, no `..`, canonicalized. May contain
  spaces? **No** — the resolver rejects/curates so the field is the trailing token; if a real path
  contains a space it is percent-escaped (`%20`) and the launcher unescapes. (Keeps the grammar
  single-token-tail and greppable.)

Example:

```
mask file  floor   .aws/credentials
mask file  default .env
mask dir   user    vendor/private
```

Emission is **minimized**: mask the shallowest masked node. A whole masked dir with **no** negated
(visible) descendant emits one `dir` line; a masked dir that contains an `!`-negated visible
descendant cannot be whole-tree-emptied (docker overlay is all-or-nothing), so the resolver emits the
individual masked `file` lines under it instead. Floor entries never have negation, so floor dirs emit
whole. (Mount-count is O(masked leaves) in the worst case — acceptable for v1; bwrap/overlayfs (H/I)
remove the per-path mount.)

### 9.4 Headless JSON + interrupt additions

- The `permission_denied` interrupt reuses the existing `InterruptRequest` (interrupt.py) unchanged —
  `kind=KIND_APPROVE` (approve a one-off) or a fail-closed `default`-deny, `source=SOURCE_SYSTEM`,
  `meta={"path": <rel>, "tier": <tier>, "op": "read|write|..."}`. No new schema.
- Audit (S7) records it via `audit.record_interrupt` like every other interrupt — **path in `meta`,
  never the file contents** (audit already strips the context payload).
- No change to `run_batch`'s stdout JSON shape; a headless run resolves the interrupt by the §6
  fail-closed policy (a floor/out-of-bounds denial has `timeout_policy` → deny/abort).

## 10. Resolver algorithm (`mask.py`)

Pure stdlib, no pip dep. `resolve(workspace: Path, state_dir: Path, mode: str) -> MaskResult` where
`MaskResult = {masked: list[MaskEntry], warnings: list[str], mode: str}` and
`MaskEntry = {relpath, type, tier}`.

1. **Assemble the ordered rule set.** In precedence order (later overrides earlier for last-match-wins,
   except the floor which is absolute):
   1. shipped **pattern-default** globs (hardcoded in `mask.py`, feature plan §2):
      `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.ssh/`, `.aws/credentials`,
      `.netrc`, `.npmrc`, `.git-credentials`, `credentials.json`, `*.p12`, `*.pfx`.
   2. `<state>/agentignore` general patterns (authoritative, but negatable within non-floor tiers).
   3. every in-workspace `.agentignore`, root→leaf, each relative to its own dir.
   4. the **designated-secret floor** (`#!floor:` in `<state>/agentignore`) — applied **last and
      absolute**.
2. **Compile each pattern** into a matcher (§10 sub-algorithm below). Track `negated` (`!` prefix),
   `dir_only` (trailing `/`), `anchored` (leading `/`), and the pattern's base dir.
3. **Walk the workspace** with `os.walk` (follow no symlinks). For each entry compute the
   last-matching rule → masked/visible. In **deny mode** default visible; in **allow mode** default
   masked, and only entries under a listed allow base (plus the agent-writable area) flip visible.
4. **Canonicalize** every candidate with `os.path.realpath`. If the real target escapes the workspace
   (a symlink out) → mask it (deny) regardless of name, and record a warning. If a symlink's real
   target is a floor path, the symlink is masked too (no floor re-exposure via an alias).
5. **Enforce the floor.** Any `!`-negation or allow-list entry whose match set intersects a floor path
   is **dropped** and a warning is appended (`"ignored negation/allow of floor path <p>"`). The floor
   masks unconditionally.
6. **Snapshot + protection-reduction check.** Sort the resolved `<tier> <rel>` set; compare to
   `<state>/mask-snapshot.txt` from the prior launch. Any path in the prior snapshot **absent** from
   the new set = protection reduced → a loud warning
   (`"protection reduced — N path(s) no longer masked: …"`). Off-HITL this prints to stderr and the run
   continues (design choice: warn, don't block, so a legitimate policy relaxation isn't a hard stop);
   under HITL it can be surfaced as an `approve` interrupt (fork pinned open in §16). Then overwrite
   the snapshot with the new set.
7. **Minimize + emit** per §9.3.

**Gitignore matcher (stdlib, ~vendored gitwildmatch).** No `pathspec` pip dep. Translate each pattern
to a compiled `re` once: `**` → match across `/`; `*` → match within a segment; `?` → one non-`/`
char; char classes pass through; a trailing `/` marks dir-only; a leading `/` anchors to the base dir,
otherwise the pattern may match at any depth below its base. Last-match-wins is the ordered scan in
step 3. Unit-tested against the gitignore edge cases in `tests/test_mask.py` (§18) — negation,
`**`, nesting, anchored vs floating, dir-only, last-match-wins.

## 11. Enforcement wiring

### 11.1 Docker mount-mask (slice B) — `scripts/run-docker.{ps1,sh}`

**Pre-flight scan.** After the workspace + state host dirs are resolved (`run-docker.ps1:60`/`:72`) and
before the main `docker run` (`:277`), and only when the feature is on (§13, `DEEPAGENTS_MASK`≠`0`),
run a throwaway read-only container:

```
docker run --rm \
  -v <MountWorkspace>:/project/workspace:ro \
  -v <StateHostDir>:/project/state:ro \
  -e DEEPAGENTS_STATE_DIR=/project/state \
  deepagent-harness python3 -m harness mask-scan
```

Mount `MountWorkspace` (the ephemeral copy when `-Ephemeral`, else the real workspace) so the mask
matches what the agent will actually see. Capture stdout (the §9.3 lines). ~1s added pre-flight cost.

**Empty overlay sources.** Create **one** empty temp file and **one** empty temp dir on the host, reuse
per hit. **Not `/dev/null`** (Linux-host-only; breaks Docker Desktop on Windows).
- `.ps1`: `$emptyFile = New-TemporaryFile`; `$emptyDir = New-Item -ItemType Directory` under
  `[System.IO.Path]::GetTempPath()`.
- `.sh`: `emptyFile="$(mktemp)"`; `emptyDir="$(mktemp -d)"`.

**Emit `-v` args, after the workspace mount.** For each scan line, append
`-v <emptyFile|emptyDir>:/project/workspace/<relpath>:ro` to `$dockerArgs` **after**
`-v ${MountWorkspace}:/project/workspace` (`run-docker.ps1:287`) so docker layers the empties on top.
`type=file` → `emptyFile`; `type=dir` → `emptyDir`. Unescape `%20` in `relpath` for the mount target.
Windows: the empty temp paths are host Windows paths; Docker Desktop accepts them as `-v` sources.

**Always-on floor.** Floor lines are emitted even in `allow` mode and even with bwrap off (§14
redundancy tier 1). Frozen host-side — computed once before `docker run`, never re-scanned mid-session.

Clean up the temp file/dir in the launcher's `finally` block (beside the ephemeral cleanup,
`run-docker.ps1:324`).

### 11.2 Path guard (slice C) — `harness/pathguard.py` + `agent.py`

`pathguard.validate_path(target: str, base: str) -> str` implements the design_doc §2 snippet:
`realpath` both, `os.path.commonpath([abs_target, abs_base]) == abs_base`, else raise
`PathGuardDenied(target)` (a `PermissionError` subclass carrying the offending relpath for the audit
`meta`). Prefer `O_NOFOLLOW`/hold-fd on the actual open where the backend later reads/writes, to
shrink the TOCTOU window (design_doc §2 caveat) — but the pre-flight check is the v1 deliverable.

Wire it into `_WorkspaceShellBackend._resolve_path` (`agent.py:193`) **after** the existing
virtual-mode de-nesting and the `super()._resolve_path(key)` call — guard the *resolved* Path, so an
in-workspace symlink whose target escapes is caught (the resolved path is realpath-compared). `base` =
the backend's `root_dir` (the workspace). This covers the deepagents file tools (read/write/edit/
ls/glob) that funnel through `_resolve_path`. The **shell** tool does not; it is bounded by the
container root + the docker mask (§14) — state this in the module docstring so no one assumes the guard
sandboxes the shell.

Removable-contract note: the guard is **always-on** (it only ever refuses a genuine escape, which is
never a legitimate file op — a real path under the workspace resolves to `commonpath == base` and
passes), so it does not violate the §13 removable seam. Only the *interrupt escalation* (D) is
HITL-gated.

### 11.3 `permission_denied` interrupt (slice D) — `agent.py` + `hitl.py`

The denial must become a `GraphInterrupt` when HITL is on, or a plain refused tool result when it is
off. The seam:

- `build_agent` (`agent.py:328`) gains an optional `on_path_denied` callback (default `None`). `cli.main`
  passes a callback **only when `hitl_conf is not None`** and the `permission_denied` system interrupt
  is enabled (`hitl_conf.system_interrupt_enabled("permission_denied")`). The callback closes over the
  request-builder — no `cost.py`-style cycle, because `agent.py` already imports freely and `interrupt`
  imports no harness sibling.
- `_WorkspaceShellBackend` catches `PathGuardDenied` from `pathguard.validate_path`. If `on_path_denied`
  is set and the path is **in-bounds-but-masked / non-floor**, it calls it → the callback raises
  `interrupt.raise_interrupt(new_request(KIND_APPROVE, prompt, default=False, source=SOURCE_SYSTEM,
  meta={...}))`. This runs **inside the tool node**, exactly where `ask_human` (S3,
  `hitl.make_ask_human_tool`) already raises `interrupt()` successfully, so the `GraphInterrupt`
  propagates to `hitl.run_interrupt_loop` (drained in `run_turn`, `cli.py:507`). On **approve** →
  return the resolved path (allow once); on **deny** → re-raise `PathGuardDenied` (→ tool error
  result). Audit both via `audit.record_interrupt`.
- **Never approvable:** a designated-secret **floor** path, or a path whose realpath is **outside** the
  workspace (a true escape). Those raise `PathGuardDenied` straight through (no interrupt offer) — the
  operator cannot grant an escape. `default=False` also fail-closes the headless/non-TTY path (§6).
- **Off-HITL:** `on_path_denied is None` → the backend re-raises `PathGuardDenied` → deepagents returns
  it as the tool's error result. Byte-for-byte a blocked call today.

**v1 honesty:** with the docker mask, an in-workspace masked file simply reads **empty** — there is no
per-read denial to intercept. So in v1 the `permission_denied` interrupt fires primarily on
**path-guard traversal denials** (a visible, audited, fail-closed event). The full "approve a one-off
read of a masked pattern/general path" UX lands with the bwrap file-tool jail (H), where each denied
read is explicit. D's v1 deliverable is: the interrupt is wired, audited, floor-safe, and fail-closed;
the approvable-exception path exists for in-bounds non-floor denials.

## 12. Support tier

### 12.1 `harness doctor` (slice E) — `harness/doctor.py`

`dispatch(argv)` (`cli.py:1046`) grows `if argv and argv[0] == "doctor": return doctor.doctor_main(argv[1:])`.
`doctor_main` runs all checks, collecting `(level, message)` records, prints a summary, returns non-zero
if any `error`. Keyless, stdlib, reuses the real loaders so validation can't drift:

- **Registry** (§12.2 design): every `provider.toml` parses; each non-null `default_model` resolves to
  a real `models/<model>.toml`; `rate_table` providers have `[pricing]`/`[pricing.estimate]` with
  `priced_as_of`; flag stale `priced_as_of`. Reuse `providers._load_providers`.
- **Credentials:** report which providers have a key / `*_BASE_URL` set — **names only, no values**
  (secret-hygiene).
- **Optional config:** `.mcp.json`, `hooks.json` parse; every `workflows/<name>/workflow.md` passes the
  same manifest parser used at load. Reuse `loaders.*` + the workflow parser.
- **Mask / floor (new, M4):** run `mask.resolve` against the workspace + state dir; assert the resolved
  policy is well-formed (no parse error), the designated-secret **floor is present** (the shipped
  defaults or a `#!floor:` block resolve to ≥1 masked path), and **no** `!`/allow-list entry targets a
  floor path (surface `mask.resolve`'s warnings as **errors** here). A deliberately weakened floor →
  non-zero exit → CI red (§1 done-when).

`scripts/verify.{ps1,sh}` optionally call `doctor` after the import check.

### 12.2 CI pipeline (slice F) — `.github/workflows/ci.yml`

On `push` + `pull_request`. Sketch:

```yaml
name: ci
on: [push, pull_request]
jobs:
  host-tests:            # stdlib tier — no Docker, fast
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pytest
      - run: python -m pytest tests/          # from deepagent-image/project
        working-directory: deepagent-image/project
        # importorskip guards skip the image-only modules cleanly off-image;
        # test_mask / test_pathguard / test_doctor run here (stdlib tier).
  image-tests:           # full suite in the real runtime layer
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build --target test -t deepagent-harness:test deepagent-image
      - run: docker run --rm deepagent-harness:test python3 -m pytest tests/
  smoke:                 # keyless smoke turn — exits 0 by design
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build --target runtime -t deepagent-harness deepagent-image
      - run: deepagent-image/scripts/smoke.sh   # keyless; git workflows are safe no-ops
  parity:                # .ps1 ↔ .sh drift
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: deepagent-image/scripts/check-parity.sh
```

The security suite (G) rides the `host-tests` + `image-tests` jobs (it *is* `test_mask` /
`test_pathguard` / `test_doctor` + the emission/exclusion cases). A deliberately weakened floor fails
`host-tests`; a broken cost calc fails as today; a drifted script pair fails `parity`. **Grant CI no
provider keys** — every tier is keyless by construction.

## 13. Config knobs & the removable contract

| Knob | Where | Default | Effect |
|------|-------|---------|--------|
| `DEEPAGENTS_MASK` | container env (`.env`) | `1` (on) | `0` disables scan + mask + floor + snapshot — the removable seam. |
| `DEEPAGENTS_MASK_MODE` | container env | `deny` | `deny` (see all but masked) \| `allow` (see only allow-listed + writable). Overridable per-workspace by the `#!mode:` header directive; env wins if set. |
| `DEEPAGENTS_AGENTIGNORE` | container env | unset | Override the in-workspace config filename (default `.agentignore`); state-dir authoritative config is unaffected. |

**Removable contract (mirrors M1 §2.5 / M2 / M3).** The single intentional default-behaviour change is
**mask-on-by-default** — like M2 changing the default `--thread-id` from `"default"` to a fresh
`session-<ts>`, this is an announced, safe default, not a silent one (secrets in a mounted repo are the
common case; §16 fork 1). The escape hatch is explicit: **`DEEPAGENTS_MASK=0`** disables the scan, the
mask mounts, the floor, and the snapshot entirely → the harness is byte-for-byte Milestone 3. The path
guard (§11.2) stays on because it has no false positives on legitimate file ops, and the
`permission_denied` interrupt stays HITL-gated as before. So: `DEEPAGENTS_MASK=0` **+** no
`.harness-config.yaml` ⇒ exactly M3.

## 14. Threat model — what holds, what doesn't

**Holds (v1):**
- A **designated-secret floor path** reads empty to *every* process in the container (the docker mask
  changes the real mounted fs), and no `.agentignore` negation, allow-list entry, or `mask_add` can
  expose it. Enforced redundantly: (1) docker mask always emits it; (2) the resolver drops any negation
  of it; (3) the file backend explicitly refuses it (belt-and-suspenders); (4) bwrap never binds it (H).
- A **pattern-default / general masked path** reads empty to every process (same mechanism), whole-tree
  deny-list, "present-but-empty".
- A **path-guard traversal** (`../`, absolute, in-workspace symlink whose target escapes) is refused for
  the **file** tools, and — under HITL — surfaced as an audited, fail-closed `permission_denied`
  interrupt.
- git-pr cannot silently **blank a masked secret** into a commit (§15.1).

**Does NOT hold (be honest — do not describe as sandboxing):**
- The docker mask is **not a sandbox**; the trust boundary is still the container (`mvp.md` §5). Masked
  files are **present-but-empty**, not absent (`mask` mode; true absence is `hide`, deferred v2).
- The **path guard is racy** (TOCTOU: a symlink swapped between `realpath` and the open). It is
  defense-in-depth, not the boundary. The bwrap bind-whitelist (H) is the real allow-list boundary.
- The path guard covers the **file tools only**. The **shell** tool runs arbitrary commands; a masked
  file it `cat`s reads empty (mask covers it), but shell path traversal is bounded by the container
  root, not the guard, until bwrap (H) routes the shell through the jail.
- An **allow-list** ("see only these dirs") is a bwrap/overlayfs capability; the v1 docker layer can
  only deny-list. `allow` mode in v1 is enforced by masking everything not listed — correct, but
  whole-tree present-but-empty, not true absence.

## 15. Integration points & gotchas

### 15.1 git-pr staging exclusion (**required — earlier draft missed this**)

A masked file reads **empty inside the container**. The `git-pr` workflow (`session.end`) stages with
`git add` **inside the container**, so a naïve `git add -A` would stage the *emptied* `.env` and the PR
would **blank the user's secret file** on the branch. This is a data/secret-handling hazard, not a
cosmetic diff. **Fix:** `git-pr` already excludes `.deepagents`/`.agent_telemetry`; extend its staging
pathspec to also exclude the resolved mask set. Source of truth: `<state>/mask-snapshot.txt` (§9.2),
readable by the step (state dir is `/project/state`, reachable by workflow steps via the full env).
Add `:(exclude)<rel>` pathspecs (or `git add` with an exclude list) for every masked `relpath`. Test:
a workspace with a masked `.env` → after a git-pr dry-run, `.env` is **not** staged and origin's
version is untouched. (`tests/test_workflows.py` / smoke.)

### 15.2 Ephemeral workspace
`-Ephemeral` mounts a throwaway **copy**; the mask must scan and overlay the **copy** (`MountWorkspace`),
not the real workspace — §11.1 already uses `MountWorkspace`. The state-dir floor config is keyed to the
**real** workspace path (persistent across ephemeral runs), so the floor is stable. `refresh_workspace`
pulling live host edits mid-run cannot unmask (mask is frozen at launch).

### 15.3 NetJail
The scan pre-flight container needs **no network** (`--internal`-safe; it only reads the mounted fs), so
it runs fine under `NET_JAIL=1`. No allowlist entry required. State this so no one adds a needless
domain.

### 15.4 Two-stack rule
The scanner/resolver/doctor run in the **harness venv** (`/opt/venv`), stdlib only, never the workspace
conda env. The scan container uses the harness image's `python3 -m harness …`, not the workspace
interpreter. No pip dep (`pathspec` is re-implemented, §10) so the host test tier stays dependency-free.

### 15.5 Frozen-at-launch vs. `mask_add`
The mask is computed host-side once, before `docker run`; agent runtime edits to any in-workspace
`.agentignore` **cannot unmask the current session**. `mask_add(path)` writes the **state-dir**
authoritative config (raise-only, no `mask_remove`) and takes effect **next run**. Register it in
`agent.py` beside `recall_past`/`refresh_workspace`, gated on the feature being on. It writes through a
`mask.append_floor`/`mask.append_deny` helper so the write path is testable without the tool decorator.

## 16. Open forks — pinned decisions

1. **Default on vs. off → ON.** Mask-by-default (floor + pattern-defaults). Escape hatch
   `DEEPAGENTS_MASK=0` (§13). Announced default change, precedent M2's thread-id default. **Decided.**
2. **Config-file name → `.agentignore`.** Matches the emerging convention (`.aiexclude`/`.cursorignore`/
   `.aiignore`/`.codeiumignore`) and reads beside `.gitignore`. Overridable via `DEEPAGENTS_AGENTIGNORE`
   for the in-workspace file. **Decided.**
3. **Mode syntax → header directives** (`#!mode:`, `#!visibility:`, `#!floor:`), not per-line flags, so
   gitignore pattern lines stay clean (§9.1). Env (`DEEPAGENTS_MASK_MODE`) wins over the header when set.
   **Decided.**
4. **`permission_denied` approvable scope → in-bounds non-floor only.** Operator may grant a one-off for
   a pattern-default/general path that resolves inside the workspace; **never** for a designated-secret
   floor path or a traversal-out-of-workspace (those hard-deny, no offer). Headless fail-closes
   (`default=False`). **Decided** (§11.3).
5. **Cross-platform → `.ps1`/`.sh` parity enforced by CI** (`check-parity`, slice F). Scan-list parsing
   + mask-mount emission must match; NetJail's `.txt` allowlists are the precedent for host-side declared
   policy. **Decided.**
6. **Still open (small):** whether the protection-*reduction* warning (§10 step 6) becomes a **blocking**
   `approve` interrupt under HITL, or stays a loud non-blocking stderr warning in all modes. Recommend
   **non-blocking warning** for v1 (a legitimate policy relaxation shouldn't wedge a session); revisit if
   tampering is observed. Confirm before coding slice A.

## 17. PR plan / sequencing

Ship the floor first; harden after. Each PR is independently reviewable and leaves the tree green.

- **PR1 — slice A (resolver + scanner):** `mask.py` + `mask_scan.py` + `cli.dispatch` `mask-scan` +
  `tests/test_mask.py`. No enforcement yet — the scan just prints. Pure/host-testable; lands the whole
  resolver + floor invariant + snapshot logic behind tests.
- **PR2 — slice B (docker mask + git-pr exclusion):** `run-docker.{ps1,sh}` scan+emit, empty overlay
  sources, `DEEPAGENTS_MASK`/`_MODE` knobs, the git-pr staging exclusion (§15.1), smoke assertion.
  First real secret-hiding across all tools.
- **PR3 — slices C + D (path guard + interrupt):** `pathguard.py` + backend wiring +
  `tests/test_pathguard.py`; the `permission_denied` interrupt seam + `test_hitl`/`test_interrupt`
  additions; `mask_add` tool. Completes the M3 S4 follow-up.
- **PR4 — slice E (`harness doctor`):** `doctor.py` + dispatch + `tests/test_doctor.py`; `verify` hook.
- **PR5 — slice F (CI):** `.github/workflows/ci.yml` + `check-parity.{sh,ps1}`. Turns G's tests into a
  gate.
- **PR6 — slice H (bwrap, stretch):** only after PR1–5 are green **and** `bwrap --unshare-all true`
  verifies in the built image. If userns doesn't verify in the M4 window, H slips to a follow-up and M4
  ships A–G.

## 18. Test matrix (host-runnable/stdlib unless noted)

| File | Cases | Tier |
|------|-------|------|
| `tests/test_mask.py` | gitignore parity (negation, `**`, nesting, anchored vs floating, dir-only, last-match-wins); symlink canonicalization + symlink-out masking; mode classification (deny/allow); **floor invariant** (a `!`/allow entry targeting a floor path does **not** expose it — regression); emission grammar + minimization (whole-dir vs per-leaf under a negated descendant); snapshot diff / protection-reduction warning; `mask_add` raise-only (append helper) | host |
| `tests/test_pathguard.py` | in-bounds pass; `../` traversal refuse; absolute-path refuse; **sibling escape** (`/workspace-evil` vs `/workspace` — `commonpath` not `startswith`); in-workspace symlink-out refuse | host |
| `tests/test_doctor.py` | floor-negation misconfig → non-zero; dangling `default_model` → non-zero; `rate_table` missing pricing → non-zero; clean shipped registry → zero; keyless (no keys/network) | host |
| `tests/test_cli.py` (add) | `dispatch("mask-scan")` / `dispatch("doctor")` route correctly; git-pr exclusion wiring reads the snapshot | host |
| `tests/test_workflows.py` (add) | git-pr staging **excludes** the mask set — a masked `.env` is not staged (§15.1) | host (`sh`-gated) |
| `tests/test_agent.py` (add) | `_WorkspaceShellBackend._resolve_path` refuses an escape; `on_path_denied=None` → `PathGuardDenied` as tool error; the guard passes a legit in-workspace path | image-only (`importorskip`) |
| `tests/test_hitl.py` / `test_interrupt.py` (add) | `permission_denied` request shape (`meta` path/tier, `default=False`); floor path never offers approve; headless fail-closes | host |
| smoke (`scripts/smoke.{ps1,sh}`) | keyless turn: a seeded `.env` in the workspace reads empty to the agent; graph-side `permission_denied` suspend/resume | image (smoke) |

Conventions per `deepagent-image/CLAUDE.md` → "Test suite layout": no keys/network/real model calls;
all writes to `tmp_path`; every floor/guard behaviour ships with a regression test.

---

**Cross-refs:** `design_doc.md` §2 (visibility, path guard L229–246, secret provisioning), §9 (interrupt
spine), §10 (security verification), §12.1/§12.2; `docs/features/workspace_visibility.md` (authoritative
mechanics); `docs/milestones/complete/milestone3.md` §0/S4 (the `permission_denied` follow-up this
completes); `deepagent-image/CLAUDE.md` (Gotchas: NetJail allowlist discipline, shell-env allowlist,
git session lifecycle, test-suite layout); code seams — `harness/agent.py:193` (`_resolve_path`),
`:328` (`build_agent`), `harness/cli.py:1046` (`dispatch`), `:507` (interrupt loop),
`harness/interrupt.py` (`new_request`/`raise_interrupt`), `harness/config.py:36`
(`SYSTEM_INTERRUPT_KEYS`), `scripts/run-docker.ps1:287` (workspace mount site).
