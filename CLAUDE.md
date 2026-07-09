# holder — Deep Agents Coding Harness

This repo builds a Docker harness that runs a [Deep Agents](https://pypi.org/project/deepagents/)
coding agent against a mounted workspace, with provider-agnostic model selection and disposable,
secret-safe containers.

- `design_doc.md` — full target vision (multi-agent, FSM routing, bubblewrap jail, telemetry-to-PR).
- `design_doc_mvp.md` — **the current build target.** When the two disagree, the MVP doc wins for
  "what we are building now."
- `design_doc_milestone1.md` — the planned successor to the MVP (cost/token visibility + resource
  caps). Wins over `design_doc.md` for "what we build next."
- `design_doc_milestone2.md` — successor to Milestone 1: present/past memory (fresh-by-default
  thread + a separate, on-demand archive that accumulates across sessions). **Built** — see the
  "Present / past memory" section in `deepagent-image/CLAUDE.md`.
- `design_doc_milestone3.md` — successor to Milestone 2: human-in-the-loop (one `interrupt()` spine,
  three trigger sources — deterministic workflow pause, agent `ask_human` tool, system events).
  **Stub** — schedules the `design_doc.md` §9 / §3 HITL design into build slices; not yet spec-complete.
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
piped/non-interactive stdin collapses to a single turn for CI/smoke. See `design_doc_mvp.md` §1a.

`.sh` equivalents exist for each script — **keep the `.ps1` and `.sh` pairs in sync** when editing one.

## Hard rules

- **Secrets live in `deepagent-image/project/.env` only.** It is gitignored and never `COPY`ed into
  the image (passed at run time via `--env-file`). Never commit it, bake it into the image, or echo
  it into logs.
- **Two Python stacks, never mixed:** harness venv at `/opt/venv` (runs `main.py` only) vs. the
  workspace conda env at `<workspace>/.conda/env` (the agent's code/tests/installs). Harness changes
  go in `project/requirements.txt` + rebuild; never edit the harness to satisfy a workspace dep.
- The MVP's trust boundary is the **Docker container, not bubblewrap** — the shell tool is not yet
  routed through `sandbox-exec` (see `design_doc_mvp.md` §5). Don't claim sandboxing the MVP doesn't have.
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
