# holder — Deep Agents Coding Harness

This repo builds a Docker harness that runs a [Deep Agents](https://pypi.org/project/deepagents/)
coding agent against a mounted workspace, with provider-agnostic model selection and disposable,
secret-safe containers.

- `design_doc.md` — full target vision (multi-agent, FSM routing, bubblewrap jail, telemetry-to-PR).
- `docs/` — spec docs, organized by type. See `docs/README.md` for the full map, including the
  **milestone lifecycle** (`planned/` docs-only → `in-progress/` doc + separate `_invariants.md` +
  code → `complete/` doc-only with invariants folded in). In short:
  - `docs/milestones/complete/` — **built + merged** milestones (`mvp.md`, `milestone1.md`,
    `milestone2.md`, `milestone3.md`, `milestone5.md`). These record shipped scope; the code is
    authoritative where they drift.
    - `mvp.md` — the shipped baseline: one-command containerized Deep Agents coding agent.
    - `milestone1.md` — cost/token visibility + resource caps.
    - `milestone2.md` — present/past memory (fresh-by-default thread + separate on-demand archive).
      See the "Present / past memory" section in `deepagent-image/CLAUDE.md`.
    - `milestone3.md` — human-in-the-loop (one `interrupt()` spine, three trigger sources) + the §12
      resilience/headless prereqs it rides on. **Built** — see §0 build status for what shipped vs.
      deferred, and the "Human-in-the-loop" section in `deepagent-image/CLAUDE.md`.
    - `milestone5.md` (+ `milestone5_spec.md`) — **Unified Config Surface**: CLI flags + an
      in-session `/config` command for live knobs (model, budgets, HITL preset) and a pre-spinup
      wizard (`harness config` / `harness config security`) for knobs fixed at container start
      (mask mode, jail/AppArmor, resource caps, NetJail), resolved through one `harness/config.py`
      precedence chain (CLI flag > env > profile file > default). **Built** — §4 records one
      deliberate deviation from the original plan (no arrow-key `/config` menu — most settable
      fields are free text, so a picker doesn't fit them) and a real `PauseMiddleware` caching bug
      the build surfaced and fixed along the way; §0.1 records the pre-merge review fixes (two
      passes, including the launcher forwarding `-e DEEPAGENTS_JAIL` so the seccomp relaxation and
      the in-container jail can't come apart). See the "Unified
      config" section in `deepagent-image/CLAUDE.md`.
  - `docs/milestones/in-progress/` — **being built** milestones (doc + separate invariants doc + code
    on a feature branch). *(`milestone4.md` — **Real Trust Boundary**, merged to `main`,
    slices **A–J all landed** (workspace visibility — `.agentignore`, 3-tier policy,
    designated-secret floor —, docker mount-mask, path-guard middleware, `harness doctor`, CI pipeline,
    security test suite). **Slice H (bwrap fs-tool jail) is built and opt-in** (`DEEPAGENTS_JAIL=1`),
    shipped as a **re-exec of the harness into a bwrap namespace** rather than the per-call jailed
    worker an earlier draft pinned — so "all fs tools route through the jail" is structural (the
    *process* is in the namespace) instead of an `agent.py` assertion. A–G harden the container's
    deny-list; H is the slice that makes the milestone's name ("Real Trust Boundary") true rather than
    aspirational — it routes every fs-touching tool, shell included, through an allow-list
    bind-whitelist, closing the gap A–G structurally cannot (see `milestone4.md` §3/§14). It is
    **off by default deliberately** (§16 fork 7): enabling it needs a narrow seccomp relaxation on the
    *outer* container to permit unprivileged user namespaces, which is an operator's trade to make, not
    a silent default — so with the jail off the boundary is still the container + deny-list mask, and
    the docs must keep saying so. The `bwrap --unshare-all` gate is **verified in the built image on a
    host with no LSM policy loaded** (Docker Desktop/WSL2) under the profile that ships, re-checkable
    via `scripts/smoke.{sh,ps1}` `JAIL_CHECK=1`/`-JailCheck`. **On an AppArmor host the jail does not
    start** without slice J: seccomp is only one of two independent gates, and Docker's
    `docker-default` profile denies `mount` outright, so bwrap fails *after* `unshare` succeeds.
    **Slice J — vendored `docker-default` with only its `mount` rule narrowed** (same shape as
    `seccomp-sync`) — is **built** (`docs/milestones/in-progress/milestone4.1.md`): the profile,
    `apparmor-sync --check`, install script, and `run-docker`/`harness doctor` wiring have all
    landed. **UNCONFIRMED:** the one thing not done is the live-host measurement on a real
    AppArmor-confined machine (Ubuntu/Debian Docker, GitHub runners) — no such host has been
    available to verify it on. CI's `apparmor-load-probe` job carries this measurement and is
    deliberately non-gating until it reports; until it does, treat the mount rule set as *derived,
    not confirmed*, same caveat `milestone4.1.md` and `apparmor/README.md` carry. The interim knob
    `DEEPAGENTS_JAIL_APPARMOR=unconfined` still works everywhere but drops the whole LSM profile
    rather than one rule — prefer the vendored profile once confirmed.
    (`milestone4.md` §11.6, §16 fork 10, invariants 37–38; `milestone4.1.md` §1/§13.1). SELinux
    hosts are untested.
    Slice D (`permission_denied` interrupt) is
    **built, audit-only** — a path-guard denial (always a true workspace escape in v1; pathguard has
    no floor/mask awareness) never offers an interactive approve — a real escape must never be a thing
    an operator's mis-click can wave through, and every denial pathguard can currently produce is
    exactly that never-approvable case. It surfaces as an always-on stderr `path-guard DENIED` line
    (HITL or not) plus, when HITL is on, a structured record in `<state-dir>/denials.jsonl` — outside
    the workspace, so the agent can't truncate the evidence of its own escape attempt. The *refusal* is
    unchanged off-HITL. `milestone4_invariants.md` — the 35 checkable boundary invariants that drive
    its tests. Invariants 5 (3rd leg) and 14 (shell coverage) are satisfied **with the jail on** and
    read as originally written with it off; **16 (approvable exception) stays deferred even under H** —
    re-exec overmounts masked paths empty rather than raising an explicit denial, so every
    `PathGuardDenied` is still a true escape and still never approvable. Folds into `milestone4.md` on
    completion.)*
    - `milestone5.1.md` (+ `milestone5.1_invariants.md`) — **Config Field Registry**: follow-on
      refactor of Milestone 5. M5 unified *resolution* but not *declaration* — a knob was spelled
      out in ten places, nine of which failed silently if missed, and no field carried its own
      valid values, which is what blocked the arrow-key `/config` menu M5 scoped out. One
      `FieldSpec` table (`harness/config.py`) is now the single declaration; `Settings`, profile
      I/O, the resolver loop, both display renderers, `/config set` dispatch, the wizard screens,
      and the picker all derive from it. **Built** — slices R1–R7 all landed. Behavior-preserving
      by construction: the M5 suite was the oracle and passed **with zero test edits**, so an
      edited test that isn't asserting a now-derived constant is a red flag. §0.1 records two
      deliberate deviations (the applier map lives in `cli.py`, not on the spec, because an
      applier touches the tracker/archive/agent that `config.py` must not import; and
      `_handle_config` keeps its 3-tuple return as an adapter over the new `LiveContext`). The one
      sanctioned behavior change: **every enum knob now rejects an invalid value at the point of
      entry**, closing M5's known `mask_mode: alow` → silently-`deny` gap for profile, env, and CLI
      at once. See the "Unified config" section in `deepagent-image/CLAUDE.md`.
  - `docs/milestones/planned/` — **not-yet-built** milestones (docs only). Wins over `design_doc.md`
    for "what we build next." *(Currently empty — `milestone5.1.md` moved to `in-progress/`.)*
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

### Worktrees — copy `.env` in as part of setup

`deepagent-image/project/.env` is gitignored, so `git worktree add` produces a tree where the
harness cannot run: no API keys, no `OLLAMA_HOST`, no `DEEPAGENTS_MODEL`. Copy it over from the
main worktree in the same step that creates the worktree:

```powershell
rtk git worktree add ..\wt-<name> -b <type>/<short-description>
rtk Copy-Item deepagent-image\project\.env ..\wt-<name>\deepagent-image\project\.env
```
```bash
rtk git worktree add ../wt-<name> -b <type>/<short-description>
rtk cp deepagent-image/project/.env ../wt-<name>/deepagent-image/project/.env
```

**Copy it with a file command — never by reading the file and writing its contents back out.**
A copy moves the bytes without either end seeing them; a read-then-write pulls live secrets into
the agent's context, which is both wasted tokens and a leak path. Same reason applies to `cat`ing
or diffing it to "check" — confirm with a path/size test instead. This is the "secrets live in
`.env` only, never echoed into logs" hard rule below, applied to worktree setup.

Removing a worktree: `rtk git worktree remove <path>` (it refuses if the tree is dirty — check
`git status --porcelain` in it first, and confirm the branch is pushed before deleting anything).

## Build & run (PowerShell primary on this machine)

```powershell
cd deepagent-image
.\scripts\build.ps1                       # docker build -t deepagent-harness
.\scripts\verify.ps1                      # sanity-check harness venv + conda
.\scripts\smoke.ps1                       # smoke test
.\scripts\smoke.ps1 -NetJail              # smoke test run inside the NetJail (NET_JAIL=1 ./scripts/smoke.sh)
.\scripts\smoke.ps1 -JailCheck            # require the M4 slice H bwrap gate to pass (JAIL_CHECK=1 ./scripts/smoke.sh)
                                          #   the gate runs either way; the flag turns a
                                          #   "host can't build the jail" skip into a failure.
                                          #   It skips for two distinct reasons — no nested userns,
                                          #   or the host LSM denying bwrap's mounts (AppArmor).
                                          #   On an AppArmor host, set DEEPAGENTS_JAIL_APPARMOR=unconfined
                                          #   to make it run (drops the whole profile — see §11.6).
.\scripts\smoke.ps1 -LiveModel            # + the live-model tier (LIVE_MODEL=1 ./scripts/smoke.sh):
                                          #   real prompts to a real model, real replies asserted.
                                          #   Needs a reachable model — with the shipped default
                                          #   that is a host `ollama serve`. Cases SKIP (not fail)
                                          #   when unreachable, so read the -ra recap.
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

## Testing guideline — exercise the real model where the behavior is the model's

The default provider is **local Ollama** (`ollama:gemma4`, `priority = 0`) precisely so that
running a real model is cheap: no key, no quota, no free-tier rate limit to wait on.

**When a test asserts something that depends on what a *model* does — not on what the harness
does with it — add a live-model case.** Stubs are deterministic *and* structurally blind: they
answer however the test wrote them to, so a harness that is internally consistent but does not
actually work still reads green. Several real bugs surfaced only by running a model, never by the
stubbed suite.

- Live cases live in `deepagent-image/project/tests/test_live_model.py`, carry the `live_model`
  marker, and take the `live_model` fixture. **Off unless `DEEPAGENTS_LIVE_MODEL=1`**;
  `smoke -LiveModel` / `LIVE_MODEL=1 ./scripts/smoke.sh` turns them on.
- This is **additive, not a replacement**. The host and image tiers stay hermetic (no keys, no
  network, no real model calls) — a stubbed test is still the right tool for harness logic, and
  CI must be able to run the suite with nothing installed.
- A live case must use a **local** model. A test that needs a cloud key is a test CI can never run.
- Live cases **skip** rather than fail when the model is unreachable, so a green exit is not proof
  they ran — check the `-ra` recap when you mean to be testing against a real model.

Full tier description: `deepagent-image/CLAUDE.md` → "Test suite layout & conventions".

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
