# holder — Deep Agents Coding Harness

This repo builds a Docker harness that runs a [Deep Agents](https://pypi.org/project/deepagents/)
coding agent against a mounted workspace, with provider-agnostic model selection and disposable,
secret-safe containers.

- `design_doc.md` — full target vision (multi-agent, FSM routing, bubblewrap jail, telemetry-to-PR).
- `docs/` — spec docs, organized by type. See `docs/README.md` for the full map. In short:
  - `docs/milestones/complete/` — **built** milestones (`mvp.md`, `milestone1.md`, `milestone2.md`).
    These record shipped scope; the code is authoritative where they drift.
    - `mvp.md` — the shipped baseline: one-command containerized Deep Agents coding agent.
    - `milestone1.md` — cost/token visibility + resource caps.
    - `milestone2.md` — present/past memory (fresh-by-default thread + separate on-demand archive).
      See the "Present / past memory" section in `deepagent-image/CLAUDE.md`.
  - `docs/milestones/planned/` — **not-yet-built** milestones. Wins over `design_doc.md` for
    "what we build next."
    - `milestone3.md` — human-in-the-loop (one `interrupt()` spine, three trigger sources).
      **Spec** — full build spec: dependency-ordered slices (spine + the §12 resilience/headless
      prerequisites it rides on), design forks closed. Current frontier; slice 6 PR-a already built.
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
