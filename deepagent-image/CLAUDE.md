# deepagent-image

A Docker harness that runs a [Deep Agents](https://pypi.org/project/deepagents/) coding agent
against a mounted workspace. The image bundles a fixed **harness** Python env; the agent operates
on user code inside a separate **workspace** conda env. `project/main.py` is the entrypoint.

> Scope note: this file is guidance for working **on** this repo (the harness). `project/AGENTS.md`
> is a different file — instructions for the agent running **inside** the built container. Don't
> conflate them. `build_agent` reads `AGENTS.md` from the container CWD (`/project`) and **appends
> it verbatim to the agent's system prompt** (`harness/agent.py`, after `BASE_SYSTEM_PROMPT`), so editing
> `AGENTS.md` directly changes agent behavior at run time — treat it as prompt code, not docs.

## Layout

> Why the extra `project/` level: it is bind-mounted to the container's `/project` (the WORKDIR)
> at run time, so it is the agent's filesystem root, not just a source folder. `main.py` reads
> `AGENTS.md` / `.mcp.json` / `hooks.json` from CWD (`/project`), and the workspace mounts under it
> at `/project/workspace`. Keep run-time config files directly in `project/`.

- `Dockerfile` — builds the `deepagent-harness` image (ubuntu:24.04 + uv venv at `/opt/venv` +
  Miniforge at `/opt/conda`). Sets `DEEPAGENTS_IN_CONTAINER=1` so the harness knows it's in-image.
- `project/main.py` — thin entrypoint shim (kept so `python3 main.py` still works). All logic lives
  in the `harness/` package; `python3 -m harness` is the equivalent entry.
- `project/harness/` — the harness package, split by concern:
  - `providers.py` — `PROVIDERS` registry + `choose_model` / `validate_credentials` /
    `resolve_chat_model` (model routing; see "Model routing" below).
  - `loaders.py` — optional-file IO: `AGENTS.md` text, `.mcp.json` tools, `hooks.json`.
  - `workflows.py` — the §3 workflow engine: `workflows/` folder format, gates
    (`trigger.py`/`trigger.sh`), side-effect steps, `WorkflowMiddleware`; the flat
    `hooks.json` is adapted into always-gate workflows (see "Custom workflows" below).
  - `agent.py` — workspace resolution, system prompt, `build_agent`, result extraction.
  - `cli.py` — `parse_args` + `main()`: wires the above around the SqliteSaver checkpointer, builds
    the agent once, then hands off to `run_repl()` — the interactive multi-turn loop (see below).
- `project/requirements.txt` — harness deps only (installed into `/opt/venv`). Not the agent's
  workspace deps.
- `project/.env.example` — copy to `project/.env`, set API keys. **`.env` is gitignored and never
  baked into the image** — it's passed at run time via `--env-file`.
- `project/providers/` — on-disk provider/model registry (`<provider>/provider.toml` +
  `<provider>/models/<model>.toml`). Loaded by `harness/providers.py`; see `providers/README.md`.
- `project/.mcp.json`, `project/hooks.json` — optional MCP servers and lifecycle hooks.
- `project/workflows/` — custom workflow folders (§3); ships `git-branch` +
  `git-pr` (the git session lifecycle). See "Custom workflows" below.
- `project/workspace/` — seed workspace (environment.yml, run-in-env.sh). Copied to
  `/project/workspace-seed/` in the image; the real workspace is bind-mounted at run time.
- `scripts/` — `build`, `run-docker`, `verify`, `smoke`, `sync-models` in both `.ps1` (Windows)
  and `.sh`. `sync-models` is a dev-time registry refresh (see Model routing).

## Commands (PowerShell primary on this machine)

```powershell
.\scripts\build.ps1                       # docker build -t deepagent-harness
.\scripts\verify.ps1                      # sanity-check harness venv + conda in one start
.\scripts\smoke.ps1                       # smoke test
.\scripts\run-docker.ps1                  # opens straight to the you> prompt
.\scripts\run-docker.ps1 "your task"      # runs that task first, then drops to the prompt
.\scripts\run-docker.ps1 -WorkspacePath C:\path\to\repo "your task"
```

`run-docker.ps1` refuses to start without `project\.env`. It bind-mounts the workspace to
`/project/workspace`, seeds missing `environment.yml` / `.gitignore` / `run-in-env.sh`, and runs
the container with `-it` so the in-container REPL (`harness/cli.py:run_repl`) has a TTY for its
`you>` prompt loop — the container stays up across turns until `/exit`/`/quit`/Ctrl-D. A redirected
stdin (CI, piped smoke runs) drops `-t` and the harness itself degrades to a single non-interactive
turn (see `design_doc_mvp.md` §1a).

## Two Python stacks — do not mix

| Stack | Location | For |
|-------|----------|-----|
| Harness | `/opt/venv` (first on PATH) | `main.py` runtime only |
| Workspace | `<workspace>/.conda/env` | the agent's project code, tests, installs |

Harness changes → `project/requirements.txt` + rebuild. Never edit `/opt/venv`, `/opt/conda`, or
`main.py` to satisfy a *workspace* dependency.

## Model routing (`harness/providers.py`)

`PROVIDERS` is the single source of truth — `choose_model`, credential validation, and chat-model
resolution all derive from it, so maps can't drift. It is **loaded at import time from the
`project/providers/` registry** (one `provider.toml` per provider + `models/<model>.toml` per
model), not hard-coded — add/change a provider or model by editing TOML, no Python edit needed
(see `providers/README.md`). Auto-selection scans by ascending `priority` and **skips any provider
whose `default_model` is unset** (ollama, lmstudio, openrouter are intentionally unset). Override
explicitly with `DEEPAGENTS_MODEL=provider:model`. OpenAI-compatible providers (cursor, openrouter,
lmstudio) route via `ChatOpenAI` and need their `*_BASE_URL`. `DEEPAGENTS_PROVIDERS_DIR` overrides
the registry path (used by tests).

`scripts/sync-models.{sh,ps1}` (= `python3 -m harness sync-models`, code in `harness/sync_models.py`)
regenerates `models/*.toml` from each provider's live list-models endpoint. **Dev-time only** — it
needs API keys + network (the sealed runtime has neither) and writes registry files you then commit.
It never rewrites `provider.toml`, so `default_model` stays a human choice. Per-provider fetching
splits into pure `parse_*` functions (response JSON → `ModelInfo`, unit-tested) and a thin urllib
GET, so no new dependency.

## Custom workflows (`harness/workflows.py`, design_doc.md §3)

A **workflow** = trigger (gate) × hook point × action (steps), discovered as a
self-contained folder under `workflows/` (`DEEPAGENTS_WORKFLOWS_DIR` overrides
the path). This is the **deterministic slice**: predicate gates + the
side-effect action tier. The classifier gate and the context-mutation /
control-flow action tiers are **planned, not built**.

Folder format (`workflows/<name>/`):
- `workflow.md` — manifest. YAML-ish frontmatter (parsed by a tiny stdlib
  parser, no `pyyaml`): `name` (== folder name), `hook` (one of the 7 events),
  `gate`, `steps` (ordered; **relative paths resolve against the folder**,
  absolute paths run as-is). Body is prose.
- `trigger.py` **or** `trigger.sh` (fixed basename, exactly one):
  - `trigger.py` — canonical, **in-process** (`gate(ctx: GateContext) -> bool`);
    runs in the harness venv, so **stdlib + engine API only** (two-stack rule).
  - `trigger.sh` — subprocess predicate; **exit 0 = run, non-zero = skip**. Gets
    `DEEPAGENTS_HOOK_EVENT` / `DEEPAGENTS_WORKSPACE` / `DEEPAGENTS_PROMPT` as env.
- step scripts (side-effects; result discarded, `check=False`, like the old hooks).

Hook points: `session.start`/`.end` (fire once in `cli.main()`), `agent.start`/
`.end`, `model.start`/`.end`, `tool.start`/`.end` (per-event via
`WorkflowMiddleware`, appended only when a non-session workflow exists). A bad
manifest (name mismatch, unknown hook, missing/duplicate gate) fails loudly with
`SystemExit`.

`hooks.json` is the **flat precursor**: each entry is adapted into a synthetic
`always`-gate (no trigger file) side-effect workflow, so both share one path.

**Shipped workflows — git session lifecycle (a paired set; one hook each):**
- `git-branch` (`session.start`) — gate: is-git-repo. Asserts a clean tree,
  fetches `origin/main`, checks out `agent/<provider>/<session-id>`, persists
  `session-id`/branch/base to `<workspace>/.deepagents/session.env`.
- `git-pr` (`session.end`) — gate: `session.env` exists. Stages (excludes
  `.deepagents`/`.agent_telemetry`), commits, pushes, and `gh pr create`s into
  `main` — **never auto-merges** (§3 merge policy; human pre-review mandatory).

Both are **safe no-ops** without their prerequisites: not a git repo / dirty tree
→ git-branch skips; no remote → push skipped (branch kept local); no `gh` /
`GH_TOKEN` → PR skipped. So a keyless smoke run still exits 0.

Tests: `tests/test_workflows.py` (pure; `sh`-dependent gate tests self-skip
without `sh`) — run by `smoke`, or standalone (`python3 tests/test_workflows.py`).

## Cost / token / energy tracking (Milestone 1)

A fully optional tracker reports per-turn and session token/cost/energy and can
cap a session. It is one `AgentMiddleware` (`harness/cost.py:CostTrackerMiddleware`)
appended in `cli.py:main` **only when there's something to track** — non-`free`
pricing, an energy estimate, or a budget. Otherwise nothing is appended and the
harness behaves byte-for-byte like the MVP (the "removable" contract,
`design_doc_milestone1.md` §2.5). `harness/cost.py` holds the math only; it must
never import `providers.py` (the import goes providers → cost, §2.4).

- **Pricing lives in the registry, not Python.** `provider.toml` declares
  `pricing = "rate_table" | "reported" | "free"`; `rate_table` models carry a
  `[pricing]` table (USD per **million** tokens: `input`/`output`/`cache_read`/
  `cache_write` + `priced_as_of`). See `providers/README.md`. `cache_*` are the
  split cached-vs-fresh prices, recorded now though caching isn't enabled.
- **Official vs. estimated prices are split.** Top-level `[pricing]` = official
  (vendor-published, incl. sync-pulled API rates), shown plain. Nested
  `[pricing.estimate]` = best-effort/hand-filled, shown with a `~` prefix and
  `(est)` tag. Official wins when both present; an unmarked guess is read as
  estimate, never official (`cost.py:rates_from_toml`, `ModelRates.pricing_source`).
- **Missing rate is loud, not fatal.** A `rate_table` model with no pricing table
  warns once, then runs with cost shown as a floor (unpriced calls excluded) —
  never a silent `$0`. `DEEPAGENTS_PRICE_ESTIMATE` (USD/Mtok) estimates instead
  (also marked `~`/`(est)`).
- **Energy** is an optional per-model `[energy]` estimate (Wh/token), tracked for
  any provider incl. local `free` ones; `DEEPAGENTS_ELECTRICITY_RATE` (USD/kWh)
  turns it into an electricity cost. Measured local-device energy is **specified,
  not built** — see `../ENERGY_SPEC.md` and `cost.py:measure_local_energy_wh`.
- **Budgets:** `--max-cost` / `--max-tokens` (or `DEEPAGENTS_MAX_COST` /
  `DEEPAGENTS_MAX_TOKENS`) end the REPL with `[harness] budget exceeded` once a
  cumulative total crosses, then print the session total — same deterministic
  exit as `/exit`.
- **Output:** per turn `[harness] usage: turn[...] session[...]` and at close
  `[harness] session total: ...`, both on **stderr** (out of the agent's reply
  stream, like the other stage markers).
- **Tests:** `tests/test_cost.py` / `tests/test_sync_models.py` (pure math, no
  keys/network) — run by `smoke` via `python3 -m pytest tests/` (needs pytest +
  harness deps; the `test` image stage has both).

## Test suite layout & conventions

`smoke` runs `python3 -m pytest tests/` in the `test` image stage. The suite is
layered by dependency so most of it also runs on a bare host with just pytest:

- **Host-runnable (stdlib + harness.cost only):** `test_cost`, `test_sync_models`,
  `test_providers` (model routing), `test_loaders` (optional-config IO),
  `test_import_isolation` (cost-↛-sibling acyclic guard). These import harness
  submodules via `tests/_bootstrap._load` (by file path, skipping
  `harness/__init__`) and never need keys, network, or the runtime stack.
- **Image-only (need deepagents/langchain/langgraph):** `test_agent` (workspace
  trust boundary, shell-env secret scrub, final-message extraction, AGENTS.md
  append), `test_hooks` (lifecycle hook dispatch), `test_cli` (arg parsing,
  budgets, the null=MVP cost-tracker contract). Each guards its module with
  `pytest.importorskip(...)`, so on a bare host the module is reported skipped
  instead of erroring; in the `test` image it runs.

Conventions for new tests:

- **No keys, no network, no real model calls.** Stub `create_deep_agent` /
  `subprocess.run`, monkeypatch `providers.PROVIDERS`, build throwaway provider
  registries under `tmp_path` via `providers._load_providers(dir)`.
- **All filesystem writes go to `tmp_path`** (or the `workspace_sandbox` fixture,
  which is a tmp workspace with CWD pointed at it). Nothing a test writes may
  reach the repo or the host-mounted workspace.
- A session-scoped autouse guard in `conftest.py` (`_clean_repo_artifacts`)
  diffs the `project/` tree and removes anything a test leaves behind, unless
  `DEEPAGENTS_KEEP_TEST_ARTIFACTS=1` is set (debug escape hatch). It's a backstop
  — write to `tmp_path` so it has nothing to do.
- **Every bug fix ships with a regression test.** When you fix a bug, add a test
  that fails on the old (buggy) code and passes on the fix, so the same bug — or
  the same *class* of bug where the type generalizes — can't silently re-surface
  later. Target the behavior, not the patch: assert the property that was wrong
  (the corrected output, the raised error, the no-crash), not the internals of
  the fix. Put it in the matching `tests/test_<module>.py` next to related cases.
  No fix is "done" until its test exists and the suite is green.

## Resource caps (Milestone 1)

`run-docker.{sh,ps1}` apply `--cpus` (2), `--memory` (4g), `--pids-limit` (512)
by default — a Docker host-boundary control so a runaway agent can't exhaust the
host or fork-bomb it. Override via env (`CPUS`/`MEMORY`/`PIDS_LIMIT`) in the `.sh`
or params (`-Cpus`/`-Memory`/`-PidsLimit`) in the `.ps1`. This is **not** a
sandbox — the trust boundary is still the container (`design_doc_mvp.md` §5);
don't describe it as sandboxing. Verify with `docker inspect` (`NanoCpus`,
`Memory`, `PidsLimit`).

## Gotchas

- Secrets live in `project/.env` only. Don't commit them, don't `COPY` them into the image, don't
  echo them into logs.
- `resolve_workspace` (`harness/agent.py`) uses the `DEEPAGENTS_IN_CONTAINER` env marker to detect
  the harness image — don't swap it back to filesystem sniffing (e.g. checking for `/project`).
- Conversation state persists at `<workspace>/.deepagents/checkpoints.sqlite`, keyed by
  `DEEPAGENTS_THREAD_ID`. Reuse the id to resume a thread.
- `project/suggestions/old/` is archived reference, not live code — ignore it.

## Conventions

- Keep `.ps1` and `.sh` script pairs in sync when editing one.
- Follow `rtk`-prefixed command usage per global CLAUDE.md.
