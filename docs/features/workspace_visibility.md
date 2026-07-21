# Workspace Visibility & Secret Masking — Feature Plan

> **Status:** ✅ Built (v1) — implemented under `docs/milestones/planned/milestone4.md` (Real Trust
> Boundary). The policy (`.agentignore` gitignore-parity, 3-tier policy, designated-secret floor),
> docker mount-mask, path-guard middleware, `permission_denied` interrupt wiring, `harness doctor`,
> CI pipeline, and security test suite all shipped. Stretch H (bwrap fs-tool jail) is not yet built.
> Named
> feature-plan doc (not a numbered milestone). Referenced from `design_doc.md` §2 (Sandbox &
> Isolation). Wins over `design_doc.md` for the mechanics of *which workspace paths an agent can see*.
>
> **Problem:** The workspace is a whole-tree bind mount (`-v ${WorkspacePath}:/project/workspace`).
> Every file under it — including secrets the user's own repo carries (`.env`, `id_rsa`,
> `.aws/credentials`) — is visible to the agent's file **and** shell tools. A prompt-injected or
> misbehaving agent can read them. This plan adds a policy + enforcement stack that restricts
> agent-visible workspace paths, with an inviolable floor for designated secrets.
>
> **Scope note:** This is *workspace* visibility (secrets living in the user's mounted repo). The
> harness's own `project/.env` is already never in the mount (except the repo-self-mount footgun,
> `design_doc.md` §2 / Q3). The agent shell-**env** allowlist (`_agent_shell_env`) already covers
> env-var secrets; this plan covers **file** secrets, which that allowlist does not touch.

---

## 1. Why not `.dockerignore` / image-layer stripping

Both are the wrong layer. `.dockerignore` governs the **build context** (`COPY` into the image); the
workspace is **bind-mounted at run time**, never built in — nothing to strip. Same for "just don't
load it into the image": the user's workspace was never in the image. Visibility is entirely a
**runtime mount / namespace** problem.

Docker bind mounts are **whole-tree**: you cannot mount a directory *except* some sub-paths. So at
the docker layer the only move is **deny-list masking** (mount everything, then overlay empties on
top). True **allow-list** ("show only these paths + what the agent creates") is a **bwrap / overlayfs**
capability, not a bind-mount one. This split drives the layered design below.

---

## 2. Policy model — three tiers, one inviolable floor

A single **policy** feeds every enforcement layer (§4). Resolved once, enforcement-agnostic.

