# deepagent-image

A Docker harness that runs a [Deep Agents](https://pypi.org/project/deepagents/) coding agent
against a mounted workspace. The image bundles a fixed **harness** Python env; the agent operates
on user code inside a separate **workspace** conda env. `project/main.py` is the entrypoint.

> Scope note: this file is guidance for working **on** this repo (the harness). `project/AGENTS.md`
> is a different file — instructions for the agent running **inside** the built container. Don't
> conflate them. `build_agent` reads `AGENTS.md` from the container CWD (`/project`) and **appends
> it verbatim to the agent's system prompt** (`main.py`, after `BASE_SYSTEM_PROMPT`), so editing
> `AGENTS.md` directly changes agent behavior at run time — treat it as prompt code, not docs.

## Layout

- `Dockerfile` — builds the `deepagent-harness` image (ubuntu:24.04 + uv venv at `/opt/venv` +
  Miniforge at `/opt/conda`). Sets `DEEPAGENTS_IN_CONTAINER=1` so `main.py` knows it's in-image.
- `project/main.py` — harness entrypoint. Provider/model routing, MCP tool loading, hooks,
  SqliteSaver checkpointer, builds the deep agent and runs the task.
- `project/requirements.txt` — harness deps only (installed into `/opt/venv`). Not the agent's
  workspace deps.
- `project/.env.example` — copy to `project/.env`, set API keys. **`.env` is gitignored and never
  baked into the image** — it's passed at run time via `--env-file`.
- `project/.mcp.json`, `project/hooks.json` — optional MCP servers and lifecycle hooks.
- `project/workspace/` — seed workspace (environment.yml, run-in-env.sh). Copied to
  `/project/workspace-seed/` in the image; the real workspace is bind-mounted at run time.
- `scripts/` — `build`, `run-docker`, `verify`, `smoke` in both `.ps1` (Windows) and `.sh`.

## Commands (PowerShell primary on this machine)

```powershell
.\scripts\build.ps1                       # docker build -t deepagent-harness
.\scripts\verify.ps1                      # sanity-check harness venv + conda in one start
.\scripts\smoke.ps1                       # smoke test
.\scripts\run-docker.ps1 "your task"      # run agent against project\workspace
.\scripts\run-docker.ps1 -WorkspacePath C:\path\to\repo "your task"
```

`run-docker.ps1` refuses to start without `project\.env`. It bind-mounts the workspace to
`/project/workspace` and seeds missing `environment.yml` / `.gitignore` / `run-in-env.sh`.

## Two Python stacks — do not mix

| Stack | Location | For |
|-------|----------|-----|
| Harness | `/opt/venv` (first on PATH) | `main.py` runtime only |
| Workspace | `<workspace>/.conda/env` | the agent's project code, tests, installs |

Harness changes → `project/requirements.txt` + rebuild. Never edit `/opt/venv`, `/opt/conda`, or
`main.py` to satisfy a *workspace* dependency.

## Model routing (`main.py`)

`PROVIDERS` is the single source of truth — `choose_model`, credential validation, and chat-model
resolution all derive from it, so maps can't drift. Auto-selection scans the list top-to-bottom
(order = priority) and **skips any provider whose `default_model` is `None`** (ollama, lmstudio,
openrouter are intentionally unset). Override explicitly with `DEEPAGENTS_MODEL=provider:model`.
OpenAI-compatible providers (cursor, openrouter, lmstudio) route via `ChatOpenAI` and need their
`*_BASE_URL`.

## Gotchas

- Secrets live in `project/.env` only. Don't commit them, don't `COPY` them into the image, don't
  echo them into logs.
- `main.py` uses the `DEEPAGENTS_IN_CONTAINER` env marker to detect the harness image — don't
  swap it back to filesystem sniffing (e.g. checking for `/project`).
- Conversation state persists at `<workspace>/.deepagents/checkpoints.sqlite`, keyed by
  `DEEPAGENTS_THREAD_ID`. Reuse the id to resume a thread.
- `project/suggestions/old/` is archived reference, not live code — ignore it.

## Conventions

- Keep `.ps1` and `.sh` script pairs in sync when editing one.
- Follow `rtk`-prefixed command usage per global CLAUDE.md.
