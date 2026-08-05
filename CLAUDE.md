# holder — Deep Agents Coding Harness

This repo builds a Docker harness that runs a [Deep Agents](https://pypi.org/project/deepagents/)
coding agent against a mounted workspace, with provider-agnostic model selection and disposable,
secret-safe containers.

- `design_doc.md` — full target vision (multi-agent, FSM routing, bubblewrap jail, telemetry-to-PR).
- `docs/` — spec docs, organized by type. See `docs/README.md` for the full map, including the
  **milestone lifecycle** (`planned/` docs-only → `in-progress/` doc + separate `_invariants.md` +
  code → `complete/` doc-only with invariants folded in). In short:
  - `docs/milestones/complete/` — **built + merged** milestones (`mvp.md`, `milestone1.md`,
    `milestone2.md`, `milestone3.md`). These record shipped scope; the code is authoritative where
    they drift.
    - `mvp.md` — the shipped baseline: one-command containerized Deep Agents coding agent.
    - `milestone1.md` — cost/token visibility + resource caps.
    - `milestone2.md` — present/past memory (fresh-by-default thread + separate on-demand archive).
      See the "Present / past memory" section in `deepagent-image/CLAUDE.md`.
    - `milestone3.md` — human-in-the-loop (one `interrupt()` spine, three trigger sources) + the §12
      resilience/headless prereqs it rides on. **Built** — see §0 build status for what shipped vs.
      deferred, and the "Human-in-the-loop" section in `deepagent-image/CLAUDE.md`.
  - `docs/milestones/in-progress/` — **being built** milestones (doc + separate invariants doc + code
    on a feature branch). *(`milestone4.md` — **Real Trust Boundary**, code on `feat/milestone_4`,
    slices A–G landed, not yet merged (workspace visibility — `.agentignore`, 3-tier policy,
    designated-secret floor —, docker mount-mask, path-guard middleware, `harness doctor`, CI pipeline,
    security test suite), **slice H (bwrap fs-tool jail) is core v1 scope, not stretch, and remains
    unbuilt — M4 is not done until it ships.** A–G harden the container's deny-list; H is the slice
    that makes the milestone's name ("Real Trust Boundary") true rather than aspirational — it routes
    every fs-touching tool, shell included, through an allow-list bind-whitelist, closing the gap A–G
    structurally cannot (see `milestone4.md` §3/§14). Slice D (`permission_denied` interrupt) is
    **built, audit-only** — a path-guard denial (always a true workspace escape in v1; pathguard has
    no floor/mask awareness) never offers an interactive approve — a real escape must never be a thing
    an operator's mis-click can wave through, and every denial pathguard can currently produce is
    exactly that never-approvable case. It surfaces as an always-on stderr `path-guard DENIED` line
    (HITL or not) plus, when HITL is on, a structured record in `<state-dir>/denials.jsonl` — outside
    the workspace, so the agent can't truncate the evidence of its own escape attempt. The *refusal* is
    unchanged off-HITL. `milestone4_invariants.md` — the 30 checkable boundary invariants that drive
    its tests (3 of them — 5, 14, 16 — are blocked on H); folds into `milestone4.md` on completion.)*
  - `docs/milestones/planned/` — **not-yet-built** milestones (docs only). Wins over `design_doc.md`
    for "what we build next." *(Currently empty.)*
  - `docs/features/workspace_visibility.md` — **named feature plan** (not a numbered milestone): restrict
    which workspace paths an agent can see (`.agentignore` policy, designated-secret floor, docker-mask
    → bwrap fs-tool jail → optional overlayfs). **Planned** — summarized in `design_doc.md` §2.
  - `docs/specs/energy.md` — energy-tracking spec: the per-token estimate ships; the measured
    local-device path is specified, not built.
  - `docs/archive/` — superseded compound-engineering artifacts (brainstorm / ideation / plan for the
    now-implemented shared test infra). Historical reference, not live guidance.
- `deepagent-image/` — the actual harness (Dockerfile, `project/main.py`, `scripts/`). It has its
  own `CLAUDE.md` with detailed guidance — **read it before editing anything under
  `deepagent-image/`.** Do not duplicate that content here.

## Session lifecycle — git branch + PR (required)

**At the start of every session:**
1. Confirm a clean tree (`rtk git status`) and that you are on `main` (`rtk git branch`).
2. Create and switch to a new working branch before making any change:
   ```bash
   rtk git checkout -b <type>/<short-description>   # e.g. feat/model-routing, fix/env-leak
   ```
   Never commit directly to `main`. If already on a non-`main` branch from a prior session,
   keep using it instead of branching again.

**Before exiting / when the work is done:**
1. Commit the work with a clear message (see Conventions below).
2. Push the branch:
   ```bash
   rtk git push -u origin <branch>
   ```
3. Open a Pull Request back into `main`:
   ```bash
   rtk gh pr create --base main --fill
   ```
   Use `gh pr create --title "..." --body "..."` if `--fill` doesn't capture the intent.
4. Report the PR URL to the user. **Do not merge the PR** — leave the merge decision to the user
   unless they explicitly ask you to merge.

Per the global "confirm outward-facing actions" rule, pushing and PR creation are outward-facing:
do them when the work is complete or the user asks, not speculatively mid-session.

## Build & run (PowerShell primary on this machine)

```powershell
cd deepagent-image
.\scripts\build.ps1                       # docker build -t deepagent-harness
.\scripts\verify.ps1                      # sanity-check harness venv + conda
.\scripts\smoke.ps1                       # smoke test
.\scripts\smoke.ps1 -NetJail              # smoke test run inside the NetJail (NET_JAIL=1 ./scripts/smoke.sh)
.\scripts\run-docker.ps1                  # opens a persistent interactive session (you> prompt)
.\scripts\run-docker.ps1 "your task"      # runs that task first, then drops to the prompt
```

`run-docker` is a persistent multi-turn session, not a one-shot: the container stays up across
turns until you type `/exit` or `/quit` at the `you>` prompt (or Ctrl-D). It needs a TTY (`-it`);
piped/non-interactive stdin collapses to a single turn for CI/smoke. See `docs/milestones/complete/mvp.md` §1a.

`.sh` equivalents exist for each script — **keep the `.ps1` and `.sh` pairs in sync** when editing one.

### Script Pair Maintenance (`.ps1` ↔ `.sh`)

This repo maintains **parallel PowerShell and Bash scripts** for cross-platform compatibility:
- `build.ps1` ↔ `build.sh`
- `verify.ps1` ↔ `verify.sh`
- `smoke.ps1` ↔ `smoke.sh`
- `run-docker.ps1` ↔ `run-docker.sh`

**Sync rule:** When you edit one, **keep the pair in sync**. Both must implement the 
same logic and support the same flags. This is a known maintenance burden; a 
cross-platform wrapper was considered but rejected due to the depth of Windows/Unix 
differences (file paths, robocopy vs. rsync, registry vs. env var probing, etc.).

**Known sync points (track when editing):**
- `run-docker.ps1` ↔ `run-docker.sh`: ephemeral workspace copy, NetJail setup, state-dir derivation.
- `smoke.ps1` ↔ `smoke.sh`: pytest invocation, image staging, artifact handling.
- Both: launcher environment defaults (CPUS, MEMORY, PIDS_LIMIT).

**Verification:** `./scripts/check-parity.sh` (bash) / `.\scripts\check-parity.ps1` (ps1) 
validates critical sections match (see script for the parity rules).

### Launcher environment (host-side, **not** `.env`)

Host-side launcher variables (MAP_HOST_USER, CPUS, MEMORY, NET_JAIL, etc.) are 
documented in detail in **[deepagent-image/CLAUDE.md — Launcher environment](./deepagent-image/CLAUDE.md#launcher-environment-host-side-not-env)**.

The key distinction: `.env` is container-bound (via `--env-file`); launcher vars 
are read by the host shell *before* `docker run` and affect container startup 
parameters only.

## Hard rules

- **Secrets live in `deepagent-image/project/.env` only.** It is gitignored and never `COPY`ed into
  the image (passed at run time via `--env-file`). Never commit it, bake it into the image, or echo
  it into logs.
- **Two Python stacks, never mixed:** harness venv at `/opt/venv` (runs `main.py` only) vs. the
  workspace conda env at `<workspace>/.conda/env` (the agent's code/tests/installs). Harness changes
  go in `project/requirements.txt` + rebuild; never edit the harness to satisfy a workspace dep.
- The MVP's trust boundary is the **Docker container, not bubblewrap** — the shell tool is not yet
  routed through `sandbox-exec` (see `docs/milestones/complete/mvp.md` §5). Don't claim sandboxing the MVP doesn't have.
- `**/old/` and `**/suggestions/` are archived reference, not live code — ignore them.

## Conventions

- Always prefix shell commands with `rtk` per the global `~/.claude/CLAUDE.md` (token-optimized
  filters), including inside `&&` chains.
- Commit messages: imperative, scoped, explain *why* when non-obvious. End commit messages with:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```
- End PR bodies with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  ```