| Tier | Meaning | Override |
|---|---|---|
| **Designated secrets** | User explicitly marks. **Always blocked, every layer, regardless of agent / model / allow-list / bwrap setting.** | **None.** No `!` negation, no allow-list entry, no flag. |
| **Pattern defaults** | Heuristic secret globs (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.ssh/`, `.aws/credentials`, `.netrc`, `.npmrc`, `.git-credentials`, `credentials.json`, `*.p12`, `*.pfx`). | Strong, but `-AllowUnmaskDefaults` + explicit `!` can override (convenience, not a guarantee). |
| **General visibility** | The user's allow / deny config (`.agentignore`). Full gitignore parity. | Fully user-controlled. |

**The designated-secret floor is the core security invariant.** It sits *below* all allow-list and
negation logic and is enforced **redundantly** (§4.4) so a single misconfigured layer cannot leak it.

### Config semantics — deny-list default, allow-list strict

- **Deny-list (default, practical for coding):** agent sees the whole workspace minus denied paths.
  Enumerating every source file as an allow-list is impractical for a coding agent.
- **Allow-list (strict, bwrap-native):** agent sees **only** listed base directories + a writable
  area for files it creates. For high-trust-gap / scoped tasks. This is `design_doc.md` §2's
  "whitelist of allowed base directories" (L857) and "bind-mount whitelist is the real boundary" (L245).

Same config file expresses both via a mode switch.

---

## 3. Config format — `.agentignore`

**Name:** `.agentignore` — matches the emerging AI-tool convention (`.aiexclude` / Google,
`.cursorignore` / Cursor, `.aiignore` / JetBrains, `.codeiumignore` / Codeium) and reads beside
`.gitignore`/`.dockerignore`. Describes *intent* ("hidden from the agent"), which survives the mask
mechanism changing from bind-mounts to bwrap.

**Syntax:** full gitignore parity — `**`, `!` negation, trailing-slash dirs, last-match-wins,
per-directory nesting.

**Recursive discovery:** every `.agentignore` at any depth is collected; each file's patterns
resolve **relative to that file's own directory** (gitignore semantics). Root + nested compose.

**Two config locations:**
- **Authoritative — state dir** (`project/state/<hash>/agentignore`, outside the workspace mount,
  unreachable by agent tools per `design_doc.md` §2 / Q3 isolation). Agent cannot read, edit, delete,
  or bypass it. No git-tree conflict. This is where the **designated-secret floor** lives.
- **Convenience — in-workspace `.agentignore`** (recursive, travels with the repo). Advisory: the
  agent *can* edit these, so on each launch the resolved mask is **snapshotted** to the state dir;
  if the next run's in-workspace config **reduces** protection vs the snapshot, warn loudly
  ("protection reduced — N paths no longer masked; continue?"). Catches both agent tampering and
  accidental user edits.

**Why not overlay-mask the in-workspace `.agentignore` itself:** an empty read-only overlay over a
git-tracked file makes the tree dirty → the `git-branch` workflow gate (asserts clean tree at
`session.start`) fails. The state-dir authoritative config achieves "agent can't touch it" *without*
the git collision.

**Append-only agent tool `mask_add(path)`** (guarded by a flag like `recall_past`): writes to the
state-dir config so the agent can **raise** protection, never lower it. **No `mask_remove`.** Mask
is frozen mid-session anyway (§4.1), so `mask_add` takes effect next run.

**No negation of the floor:** the resolver rejects/ignores any allow-list entry or `!` that would
expose a designated secret, and warns.

---

## 4. Enforcement layers

| Layer | Model | Covers | Absence | Status |
|---|---|---|---|---|
| **4.1 Docker mount mask** | deny-list | **all tools** (file + shell + any proc — real fs) | present-but-empty | buildable now |
| **4.2 bwrap fs-tool jail** | **allow-list** | shell **+ all fs-touching file tools** (once routed) | true absent | `sandbox-exec` built, not wired |
| **4.3 overlayfs view** | allow-list | all tools, tool-agnostic | true absent | optional extension, not built |

### 4.1 Docker mount mask (interim, approved for v1)

- Pre-flight scan (§5) → for each masked path, append `-v <emptyFile|emptyDir>:/project/workspace/<rel>:ro`
  **after** the base workspace `-v`. Docker layers later mounts on top → path shows empty.
- **Portable source:** an empty temp **file** + empty temp **dir** (reused per hit). **Not `/dev/null`**
  (Linux-host-only; breaks Docker Desktop Windows).
- Covers **every** process in the container (it changes the actual mounted fs), but is whole-tree
  → deny-list only, and "present-but-empty" not absent (= the `mask` visibility mode, §6).
- **Always-on enforcer of the designated-secret floor** — applied even in allow-list mode, even when
  bwrap is off/unwired.
- **Frozen at launch:** scan + mounts computed host-side before `docker run`; agent runtime edits to
  any `.agentignore` cannot unmask the current session.
- Not sandboxing — trust boundary is still the container (`docs/milestones/complete/mvp.md` §5). Do not describe it
  as a sandbox.

### 4.2 bwrap fs-tool jail (the real allow-list boundary)

The design's real boundary (`design_doc.md` §2, L245/L857). `scripts/sandbox-exec.sh` exists but is
**built-not-wired**, and nested-userns-in-docker is **unverified** (`design_doc.md` §2, ~L87-91).

- **Route ALL fs-touching tools through the jail, not just the shell.** deepagents' file tools
  (read / write / edit / ls / glob) run **in-process** today and read the container's real fs — so
  bwrap over the shell alone leaves the file tools bypassing it. Close the gap by executing every
  fs tool inside the agent's bwrap namespace (per-tool `sandbox-exec`, or a persistent jailed
  tool-executor), so the **same bind whitelist** gates shell and file access uniformly.
- Binds sourced from the resolved policy: allow-list mode binds only listed base dirs; workspace
  bound writable so agent-created files persist; designated secrets **never** bound.
- **Verify nested userns runs first** (may need `--security-opt` / userns tweaks); until wired +
  verified, do not claim sandboxing.
- **Enforce the invariant "all fs tools must route through the jail"** in `agent.py` so a newly added
  tool (e.g. a new MCP fs tool) cannot silently reopen the bypass.

**Trade-offs:** single enforcement point (reuses `sandbox-exec`), true absence, allow-list native;
but must guarantee no tool escapes the jail, per-read spawn overhead (or build a persistent executor),
and the nested-userns verification risk.

### 4.3 overlayfs view (optional extension)

Tool-agnostic true allow-list at the **mount** itself: lower = real repo (read-only, non-allowed
whiteout'd out) + upper = writable tmpfs (agent additions/edits) + merged at `/project/workspace`.

**Trade-offs:** covers even tools that escape the jail (defense-in-depth); but agent writes land in
the tmpfs upper, not the live host repo → extract the upper diff at `session.end` → commit (fits the
git-pr / telemetry-to-PR flow, but **changes today's live-write model**). overlayfs-in-container
support/perf varies by host. Keep **opt-in**, not required for v1.

### 4.4 Redundant enforcement of the designated-secret floor

Enforced at every layer so one misconfig can't leak:
1. **Docker mask** — empty-overlay always applied to designated secrets (even in allow-list mode / bwrap off).
2. **bwrap** — never `--bind`s a designated-secret path; no generated bind set can re-add it.
3. **File-tool jail** — routed read/write does an explicit path-refuse on designated secrets (belt-and-suspenders beyond "not bound").
4. **Config resolver** — rejects/ignores any allow-list entry or `!` that would expose a designated secret; warns.

---

## 5. Scanner — one matcher, pre-flight container

Full gitignore parity (negation, `**`, nesting, last-match-wins) in **both** PowerShell and bash =
two error-prone reimplementations of a security-critical matcher. Instead, **one Python matcher** run
as a read-only pre-flight container in the harness venv:

```
docker run --rm -v ${WorkspacePath}:/project/workspace:ro deepagent-harness \
    python3 -m harness.mask_scan          # stdout: <mode> <type> <relpath> per line
```

- Vendored stdlib gitwildmatch (no pip dep); walks nested `.agentignore` + state-dir config +
  defaults; **canonicalizes** paths to kill symlink re-exposure; emits resolved set + mode.
- Both `.ps1`/`.sh` just parse the flat list → build `-v` args (docker mask) or bind flags (bwrap).
  No matcher logic in shell. ~1s extra pre-flight cost.
- Honors the two-stack rule (scanner in the harness venv, not the workspace conda env).

---

## 6. Visibility modes

- **`mask` (default, free):** name still visible, contents gone (empty overlay / not-bound). This is
  exactly "known to exist but not accessible" — and the base docker mechanism already produces it.
- **`hide` (deferred v2):** name absent too. Needs mount-namespace whiteout / overlayfs upper. Harder;
  not required for v1.

Per-entry mode via a config directive (exact syntax TBD; keep gitignore pattern lines clean — use a
header directive or a separate block, not per-line flags).

---

## 7. Sequencing

1. **Config + resolver** (§3, §5) — enforcement-agnostic; designated-secret floor as a separate absolute tier.
2. **v1 — docker deny-list mask** (§4.1) — immediate secret hiding across all tools; always-on floor enforcer.
3. **Main forward step — bwrap** (§4.2) — wire `sandbox-exec`, route all fs tools through the jail,
   allow-list binds from config; verify nested userns first.
4. **Optional — overlayfs** (§4.3) — tool-agnostic true allow-list + upper-diff write-back.

Config is built once and feeds all layers, so later layers slot in without reworking policy.

---

## 8. Open items

- **Defaults on vs off:** recommend **on** (mask-by-default; secrets in a mounted repo are the common case).
- **`.agentignore` name:** confirm vs `.workspaceignore` / `.mountignore`.
- **`hide` mode:** spec as deferred v2 (above) or drop from scope entirely.
- **Config mode syntax** (deny vs allow; per-entry visibility mode): pin exact directive format.
- **Cross-platform:** keep `.ps1` / `.sh` pairs in sync (scan-list parsing, mask-mount emission).

---

## 9. Tests (host-runnable, stdlib)

- Scanner: gitignore parity (negation, `**`, nesting, last-match-wins), symlink canonicalization,
  mode classification.
- **Designated-secret floor invariant:** a `!`/allow-list entry targeting a designated secret must
  **not** expose it, at the resolver level; regression test per `deepagent-image/CLAUDE.md` "every bug
  fix ships with a regression test."
- Mask-mount emission: correct `-v` arg set from a scan list (both shells).
- Snapshot / protection-reduction warning between runs.
