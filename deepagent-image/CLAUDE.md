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
  - `config.py` — Milestone 5 `Settings`/`resolve_settings` (every run knob, one precedence
    chain) plus the unchanged Milestone 3 `HitlSection`/`.harness-config.yaml` grammar it nests;
    see "Unified config" below.
  - `config_cli.py` — Milestone 5 `harness config` / `harness config security` keyless wizard.
    Adds no langchain/deepagents dependency of its own — see "Unified config" below.
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
- `scripts/` — `build`, `run-docker`, `verify`, `smoke`, `sync-models`, `dev-setup` in both `.ps1`
  (Windows) and `.sh`. `sync-models` is a dev-time registry refresh (see Model routing);
  `dev-setup` builds the optional host venv (see "Host dev venv" below) and is the one script
  that touches nothing in the image.
  `jail-check.py` is the odd one out: a single cross-platform Python script (no `.ps1`/`.sh` pair)
  driven by `smoke` to verify the M4 slice H bwrap jail actually holds in the built image. It lives
  here rather than in `tests/` because it only means anything when the container was started with
  `--security-opt seccomp=seccomp/userns.json`, which a test running *inside* the container cannot
  set for itself. Exit 77 = skipped (host can't nest userns), 1 = boundary regression.
- `netjail/` — opt-in deny-all-egress network jail for `run-docker` (`NET_JAIL=1` / `-NetJail`):
  the agent runs on an `--internal` network and reaches only the host ports and internet domains
  declared in `host-services.txt` / `allowed-domains.txt`. See `netjail/README.md`. Core mechanics
  (isolation, allowlist enforcement, host forwarder) verified on Docker Desktop; the proxy check
  **fails closed** (aborts rather than run with an open proxy). Default proxy image is
  `kalaksi/tinyproxy` (Docker Hub; `ghcr.io` is blocked on some networks).

## Commands (PowerShell primary on this machine)

```powershell
.\scripts\build.ps1                       # docker build -t deepagent-harness
.\scripts\verify.ps1                      # sanity-check harness venv + conda in one start
.\scripts\smoke.ps1                       # smoke test
.\scripts\smoke.ps1 -LiveModel            # + the live-model tier (real model in/out)
.\scripts\run-docker.ps1                  # opens straight to the you> prompt
.\scripts\run-docker.ps1 "your task"      # runs that task first, then drops to the prompt
.\scripts\run-docker.ps1 -WorkspacePath C:\path\to\repo "your task"
```

To iterate on the image tiers **without** a rebuild, bind-mount the tree over
`/project` — see "Fast dev loop" under "Test suite layout & conventions". It has two
mount/env gotchas whose failures masquerade as real regressions; read it before
hand-rolling a `docker run`.

`run-docker.ps1` refuses to start without `project\.env`. It bind-mounts the workspace to
`/project/workspace`, seeds missing `environment.yml` / `.gitignore` / `run-in-env.sh`, and runs
the container with `-it` so the in-container REPL (`harness/cli.py:run_repl`) has a TTY for its
`you>` prompt loop — the container stays up across turns until `/exit`/`/quit`/Ctrl-D. A redirected
stdin (CI, piped smoke runs) drops `-t` and the harness itself degrades to a single non-interactive
turn (see `docs/milestones/complete/mvp.md` §1a).

### Launcher environment (host-side, **not** `.env`)

These configure `run-docker` *before* `docker run` — they are read by the launcher script on the
host, not forwarded into the container. **Do not add them to `.env.example`**: `.env` is passed via
`--env-file` into the container and would never reach the host-side code that reads these. Set them
inline (`VAR=x ./scripts/run-docker.sh …`) on `.sh`, or via the named `.ps1` params.

See [ENV_VARS.md](./ENV_VARS.md#not-in-env--launcher-environment-host-side) for the authoritative reference of all launcher-side variables.

| Env (`.sh`) | `.ps1` param | Default | Purpose |
|-------------|--------------|---------|---------|
| `MAP_HOST_USER` | — | unset → auto | Run the container as your host uid:gid so host-owned bind mounts (state dir + workspace) are writable. `1` force on, `0` force off; **unset auto-enables on a native-Linux engine only** (not WSL/Docker Desktop/OrbStack/macOS). Fixes the turn-1 sqlite `unable to open database file` / `readonly database` crash on bare Linux. Decision logic: `scripts/lib/hostmap.sh`. **macOS:** auto never maps (host uid ≠ the daemon VM's uid). Correct for Docker Desktop/OrbStack (they squash ownership); a colima/lima config whose mount driver *preserves* ownership can still hit the crash, and `MAP_HOST_USER=1` is not a reliable fix there (maps to the macOS uid, not the VM's) — use a squashing mount driver or an in-VM chown. |
| `HOST_UID` / `HOST_GID` | — | `id -u` / `id -g` | Override the uid:gid used when mapping is active. |
| `CPUS` | `-Cpus` | `2` | `--cpus` cap. |
| `MEMORY` | `-Memory` | `4g` | `--memory` cap. |
| `PIDS_LIMIT` | `-PidsLimit` | `512` | `--pids-limit` cap (fork-bomb guard). |
| `EPHEMERAL` | `-Ephemeral` | off | Mount a throwaway copy of the workspace; revert on close. |
| `SAVE_WORKSPACE` | `-SaveWorkspace` | off | Snapshot the ephemeral copy before discard; implies ephemeral. |
| `NET_JAIL` | `-NetJail` | off | Deny-all-egress network jail (see `netjail/`). |
| `DEEPAGENTS_JAIL_APPARMOR` | — | unset → auto | AppArmor stance for the bwrap jail. Read from the host env **or `.env`**, same as `DEEPAGENTS_JAIL`, but it only affects `docker run` flags — nothing reads it inside the container. Unset → **auto**: pass nothing where no LSM is in force, select slice J's `deepagent-userns` where it is loaded, and **abort pre-flight** where an LSM is in force but the profile is not loaded. `unconfined` → `--security-opt apparmor=unconfined` (works everywhere; drops the whole profile). Any other value is passed through as a host-loaded profile name. See "AppArmor: the second gate" below. |

(`MAP_HOST_USER`/`HOST_UID`/`HOST_GID` are Linux-only mount-ownership knobs and have no `.ps1`
param — Windows is always Docker Desktop, where mounts are already squashed.)

**Ephemeral workspace (`-Ephemeral` / `EPHEMERAL=1`).** Instead of mounting the real workspace,
run-docker mounts a throwaway COPY of it (under `deepagent-image/.ephemeral/<ts>/`), so **every
change the agent makes reverts on close** — the real workspace is never touched. `-SaveWorkspace` /
`SAVE_WORKSPACE=1` snapshots the post-run copy to `deepagent-image/workspace-logs/<ts>/` before it
is discarded, and implies `-Ephemeral`. The copy **excludes `.conda`** (the rebuildable workspace
conda env) so a run doesn't clone gigabytes — an ephemeral run rebuilds the env. Harness **state**
(`checkpoints.sqlite` / `past.sqlite`, keyed to the *real* workspace path) stays persistent across
ephemeral runs; only the workspace tree is throwaway. Both output dirs are gitignored.

**Live refresh into the ephemeral copy (`harness/refresh.py`).** The copy is a point-in-time
snapshot: edits made to the *real* workspace after launch don't reach it on their own. So ephemeral
mode *also* bind-mounts the real workspace **read-only** at `/project/workspace-src`
(`DEEPAGENTS_WORKSPACE_SRC`), and exposes two ways to pull live host edits into the copy mid-run:
the **`/refresh [subpath]`** REPL command and the **`refresh_workspace(path=None)`** agent tool
(registered only when the source mount is present). Both mirror `src → workspace` **source-wins on
conflict** (the agent's in-flight edits to the *same* file are overwritten), **excluding `.conda`**,
and **do not delete** agent-created files absent from the source (pulls in, never prunes). Escaping
the workspace (`..`/absolute) is refused. The copy stays throwaway — refreshed content reverts on
close like everything else, so you can also just `cd deepagent-image/.ephemeral/<ts>/` on the host
to run tests against the agent's live state. On a normal (non-ephemeral) run the mount is absent,
`refresh.workspace_src()` is None, and both the command and tool report "unavailable" — inert.
Tests: `tests/test_refresh.py` (host-runnable, stdlib only).

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
(see `providers/README.md`). Override explicitly with `DEEPAGENTS_MODEL=provider:model`.
OpenAI-compatible providers (cursor, openrouter, lmstudio) route via `ChatOpenAI` and need their
`*_BASE_URL`. `DEEPAGENTS_PROVIDERS_DIR` overrides the registry path (used by tests).

**Auto-selection** (`choose_model`, when neither `--model` nor `DEEPAGENTS_MODEL` is set) scans by
ascending `priority` and takes the first provider that has a `default_model` **and** is *available*
(`providers.provider_available`). Two gates, both required:
- `default_model` set — lmstudio and openrouter are still deliberately unset, so they are never
  auto-picked.
- available — a **keyed** provider (`requires_key = true`) needs a non-empty `api_key_env`; a
  **keyless** one (`requires_key = false`) is always available, because there is no credential whose
  presence could signal "configured".

**`ollama` is the shipped default** (`priority = 0`, `default_model = "gemma4"`), so an unconfigured
run pins a local model instead of spending a cloud free-tier quota — which is what makes the
live-model test tier practical (see "Test suite layout & conventions"). Two consequences worth
knowing:
- Auto-selection now effectively always succeeds, so a host with no Ollama daemon fails at
  *connect* time, not with `choose_model`'s "No model configured" `SystemExit`. That exit is only
  reachable when every provider carrying a `default_model` is keyed and unkeyed.
- The `gemma4` stem is an Ollama **tag**. A locally-tagged variant (`gemma4:harnesstest1`) needs its
  own `providers/ollama/models/<tag>.toml` before it is a *known* spec; without one it still runs
  (`validate_credentials` passes unknown specs through to `init_chat_model`) but carries no rates or
  metadata.

**`[options]` — client kwargs from the registry.** `provider.toml` and
`models/<model>.toml` can each carry an `[options]` table whose keys are passed verbatim to the
chat-model constructor; `DEEPAGENTS_MODEL_OPTIONS="num_ctx=131072,temperature=0.2"` overrides both
(`providers.resolve_model_options`). This is what lets **one Ollama tag serve every context size** —
per-request options beat the tag's Modelfile `PARAMETER` block, so no `ollama create` per variant.
`providers/ollama/models/gemma4.toml` ships `num_ctx = 65536` because Ollama's own default is well
below what a coding agent needs and the failure mode is silent truncation, not an error.

Deliberately a generic key=value bag, **not** a typed `--num-ctx` flag: these are provider-specific
client kwargs (`num_ctx` is Ollama's; OpenAI has no such thing), so they are registry data rather
than `Settings` fields — adding a flag per option is the sprawl Milestone 5.1's field registry
exists to remove. Two behaviours: **options fail loudly** where the rate limiter degrades quietly
(a dropped `num_ctx` changes answers; a missing limiter only costs speed), and options are resolved
**once at agent build**, never per call — changing `num_ctx` makes Ollama reload the model (~15–20s,
KV cache reallocation), so varying it per turn would thrash the GPU. Full detail:
`providers/README.md` → "`[options]` — client kwargs".

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
`docs/milestones/complete/milestone1.md` §2.5). `harness/cost.py` holds the math only; it must
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
  not built** — see `docs/specs/energy.md` and `cost.py:measure_local_energy_wh`.
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

## Telemetry (Milestone 6)

Per-turn measurements become durable, decomposable and publishable. **Defaults ON**;
`DEEPAGENTS_TELEMETRY=0` in `.env` (or the shell) is the whole off switch — env-only,
no CLI flag, not persisted to the profile (`milestone6_spec.md` §7).

Two files, both in the **state dir** (`archive.state_dir`), never the workspace:

- `<state-dir>/usage.jsonl` — one JSON object per turn, `schema: 1` first, append-only.
  Tokens (fresh-input split, same as the ledger row), cost/energy, the wall-clock
  decomposition, per-tool-name call counts, how the turn ended (`outcome`, with
  `failed` derived from it), and the anomaly counters (`retry_count`,
  `context_trimmed`).
- `<state-dir>/session.json` — the per-run summary, **derived** from those records.
  `past.sqlite` stays authoritative for session totals; two files disagreeing is
  worse than one file missing.

**The placement is the point, not a detail.** Telemetry is an *audit surface* — evidence
about the agent, produced by the harness, read by a human — so the audited party must not
be able to rewrite it. Same reasoning M4 slice D applied to `denials.jsonl`. Stated
precisely: **file-tool-proof always** (pathguard + the workspace-rooted backend cannot
address the state dir), **shell-proof only under `DEEPAGENTS_JAIL=1`**. With the jail off,
a container shell can still reach it by absolute path. Do not round that up.

**Wall clock decomposes**, each component measured at its own seam and only the residual
inferred:

| Field | Seam |
|---|---|
| `model_ms`, `model_calls` | `TelemetryMiddleware.before_model` / `after_model` |
| `tool_ms`, `tool_calls`, `tool_errors` | `wrap_tool_call`, name off `request.tool_call` |
| `retry_sleep_ms`, `retry_count` | the `sleep=` wrapper `cli._invoke_resilient` injects |
| `paced_sleep_ms` | `ratelimit`'s module counter around `InMemoryRateLimiter.acquire` |
| `hitl_wait_ms`, `interrupts` | `hitl.run_interrupt_loop`'s `on_wait` observer |
| `duration_ms` | `cli.run_turn`, around the whole turn |
| residual | `duration_ms` minus the rest — the only inferred number |

Three things worth knowing before touching this code:

- **It is its own middleware, not a hook on the cost tracker.** M1 appends no tracker at
  all on an unpriced model — which is `ollama:gemma4`, the shipped default and the
  local-benchmark case. Telemetry riding on it would vanish exactly where it is wanted.
  Tokens are parsed off `usage_metadata` via `cost._latest_usage` + `cost._split_tokens`
  (use those; two parsers is how the numbers drift). Cost/energy come from the tracker
  when one exists, else **`null` — never `0.0`**, which reads as "free" and is a different
  claim.
- **The record is written from `run_turn`'s `finally`**, not `after_agent` and not the
  callers' `except` blocks. `after_agent` never fires for a turn that raises, which is the
  record an operator most wants; `run_turn` has no general `except`; and `duration_ms` has
  to bracket the HITL resume loop, which runs after `after_agent` already fired.
- **`run_turn` is also the only place the accumulator resets** (`begin_turn()`). `before_agent`
  looks like the turn boundary and is not — it fires once per *invoke*, and a turn invokes
  several times (a resilience retry; every HITL resume). A reset there silently erases the
  retry numbers and every pre-suspend tool count. `tracker.turn` has exactly this defect, which
  is why per-turn cost is a **delta against `tracker.session`** (never reset) rather than a read
  of `tracker.turn` — that also makes the per-turn costs sum to the session total by
  construction. If you add a hook here, ask which of the two it fires per.
- **`TelemetryMiddleware` is the OUTER `wrap_tool_call`** (langchain composes middleware
  order first-is-outermost), so `PauseMiddleware`'s `GraphInterrupt` / `HaltTurn` pass
  straight through it. They are control flow, not tool failures: they must not count as
  `tool_errors`, and a gated call must not be counted twice (it enters once to suspend,
  once on resume). `cli._is_control_flow` is that classifier.
- **A turn records *how* it ended, and `failed` is derived from that** — never set on its
  own. `outcome` is one of `ok` / `denied` / `budget` / `cancelled` / `aborted` / `error`,
  and `failed` is the property `outcome == "error"`. Only `error` reaches `turns_failed`;
  the summary also carries an `outcomes` map that sums to `turns`. The reason is invariant
  2a generalized: a deny, a `--max-cost` cap, a Ctrl-C and a fail-closed headless abort are
  all the harness doing what it was told, and a sweep reading `turns_failed` must not be
  measuring the operator or its own budget. `cli._turn_outcome` is the one classifier —
  a new governance exception is a line there, not a new flag.

Read access, keyless in the strong sense (no API key, no network, no model, and no runtime
stack — the route lives in `harness/entry.py`, so it never imports `cli.py`):

```bash
harness telemetry show [--run <run_id>] [--state-dir <path>]   # default: most recent
harness telemetry list [--topic LABEL] [--limit N]
harness telemetry pr-block [--run <run_id>]                    # used by open-pr.sh
```

`show` falls back to deriving from the records when `session.json` is absent (a crashed run
has records and no summary) and **says which source it used**.

**PR body:** `workflows/git-pr/open-pr.sh` builds its body into a temp file and passes
`--body-file`, appending the rendered block when a summary parses. Any failure — missing
file, bad JSON, non-zero exit, missing interpreter — leaves the body byte-identical to the
old hardcoded string and exits 0. A telemetry gap must never be why a PR does not open.

**Headless join:** `_batch_payload` carries `run_id`, `topic` and `usage_log` alongside
`thread_id` (which repeats across resumes and is therefore not the `past.sqlite` key). That
is what lets a benchmark sweep join 300 instances' stdout to the ledger.

**Tests:** `tests/test_telemetry.py` + `tests/test_scrub.py` (host), the Milestone 6 block
in `tests/test_cli.py` (image), the `open-pr.sh` cases in `tests/test_workflows.py`, plus
`tests/test_live_model.py::test_a_real_turn_produces_a_decomposable_record` — the one thing
a stub cannot check, since a stub populates `usage_metadata` however the test wrote it.

## Present / past memory (Milestone 2)

The harness keeps **two** stores in the harness **state dir** (`archive.state_dir`),
split so the past can never leak into context by accident. The state dir defaults
to `<workspace>/.deepagents/`, but `DEEPAGENTS_STATE_DIR` relocates it **out of the
workspace** — `run-docker` sets it to `/project/state` (a second, per-workspace host
mount outside the workspace bind-mount) so the agent's own file/shell tools, rooted
at the workspace, cannot read `past.sqlite` or corrupt the live `checkpoints.sqlite`.
(The past archive's isolation was previously structural only at the checkpointer
layer; this extends it to the tool layer. A future bwrap jail binding only the
workspace hides the state from the shell too.) `session.env` follows the same dir:

- **Present** — `checkpoints.sqlite`, the LangGraph `SqliteSaver` (one live
  thread, auto-loaded). `--thread-id` now defaults to a **fresh `session-<ts>`
  per run** (not the old literal `"default"`), so a new session starts fresh and
  never silently resumes yesterday's conversation. Pass a name (`--thread-id
  my-refactor` / `DEEPAGENTS_THREAD_ID`) to resume/create a named thread.
- **Past** — `past.sqlite`, an accumulating archive in its **own DB the
  checkpointer never opens**, so it is *structurally* impossible to auto-inject.
  Owned by `harness/archive.py` (stdlib `sqlite3` only; **must not import
  `providers.py`/`cost.py`** — the model, cost totals, and provider/model strings
  are passed in from `cli.py`). One `sessions` row **per run** (`run_id` PK — not
  `thread_id`, which repeats across resumes) + a full `turns` transcript.

Write path: `ArchiveMiddleware` (appended in `cli.main` next to the cost tracker,
only when enabled) taps each **completed** turn and writes it immediately, so a
crash bounds loss to the in-flight turn. At `session.end` the row is closed with
a summary (cheap LLM condense; deterministic first-prompt+turn-count fallback on
keyless/offline/failed calls) and the **Milestone 1 token/cost totals** — written
*after* the M1 session-total line prints so the ledger matches stderr, `cost_usd`
NULL on a keyless run. This makes `past.sqlite` the on-disk spend ledger §8 wants.

Read path — **recall only, never automatic** (`archive.recall(query, topic=…)`):
- `/recall [query] [--all]` — REPL command. No query lists recent sessions; a
  query stages a **marked** context slice injected into the next turn. The tap
  skips recall-marked messages, so recalled context is **never re-archived**.
- `recall_past(query, topic=None)` — agent tool (registered in `agent.py`,
  guarded by `DEEPAGENTS_ARCHIVE`) so the model can pull prior context mid-turn.
- **Continual topic** (`--topic`/`DEEPAGENTS_TOPIC`/`/topic <name>`): an explicit
  operator label (NULL = untagged/global). A tagged session's recall defaults to
  its own topic lane; `--all` widens to the whole archive. Auto-clustering of
  topics is deferred — the `topic=` seam is where a later embedding backend slots.

Knobs: `DEEPAGENTS_ARCHIVE=0` disables the archive entirely (removable contract —
delete `archive.py`+`memadmin.py`+wiring and default `--thread-id` back to
`"default"` and the harness is byte-for-byte Milestone 1). `DEEPAGENTS_TOPIC`
tags the run.

**Lifecycle admin (keyless, `harness/memadmin.py`, via `cli.dispatch`):**
`harness threads list|show|rm|prune` over `checkpoints.sqlite` and `harness past
list|show|rm|prune|topics` (with `--topic` filter) over `past.sqlite`. Deletes
are confirm-guarded — `rm`/`prune` refuse without `--yes`. This is the **manual**
retention surface; automatic/policy GC stays deferred.

**Not deepagents' native `memories/`.** This past archive is the harness's own
conversation ledger; Milestone 2 did **not** wire deepagents' built-in
`memories/` store — no dead scaffolding for it exists. If a future milestone
wants agent-authored long-term memories, that is a separate, additive store.

Tests: `tests/test_archive.py` + `tests/test_memadmin.py` (host-runnable, stdlib
only) and the M2 additions in `tests/test_cli.py`.

## Human-in-the-loop (Milestone 3)

A session can **suspend and ask a human**, then resume from the persisted
checkpoint. *One interrupt request object, three trigger sources, one human
channel* (design_doc.md §9). **Off unless `project/.harness-config.yaml` exists** —
absent, every seam below is skipped and the harness is byte-for-byte Milestone 2
(removable contract). Copy `.harness-config.yaml.example` to turn it on.

**How the config reaches the container:** `.harness-config.yaml` is host-local and
**gitignored (like `.env`), so it is NOT baked into the image** (the Dockerfile's
enumerated `COPY` list omits it, and would break the build if it required a
gitignored file). `run-docker.{ps1,sh}` **bind-mount it into `/project`** at run
time when present — `cli.main` reads `Path.cwd()/.harness-config.yaml` (CWD =
`/project`). So HITL is off unless *both* the file exists on the host *and*
run-docker mounts it; a raw `docker run` without that mount runs with HITL off.

The spec (`docs/milestones/complete/milestone3.md`) is authoritative on intent; the
code drifts from it in two deliberate places, noted below.

- **P1 resilience (`resilience.py`)** — pure, host-tested backoff/classification.
  `cli._invoke_resilient` wraps every model invoke: bounded exponential backoff
  with full jitter on retryable errors (429/5xx/connection reset; caps from
  `DEEPAGENTS_MAX_RETRIES` / `DEEPAGENTS_RETRY_BASE`), plus a one-shot
  context-overflow stopgap (shed injected/recall context, retry once — the
  pre-§7/Headroom placeholder). An exhausted error re-raises, which is the seam S4
  hooks onto.
- **S1 spine (`interrupt.py` + `hitl.py`)** — `InterruptRequest{id,kind,prompt,
  options,context,default,timeout_policy,source,meta}` with a stable uuid `id` so a
  resume binds to the right pause and the *same* keyed prompt re-surfaces after a
  mid-wait restart (the request dict is the value handed to `langgraph.interrupt()`,
  so it round-trips through `checkpoints.sqlite`). `hitl.run_interrupt_loop` drains
  a result's `__interrupt__`, resolves each via the channel (or headless policy),
  audits it, and resumes with `Command(resume=…)`. REPL presentation is cap+expand
  (`/show` expands a truncated context, §6).
- **S2 pause gate (`hitl.PauseMiddleware`)** — gates `tool.start` by the
  `autonomy_level` preset (strict gates all tools; guided/autonomous gate only on a
  `review_triggers` match). The gate reads the call off `ToolCallRequest.tool_call`
  (`{name,args,id}` — **not** top-level `tool_name`/`args`, an early bug that let
  every call run ungated under `guided`) and surfaces the tool **params** in the
  approval prompt (full args as `/show` context). On **deny**, behaviour is set by
  **`on_deny`** (config, default `halt`): `halt` raises `HaltTurn` so the turn ends
  and control returns to the prompt (`cli.run_turn` catches it, repairs the dangling
  tool_call via `update_state`, returns no reply) — no post-deny model call and no
  window for the model to re-issue the denied action in another form (a denied
  `rm -rf` was observed being bypassed as `rmdir` under `continue`); `continue` feeds
  a stop-and-report ToolMessage back and lets the ReAct loop proceed. Either way a
  `[harness] … DENIED — NOT executed` line prints (ground truth over a model that may
  falsely claim success). **Note:** `review_triggers` are phrasing-blind (a pattern
  gate is triage, not a guarantee) — for a hard destructive-action guarantee gate
  `execute` by `tool_name` or use the planned fs jail (§2). **Deviation:**
  the spec calls for a `pause` *step type* in `workflows.py`; workflow steps are
  subprocess side-effects that cannot suspend the graph in-process, so the pause is
  an `AgentMiddleware` that raises `interrupt()` instead. `review_triggers` matching
  (`config.match_triggers`, `{on,pattern}`, glob or `re:` regex) is pure/host-tested.
  The **PR gate (`session.end`) is now a blocking approval** for the gated presets
  (strict/guided): `cli._pr_approval` runs just before the `session.end` git-pr
  workflow and asks the operator to approve opening the PR (context = branch/base +
  `git log`/`diff --stat` of what would be pushed, via `/show`); a deny **skips
  git-pr entirely** (no stage/commit/push/PR). Gate logic is pure/host-tested
  (`hitl.should_gate_pr`, `hitl.make_pr_gate_request`). It is an **interactive veto
  only** — headless/non-TTY and the `autonomous` preset return True and the PR
  proceeds as before (git-pr never auto-merges, so opening it without a human is
  safe, and a stuck prompt in CI is worse; contrast the tool gate, which
  fails-closed headless because a tool call can be destructive). An EOF/Ctrl-C at
  the gate is a decline.
- **S3 ask_human (`hitl.make_ask_human_tool`)** — a Deep Agents tool the agent calls
  when *it* is blocked; raises the same interrupt, returns the human reply as the
  tool result (trusted input, §6). Registered next to `recall_past`, gated on HITL.
- **S4 system interrupts** — **`provider_error` is wired** (REPL-level retry/abort
  after P1 exhaustion, `cli._run_turn_hitl`; `switch provider` deferred — needs an
  agent rebuild). **`permission_denied` is wired, audit-only** (Milestone 4 slice D,
  `hitl.make_path_denied_handler` + `cli._should_audit_path_denials`): a path-guard
  denial (always a true workspace escape in v1 — `pathguard.py` has no floor/mask
  awareness) never suspends the graph or offers an approve choice — a real escape
  can't be a thing an operator's mis-click waves through. It surfaces on **two
  channels with different gating**: (1) an operator `[harness] path-guard DENIED — …`
  line on **stderr from `agent._resolve_path`, always, HITL or not** — off-HITL the
  only other trace is the tool-error string the *model* reads back, which it can
  quietly route around; and (2) a structured record in **`<state-dir>/denials.jsonl`**
  when HITL is on. That record deliberately does **not** go in the in-workspace
  `interrupts.jsonl`: a denial is evidence the agent tried to escape, and the
  workspace log is in-bounds for the path guard, so the agent's own file tools could
  truncate it (same isolation rationale as M2's `past.sqlite`). Closes the file-tool
  tamper path only — the shell tool is container-root-bounded, not guard-covered, so
  it can still reach the state dir until the bwrap jail. That isolation depends on
  `DEEPAGENTS_STATE_DIR` being set (`archive.state_dir` otherwise falls back to
  `<workspace>/.deepagents`, back inside the mount); both launchers set it, and
  **`harness doctor` now errors** when an in-container state dir resolves inside the
  workspace, so it is a checked property rather than a convention. The *refusal* is
  unchanged off-HITL. **`missing_price` is a
  recognized config key but not yet enforced** — it would need `cost.py` to raise an
  interrupt, which the acyclic import guard (cost imports no sibling) forbids, so it
  belongs in a separate reader middleware. Follow-up.
- **P2 headless (`cli.run_batch`, `--headless` / `DEEPAGENTS_HEADLESS`)** — one-shot:
  run the task(s) to completion, emit one JSON result on **stdout** (final message,
  thread id, tokens, cost, branch, exit code; stage markers stay on stderr). PR URL
  is not yet captured into the JSON (git-pr runs at session.end and logs it to
  stderr). Interrupts resolve by the §6 fail-closed policy.
- **S5 policy (`interrupt.headless_decision`)** — `interruption_policy` +
  per-request `timeout_policy`. Non-TTY runs fall through to `default`; an
  `approve` with no default denies (fail-closed); `strict`+`blocking` with no safe
  fall-through **aborts with `EXIT_INTERRUPT_ABORT` (42)** rather than hang. Shadow
  mode UX is the one open fork (§6) — not built.
- **S6** — PR-a (`prompt_toolkit` input) shipped earlier. **PR-b (the `choose`
  arrow-key select menu) is now wired** (`cli._arrow_select` → `ReplChannel(select=…)`):
  an inline (non-full-screen) ↑/↓ + Enter menu over a `choose` request's options,
  active only on an interactive TTY with `prompt_toolkit`. Esc/Ctrl-C (or no
  prompt_toolkit) falls back to the typed index/name path (`interpret_reply`), so
  the channel stays host-testable with a fake `select` and the non-TTY path is
  unchanged.
- **S7 audit (`audit.py`)** — appends a scrubbed record (id, kind, prompt, resolved
  value, source, `meta`, timestamps — **never the context payload**) to one of **two
  sinks**. Default: `<workspace>/.agent_telemetry/interrupts.jsonl`, git-ignored and
  git-pr-excluded — the HITL UX trail, and agent-writable. Boundary denials instead
  pass `sink=audit.denials_path(archive.state_dir(workspace))` →
  `<state-dir>/denials.jsonl`, outside the workspace mount (see S4 above). `meta` is
  scrubbed **recursively**, since it is a free dict and a nested value would
  otherwise be a blind spot around the §10 backstop.

**Budget/clock pause on interrupt (§6 "pause the clock") is not yet wired** — the M1
cost/resource caps still tick while a human is deciding; tracked as a follow-up.

Config knobs: `.harness-config.yaml` (`autonomy_level`, `review_triggers`,
`interruption_policy`, `on_deny`, `system_interrupts`); `--headless`/`DEEPAGENTS_HEADLESS`;
P1's `DEEPAGENTS_MAX_RETRIES` / `DEEPAGENTS_RETRY_BASE`.

Tests (host-runnable, stdlib/injected-fakes): `test_resilience`, `test_interrupt`,
`test_config`, `test_audit`, `test_hitl` (loop/channel/resolution), plus the P1
wiring cases in `test_cli`. `PauseMiddleware`'s field extraction + gate/deny
decision is host-tested in `test_hitl` against the real `ToolCallRequest.tool_call`
shape; only the graph-side `interrupt()`/`ask_human` suspend/resume is image-only
(exercised by smoke).

## Test suite layout & conventions

`smoke` runs `python3 -m pytest tests/` in the `test` image stage. The suite is
layered by dependency so most of it also runs on a bare host with just pytest:

- **Host-runnable (stdlib only — the default, and most of the suite):** anything
  that does not touch the runtime stack. These import harness submodules via
  `tests/_bootstrap._load` (by file path, skipping `harness/__init__`) and never
  need keys, network, or langchain. **Do not maintain a list of them** — CI runs
  `pytest tests/` with pytest and nothing else, so membership is decided by whether
  a module imports cleanly, not by an enumeration someone has to remember to
  extend. (It used to be enumerated in `ci.yml`, the list drifted, and 246 tests
  quietly stopped running in that job.) One of them polices the tier itself:
  `test_import_isolation` carries the cost-↛-sibling acyclic guard **plus** M5
  §0.1 F6's keyless-path guard — `harness`, `harness.entry`, `harness.config_cli`,
  `harness.doctor` and `harness.telemetry` must each import without pulling
  `cli`/`agent`/deepagents/dotenv.
- **Runtime-dependent (need deepagents/langchain/langgraph):** guarded with
  `pytest.importorskip(...)` so a bare host reports SKIPPED instead of erroring,
  and the `test` image runs them for real. Guard at **module** level when the whole
  file needs the stack (`test_agent` — workspace trust boundary, shell-env secret
  scrub, final-message extraction, AGENTS.md append; `test_cli` — arg parsing,
  budgets, the null=MVP cost-tracker contract; `test_hooks`), or **per test** when
  the rest of the module is pure (`test_hitl`, where only the two cases that build
  a real `ToolMessage` need `langchain_core`). Prefer per-test: a module-level guard
  over one impure case costs the host tier every other test in the file.

  **A missing guard turns the host CI job red**, by design — that is the signal
  that a new test reached for the runtime stack. Add the guard; never narrow what
  CI collects to route around it.
- **Live-model (needs a reachable model):** `test_live_model` — real prompts to a
  real model, real replies asserted. **Off unless `DEEPAGENTS_LIVE_MODEL=1`**
  (marker + gate in `conftest.py`), so the two tiers above stay hermetic and CI is
  unaffected. `smoke -LiveModel` / `LIVE_MODEL=1 ./smoke.sh` turns it on and points
  the container at a host-run daemon. Take the session-scoped **`live_model`
  fixture**: it resolves the model through the harness's own
  `choose_model` → `validate_credentials` → `resolve_chat_model` path (so a routing
  regression fails here too) and **skips** — never fails — when the stack or the
  model is unreachable.

### Host dev venv (`scripts/dev-setup.{ps1,sh}`) — optional, and deliberately so

Everything above assumes a bare host has nothing but pytest, which is exactly what
CI does (`.github/workflows/ci.yml`: `pip install pytest`, then an explicit list of
host-tier files). That property — **the suite runs with nothing installed** — is
load-bearing and does not change.

But it left no way to run anything langchain-touching *outside* Docker: the
image-only tiers (`test_cli`, `test_agent`, `test_hooks`), the keyless admin
commands this file documents as host-side, and any throwaway probe against real
middleware. The admin-commands section below has always said "requires the harness
venv: `source deepagent-image/.venv/bin/activate`" — and nothing created that venv.

```powershell
.\scripts\dev-setup.ps1              # create deepagent-image\.venv + install
.\scripts\dev-setup.ps1 -Recreate    # rebuild from scratch
```
```bash
./scripts/dev-setup.sh
./scripts/dev-setup.sh --recreate
```

Installs `project/requirements.txt` + pytest into `deepagent-image/.venv`
(gitignored). Four things to hold onto:

- **It is not a third stack.** It mirrors the *image's* harness venv (`/opt/venv`)
  from the same `requirements.txt`. The two-stack rule is untouched: harness deps
  here, the agent's deps in `<workspace>/.conda/env`, never mixed. Do not install a
  workspace dependency into it.
- **It is not required, and no guard may be dropped because it exists.** Every
  `pytest.importorskip(...)` in the image-only modules stays. Removing one because
  "langchain is installed anyway" breaks the CI job that installs only pytest, and
  breaks it silently — the tests would *error*, not skip, on a bare checkout.
- **It changes what a local `pytest tests/` means.** With langchain present the
  `importorskip` guards stop skipping, so the image-only tiers now run on the host.
  Good for feedback speed; it also means a local green no longer proves anything
  about the *image*. Same caveat the bind-mount loop below carries — a missing
  `COPY`, a stale layer, or an image-only dep is invisible from here.
- **It can drift from the image.** No lockfile, so wheels resolve to whatever is
  current for your platform and Python minor (the image is ubuntu:24.04 ⇒ 3.12; the
  script warns when yours differs). `smoke` builds clean and stays the check before
  a PR.

Related but separate, and no longer a reason to build this venv: the keyless admin
commands (`config`, `doctor`, `threads`/`past`, `telemetry`, …) used to be dragged
into langchain by `harness/__init__.py` importing `cli` unconditionally. Milestone 5
§0.1 F6 fixed that — a lazy `__init__` plus `harness/entry.py` — so they now run on
a bare interpreter. This venv is for the image-only *test tiers*, not for them.

### Fast dev loop — run the image tiers without rebuilding

`smoke` rebuilds both image targets every run, which is the right default but slow
to iterate against. Once the images exist, bind-mount the working tree over
`/project` instead: the container gets your live edits with no rebuild. **Rebuild
only when `requirements.txt` changes** — everything else is mounted.

```powershell
$w = "<repo>\deepagent-image"
docker run --rm -v "$w\project:/project" `
  -v "$w\apparmor:/project/apparmor:ro" -v "$w\seccomp:/project/seccomp:ro" `
  --add-host host.docker.internal:host-gateway `
  -e DEEPAGENTS_LIVE_MODEL=1 -e OLLAMA_HOST=http://host.docker.internal:11434 `
  deepagent-harness-test python3 -m pytest tests/ -q -ra
```

```bash
w="<repo>/deepagent-image"
docker run --rm -v "$w/project:/project" \
  -v "$w/apparmor:/project/apparmor:ro" -v "$w/seccomp:/project/seccomp:ro" \
  --add-host host.docker.internal:host-gateway \
  -e DEEPAGENTS_LIVE_MODEL=1 -e OLLAMA_HOST=http://host.docker.internal:11434 \
  deepagent-harness-test python3 -m pytest tests/ -q -ra
```

One non-obvious requirement, and one preference:

- **Mount `apparmor/` and `seccomp/` back explicitly.** The Dockerfile copies them
  *into* `/project`, but on the host they are **siblings** of `project/`
  (`deepagent-image/apparmor`, `deepagent-image/seccomp`) — so mounting `project/`
  over `/project` hides the baked-in copies. Without the two extra mounts,
  `test_apparmor` / `test_seccomp` / the AppArmor+seccomp cases in `test_doctor`
  fail on missing artifacts (11 tests). Not fixable in the tests: they are the CI
  regression guards on those profiles, so skipping when the artifact is absent
  would let a *deleted* profile pass silently.
- **Prefer leaving `DEEPAGENTS_MODEL` unset.** Not a correctness trap — every test
  that asserts default/profile resolution now scrubs it explicitly, and the suite
  is green with it set. But leaving it unset is the stronger check: auto-selection
  then has to reach `ollama:gemma4` by itself, which is what a stock run does.
  Point the live tier at another model with a per-test monkeypatch when you need to.

Drop `DEEPAGENTS_LIVE_MODEL` / `OLLAMA_HOST` / `--add-host` to run just the
hermetic tiers. `smoke` remains the authority before a PR — it builds clean, so it
catches anything the mount papers over (a missing `COPY`, a stale image layer).

Conventions for new tests:

- **Exercise the real model where the behavior under test is model behavior.**
  Stubs are deterministic *and* structurally blind: they answer however the test
  wrote them to, so a harness that is internally consistent but doesn't actually
  work reads green. Real bugs have been caught only by running a model —
  tool-calling the model won't emit, `usage_metadata` a provider omits (silently
  billing $0), a reply shape the extractor mishandles. So when a case asserts
  something that depends on what a *model* does rather than on what the harness
  does with it, add a `live_model` case alongside the stubbed one. Ollama being
  the default makes this cheap: no key, no quota, no rate limit.
  **Don't convert the existing tiers** — a stubbed test is still the right tool
  for harness logic, and the live tier is additive.
- **No keys, no network, no real cloud calls — outside the live tier.** In the
  host and image tiers: stub `create_deep_agent` / `subprocess.run`, monkeypatch
  `providers.PROVIDERS`, build throwaway provider registries under `tmp_path` via
  `providers._load_providers(dir)`. The live tier is the *only* place a real model
  call belongs, it is opt-in, and even there the model must be a local one — a
  test that needs a cloud key is a test CI can never run.
- **All filesystem writes go to `tmp_path`** (or the `workspace_sandbox` fixture,
  which is a tmp workspace with CWD pointed at it). Nothing a test writes may
  reach the repo or the host-mounted workspace.
- **To keep a file for post-run inspection, take the `artifact_dir` fixture**
  (`conftest.py` → `tests/_artifacts.py`). By default it is `tmp_path` (deleted
  with the session). Under smoke's `-KeepArtifacts` / `KEEP_ARTIFACTS=1` — which
  sets `DEEPAGENTS_TEST_ARTIFACTS_DIR` and bind-mounts a host folder at
  `/artifacts` (**outside `/project`**, so the artifact-guard leaves it alone) —
  it becomes a per-test subdir there, so files are shipped out to
  `test-artifacts/<timestamp>/` on the host and survive the disposable container.
  `tests/test_artifacts.py` exercises both modes.
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
sandbox — the trust boundary is still the container (`docs/milestones/complete/mvp.md` §5);
don't describe it as sandboxing. Verify with `docker inspect` (`NanoCpus`,
`Memory`, `PidsLimit`).

## Rate limiting / request pacing

Two layers keep a run under a provider's plan limits — one reactive, one proactive:

- **Reactive (`resilience.py`)** — on a retryable 429/5xx the backoff now **honors
  the server's own wait**: `retry_after_seconds` reads a `retry_delay`/`retry_after`
  attribute (Google `ResourceExhausted` sets `retry_delay`; OpenAI/Anthropic a
  numeric `retry_after`), a `Retry-After` header, or a `retry_delay { seconds: N }` /
  "retry in Ns" fingerprint in the message, and sleeps exactly that (capped at
  `_SERVER_DELAY_CAP_SECONDS`=120 so a huge server wait escalates to S4 instead of
  freezing). Falls back to jittered exponential backoff when the server says nothing.
- **Proactive (`ratelimit.py`)** — declares plan limits in the registry and paces
  **every** model call in the ReAct loop (not just per-turn) via langchain's
  `InMemoryRateLimiter`, attached to the model in `providers.resolve_chat_model`.
  **RPM is exact** (min interval); **TPM is best-effort** — a `tokens_per_request`
  estimate converts a tokens/min budget to a request rate, and the stricter of
  RPM/TPM binds (`effective_rps`). Because native providers return a bare model
  *string* (create_deep_agent calls `init_chat_model` itself), attaching a limiter
  means building the model object here; construction failure degrades to the unpaced
  string, never a hard error.

Config: `provider.toml` `[limits]` — top-level `rpm`/`tpm`/`tokens_per_request`,
optional per-tier `[limits.<tier>]` blocks. **Inert until a tier is selected**:
set `tier` in the TOML or `DEEPAGENTS_PROVIDER_TIER` at run time. `DEEPAGENTS_RPM` /
`DEEPAGENTS_TPM` / `DEEPAGENTS_TOKENS_PER_REQUEST` override the numbers (and can pace
a provider that ships no `[limits]` at all). No tier + no env = no pacing (byte-for-byte
prior behaviour — the removable contract). `google_genai/provider.toml` ships
`free`/`tier1` ballparks; **confirm against your own console** (limits change and
differ per model). Note a tight free tier (e.g. 15k TPM) paces a coding turn to
~1 call/minute — that is the tier's real ceiling, surfaced instead of 429-thrashed.

See [ENV_VARS.md](./ENV_VARS.md#rate-limiting--request-pacing) for rate-limit environment variables.

Tests: `tests/test_ratelimit.py` (pure math + tier/env resolution), the
`retry_after_*` cases in `tests/test_resilience.py`. The actual limiter attachment
is image-only (smoke).

## Gotchas

- Secrets live in `project/.env` only. Don't commit them, don't `COPY` them into the image, don't
  echo them into logs.
- **The agent's shell tool sees an env allowlist, not the harness env** (`_agent_shell_env`,
  `harness/agent.py`): PATH/HOME/CONDA_*/MAMBA_*/GIT_*/locale + common shell vars pass; everything
  else — provider keys and any other var — is withheld so a prompt-injected agent can't `printenv`
  a credential onto the host-mounted workspace. It's an allowlist, not a secret-suffix denylist —
  adding a feature that needs a new var in the *agent's* shell means adding it to
  `_SHELL_ENV_ALLOW_EXACT`/`_SHELL_ENV_ALLOW_PREFIXES`, or telling the user to set
  `DEEPAGENTS_SHELL_ENV_ALLOW` (comma/space list; trailing `*` = prefix). Host-side workflow
  steps (git-branch/git-pr) use the full env via `GateContext.as_env`, so `GH_TOKEN` etc. still
  reach them — this allowlist only narrows the *agent's* shell.
- `resolve_workspace` (`harness/agent.py`) uses the `DEEPAGENTS_IN_CONTAINER` env marker to detect
  the harness image — don't swap it back to filesystem sniffing (e.g. checking for `/project`).
- Present conversation state persists at `<state-dir>/checkpoints.sqlite` (state dir =
  `archive.state_dir`: `<workspace>/.deepagents/` by default, or `DEEPAGENTS_STATE_DIR` when set —
  `run-docker` points it at `/project/state`, outside the agent's workspace root), keyed by
  the present `thread_id`. Since Milestone 2 the id defaults to a **fresh `session-<ts>` per run**
  (fresh context by default); set `--thread-id`/`DEEPAGENTS_THREAD_ID` to a prior id to resume that
  thread. The separate `past.sqlite` archive beside it is **never** opened by the checkpointer (see
  "Present / past memory" above) — don't conflate the two DBs.
- `project/suggestions/old/` is archived reference, not live code — ignore it.
- **A failed turn never kills the session.** `run_repl` catches any turn exception
  (not just `KeyboardInterrupt`/`BudgetExceeded`) — e.g. a transient provider 5xx
  surfaced out of `agent.invoke`. Interactive: report `[harness] turn failed: …` and
  drop back to the prompt for a retry (staged `/recall` slice is dropped). Non-TTY:
  report and close cleanly (rc 0) so `main()` still finalizes the archive row. Set
  `DEEPAGENTS_DEBUG=1` to also dump the turn's partial checkpointer state on failure
  (accumulated AI reasoning / tool calls / tool results from earlier super-steps, via
  `cli._dump_partial`); off by default. A pre-generation failure (e.g. a 500 that
  fails before the model emits anything) legitimately shows no new state.
  **No built-in way to see the raw prompt/response on a successful turn** (system
  prompt, full message history, tool schemas, tool-call/result blocks, as literally
  sent to/from the model) — `DEEPAGENTS_DEBUG` is failure-only and checkpointer-state,
  not raw wire text. Today's workaround for a local model: run the model server with
  its own debug logging (e.g. `OLLAMA_DEBUG=1` + `ollama serve` in a foreground
  terminal) and watch that. A `DEEPAGENTS_RAW_TRACE` mode is proposed in
  `design_doc.md` §11 (Framework Enhancements) — not built.
- **NetJail is deny-all by default.** When you implement a feature that needs a host service
  (e.g. a daemon on the Docker host) or internet access (a model API, package registry, git
  remote), you MUST also grant it in the jail or it silently breaks under `NET_JAIL=1` /
  `-NetJail`: add a `<name> <port>` line to `netjail/host-services.txt` for a host service, or
  the bare domain to `netjail/allowed-domains.txt` for an egress destination. Anything not
  listed is blocked. Don't widen the allowlist beyond what the feature needs.

## Workspace visibility / secret masking (Milestone 4)

> **Status: in-progress** — code on `feat/milestone_4`, slices A–H landed
> (H opt-in). Full spec in `docs/milestones/in-progress/milestone4.md`.

The harness can enforce a trust boundary on the workspace filesystem:

**Config knobs** (set in `project/.env` or at container runtime):
- `DEEPAGENTS_MASK` (default 1): Enable/disable the masking scan and empty-overlay mounts.
  Set to 0 for Milestone 3 parity (byte-for-byte unchanged).
- `DEEPAGENTS_MASK_MODE` (default "deny"): Visibility mode.
  - "deny": Agent sees everything except masked paths (present-but-empty).
  - "allow": Agent sees only allow-listed paths (requires `.agentignore` to opt paths in).
- `DEEPAGENTS_AGENTIGNORE` (default ".agentignore"): Override the in-workspace config filename.
- `DEEPAGENTS_JAIL` (default **0, off**): Route all fs tools + the shell through a bubblewrap jail
  (slice H). See "bwrap fs jail" below — enabling it also requires the narrow seccomp profile.
- `DEEPAGENTS_NS_GUARD` (default: **tracks `DEEPAGENTS_JAIL`**): shell-tool denylist for the namespace
  syscalls the jail's seccomp profile re-permits container-wide. `warn` = record without refusing,
  `0` = off, `1` = force on with the jail off. See "bwrap fs jail" below.

### bwrap fs jail (slice H, opt-in)

`DEEPAGENTS_JAIL=1` makes the harness **re-exec itself into a bwrap mount namespace** at startup
(`harness/jail.py`). Every tool in the process then inherits it — the in-process deepagents file
tools included, with upstream code running untouched. The shell tool goes one further into a
**nested** jail (`scripts/sandbox-exec.sh`) that binds only the workspace.

What it buys over the docker mask alone:
- **The floor gets an independent second enforcer.** Masked/floor paths are overmounted empty
  *inside* the jail, so they stay unreadable even if the docker overlay were disabled or
  misconfigured (milestone4 invariant 5 leg 4).
- **The shell can no longer reach the state dir.** Previously `cat /project/state/denials.jsonl`
  worked from the shell tool — and so did truncating it, i.e. the agent could destroy the record of
  its own escape attempt. Under the jail the shell sees only `workspace` (invariant 17a).
- `/project` is bound **read-only**, so the agent cannot write to harness code.

**Why it is off by default.** Docker's default seccomp profile blocks unprivileged user-namespace
creation, so the jail needs `--security-opt seccomp=deepagent-image/seccomp/userns.json` — Docker's
default with exactly five syscalls relaxed (`clone`, `unshare`, `mount`, `umount2`, `pivot_root`).
That relaxation grants no privilege (the kernel still enforces capabilities) but does expose kernel
userns attack surface, so turning it on is a deliberate trade. See `seccomp/README.md`.

**The relaxation is container-wide, and `harness/nsguard.py` is the backstop.** `--security-opt` is a
`docker run` flag — it cannot be scoped to the harness process — so enabling the jail hands the
*agent's shell* those same five syscalls. `nsguard` denylists the common routes at the shell seam:
`unshare`/`nsenter`/`mount`/`chroot`/`bwrap`/container runtimes in command position (basename-matched,
sees past `sudo`/`timeout` wrappers, scans every `&&`/`;`/`|` segment), plus `CLONE_NEW*`, `unshare(`,
`setns(`, raw `syscall(272|155|308)` anywhere — the interpreter one-liner route. A hit is refused,
printed to stderr regardless of HITL, and (HITL on) recorded to `<state-dir>/denials.jsonl` with the
**match and reason only, never the command string** (a command can carry workspace content).
Defaults on exactly when the jail is on, so the removable contract is untouched;
`DEEPAGENTS_NS_GUARD=warn` records without refusing, `=0` disables, `=1` forces it on with the jail off.

**It is a tripwire, not containment — never describe it as sandboxing.** A string denylist is
phrasing-blind (the same caveat as `review_triggers`, where a denied `rm -rf` came back as `rmdir`);
anything compiled, base64-decoded, or indirected passes. The value is that a casual escape attempt is
refused and leaves evidence instead of silence.

`run-docker.{ps1,sh}` pass the profile automatically when `DEEPAGENTS_JAIL` is on, and **fail closed**
if it is missing rather than launching unjailed. They also forward `-e DEEPAGENTS_JAIL=1` from inside
that same block: `jail.jail_enabled()` reads the **environment**, not `Settings`, so a value resolved
from the `-Jail` flag / host env / profile tier would otherwise apply the seccomp relaxation and
never start the jail — five syscalls relaxed container-wide, no bwrap re-exec, and `nsguard` (which
defaults to tracking `DEEPAGENTS_JAIL`) off too, i.e. strictly worse than running with the jail off.
The relaxation and the jail turn on together or not at all; `check-parity` guards the pair. `harness doctor` verifies the profile is still
narrow and probes whether bwrap can actually unshare here.

Regenerate the profile with `python3 -m harness seccomp-sync` (dev-time, needs network);
`tests/test_seccomp.py` is the CI regression guard against a widened or unconfined profile (it
asserts the committed artifact, so the guard runs in the ordinary host tier).

### AppArmor: the second gate (slice J)

seccomp is only **one of two** gates, and both must allow. On Ubuntu/Debian Docker — most Linux
container hosts — Docker also applies a generated `docker-default` AppArmor profile whose literal
`deny mount,` blocks bwrap at its first mount, *after* `unshare` has already succeeded. No seccomp
change affects this, and entering a user namespace does not shed AppArmor confinement, so the jail
cannot work around it from inside. The fingerprint is
`bwrap: Failed to make / slave: Permission denied` (contrast a seccomp/userns refusal, which fails
earlier with `No permissions to create new namespace`); `jail.classify_bwrap_failure` tells them
apart so preflight, `doctor`, and the smoke gate all name the right cause.

**Slice J is the fix, and it is built** (`harness/apparmor.py` + `apparmor/deepagent-userns`): moby's
`docker-default` with **only** its `deny mount,` narrowed to the seven mount rules bwrap performs.
Everything else in the profile survives byte-for-byte, asserted by `apparmor.verify_profile` →
`tests/test_apparmor.py` → CI. See `apparmor/README.md`.

It needs a one-time host step, because unlike a seccomp profile an AppArmor profile is not a file you
hand to `docker run` — it must be compiled into the **host kernel** first, on whichever machine runs
`dockerd`:

```bash
sudo deepagent-image/scripts/install-apparmor-profile.sh          # load (enforce)
     deepagent-image/scripts/install-apparmor-profile.sh --status # loaded? which sha?
```

`run-docker` then selects it automatically: it asks the daemon what confines a container, and only
if an LSM is in force asks whether `deepagent-userns` is loaded. Not loaded ⇒ it **aborts before
`docker run`** with the install command. It never falls back to `unconfined` on its own.

**⚠️ Built, but not yet measured on an AppArmor host.** Every machine this was developed on is
Docker Desktop/WSL2, which loads no AppArmor policy — the same blind spot that let slice H ship
claiming more reach than it had. So the mount rule set is *derived from bwrap's syscall sequence,
not confirmed against a live denial log*. Treat `DEEPAGENTS_JAIL=1` on Ubuntu/Debian as untested
until a run on such a host is recorded (CI's non-gating `apparmor-load-probe` job exists to produce
exactly that record). If it denies something, read `dmesg | grep 'apparmor="DENIED"'` and add **only**
the rule the denial demands, with a justification in `apparmor/README.md` — a broad `mount,` catch-all
is `unconfined` in disguise and `verify_profile` rejects it.

Other options:
- **`DEEPAGENTS_JAIL_APPARMOR=unconfined`** — works on any host, but drops the **whole**
  `docker-default` profile, not just its deny-mount rule. Wider than the five relaxed syscalls
  `DEEPAGENTS_JAIL` alone costs, so it is opt-in, never a launcher default, and both `run-docker` and
  `doctor` say what was given up.
- **Any other value** — passed through as a host-loaded profile name.
- Docker Desktop/WSL2 loads no AppArmor policy, so nothing is needed there.

Regenerate with `python3 -m harness apparmor-sync` (dev-time, needs network); `--check` verifies the
committed artifacts offline. **SELinux hosts (RHEL/Fedora) are a third environment and are untested**;
rootless Docker and Podman likewise.

### Quick-Start: In-Workspace `.agentignore` File

The `.agentignore` file (gitignore syntax) in your workspace root controls which paths 
the agent sees. Default globs (`.env`, `*.pem`, `.aws/credentials`, etc.) are **always 
masked** — even `.agentignore` cannot unmask them.

**Example 1: Deny mode (default) — Agent sees everything except masked paths:**
```
# .agentignore (in workspace root)

# Mask additional sensitive files
private/notes.md
config/secrets.yaml

# Unmask a default glob (deny mode only):
!.env.local                    # this .env variant is readable

# Designated-secret floor (can never be negated):
#!floor:
id_rsa
.ssh/
~/.aws/credentials
#!floor-end
```

**Example 2: Allow mode — Agent sees *only* what you explicitly allow:**
```
# .agentignore with #!mode:allow

#!mode:allow

# Paths the agent CAN see — PLAIN patterns, not negation. In allow mode a
# plain pattern match IS the allow-list entry (the resolver flips it visible);
# `!`-negation has no special meaning here and does NOT allow-list a path.
src/
tests/
README.md
package.json

# Everything else is hidden (including defaults like .env)
```

**Defaults (always masked, can't be unmasked):**
```
.env, .env.*, *.pem, *.key, id_rsa, id_ed25519, .ssh/, .aws/credentials, .netrc,
.npmrc, .git-credentials, credentials.json, *.p12, *.pfx
```

See `docs/features/workspace_visibility.md` (§3, full syntax) for advanced rules.

**Removable contract:** Set `DEEPAGENTS_MASK=0` and the harness behaves byte-for-byte like M3.

## Unified config (Milestone 5)

Every run knob — model, budgets, HITL posture, mask/jail/security posture, resource caps —
resolves through **one precedence chain** instead of scattered env-var/`.env` edits:
`CLI flag > env var (shell-exported or `.env`, dotenv-loaded before anything else runs) >
profile file (`project/.harness-profile.yaml`) > built-in default`. `.env` keeps its role as
the baseline record of defaults when nothing else overrides it.

- **`harness/config.py`** — the resolver module. `resolve_settings()` returns a
  `(Settings, SettingsSources)` pair (`Settings` covers every knob; `SettingsSources` tags each
  field's provenance as `"cli"`/`"env"`/`"profile"`/`"default"`). Also still holds the unchanged
  Milestone 3 HITL grammar — `HitlSection` (was `Config`) + `.harness-config.yaml` parsing —
  nested onto `Settings.hitl`, since the two **on-disk files stay separate** (the HITL parser is
  a tested, non-trivial grammar not worth risking in a merge); only the Python API is unified.
  `LIVE_FIELDS` (`model`, `thread_id`, `topic`, `max_cost`, `max_tokens`, `hitl`) is the single
  source of truth for the split below — both `/config` and `harness doctor` filter on it.
- **The pre-spinup / in-session split** (why this isn't one flat list): mask mode, jail/AppArmor,
  resource caps, and NetJail are Docker startup flags — fixed the moment `docker run` executes,
  no amount of in-session UI changes that. Model, budgets, HITL posture, and topic are read by
  the harness process itself and can change any time before the next model call.
  | Fixed at container start (host side) | Live in-session (container side) |
  |---|---|
  | Mask mode, jail/AppArmor, resource caps, NetJail | Model, budgets, HITL posture, topic |
- **Pre-spinup, host side:**
  - `run-docker.{ps1,sh}` gain `-Model`/`-MaskMode`/`-Jail`/`-JailApparmor` (`.ps1`) /
    equivalent env vars (`.sh`, which already treats `VAR=x ./run-docker.sh` as its "flag"
    mechanism), each resolving through `scripts/lib/config.{ps1,sh}`'s
    `Resolve-HostSetting`/`_resolve_host_setting` — the same four-tier precedence, since a
    Docker flag has no way to read `Settings` directly. The **resource caps + NetJail**
    (`-Cpus`/`-Memory`/`-PidsLimit`/`-NetJail`, `CPUS`/`MEMORY`/`PIDS_LIMIT`/`NET_JAIL`) go
    through the same resolver, so a `cpus:`/`net_jail:` saved by `harness config security`
    actually reaches `docker run` — the `.ps1` defaults are `""`, not the literal caps, because
    a literal default there would shadow the profile tier and make a saved cap unreachable.
    `-NetJail` is a `[switch]`, so it consults the lower tiers only via
    `$PSBoundParameters.ContainsKey`, the one way to tell "not passed" from `-NetJail:$false`.
    `-Autonomy`/`AUTONOMY` is shaped
    differently: the HITL preset isn't a `Settings`/profile field (it's a whole-file swap-in --
    `.harness-config.yaml`'s presence *is* the on/off switch), so it's a plain-text write/update
    of the `autonomy_level:` line (creating the file if absent), not a four-tier resolve --
    necessarily turns HITL on for the run if it wasn't already.
  - **`harness config`** (keyless, `harness/config_cli.py`) — the pre-spinup wizard: model +
    security posture + HITL preset, then confirms (or `--save` skips the prompt) before writing
    `.harness-profile.yaml`. `harness config show` prints the resolved config with no prompts;
    `harness config set <field> <value>` is a one-shot, non-interactive write (validated by
    round-tripping through the profile parser, rolled back on a bad value).
    **`harness config security`** is the same wizard with the model/HITL screens skipped, plus
    two quick-edit loops: `.agentignore` (add a masked path or a floor entry, appending to the
    workspace's `.agentignore` -- a convenience wrapper, not a new masking mechanism, M4 still
    owns that file's format) and NetJail allowlists (add/delete entries in
    `netjail/host-services.txt` / `netjail/allowed-domains.txt` in place, preserving comments --
    resolved relative to `config_cli.py`'s own path since `netjail/` isn't copied into the image,
    so this only works run on the host, same pre-spinup context every other knob here assumes).
    It adds **no langchain/deepagents dependency of its own** (stdlib + `harness.config` +
    `harness.providers` only) -- that is why it is a separate module from `cli.py`. Since
    §0.1 F6 that also means what it sounds like: **the wizard runs on a host with no runtime
    stack installed.** `harness/__init__.py` resolves `main` through a lazy `__getattr__`
    instead of importing `cli` eagerly, and subcommand routing lives in the stdlib-only
    `harness/entry.py`, so `python3 -m harness config` / `doctor` never import `cli.py`.
    Both halves were needed -- a lazy route inside an eager module is still eager.
    Guarded by `tests/test_import_isolation.py`.
    Both write paths (`set` and the wizard) **refuse to run from a cwd with no `providers/`
    directory** and name the right one: they write `Path.cwd()/.harness-profile.yaml`, so run
    from the repo root they'd produce a profile `run-docker` never mounts and report success.
- **In-session, `/config`** (REPL, always in the slash menu): `/config` shows the resolved
  config, source-tagged, live fields first then the pre-spinup half read-only; `/config set
  <field> <value>` edits one live field (`model` rebuilds the agent through the same
  `validate_credentials` + `build_agent` path `main()` uses at startup, so a bad model fails the
  same way live as at launch; `hitl.autonomy_level`/`hitl.on_deny`/`hitl.interruption_policy`
  mutate the live `HitlSection` `PauseMiddleware` already holds a reference to, via
  `object.__setattr__` since it's a frozen dataclass — `PauseMiddleware` reads these fields live
  off that object rather than caching them at construction, specifically so this takes effect on
  the next gated call with no rebuild); `/config save` persists the session's edited live fields
  to the profile. Attempting to `/config set` a pre-spinup field is refused with a pointer to
  `harness config` — it can't change without a container restart. `/config set model` also
  **re-points the cost tracker** at the new model's rates (`CostTrackerMiddleware.reprice`) —
  it caches pricing/rates/name at construction, so without that every post-switch turn would be
  billed at the launch model's rates. A session launched on an unpriced model has *no* tracker
  (M1's null=MVP contract) and one can't be added mid-session without under-counting the run, so
  the switch says cost tracking stays off rather than starting a half-accurate one.
  `/config set topic` and `/config set model` also **re-tag the run's `past.sqlite` row**
  (`archive.set_topic` / `archive.set_model`) — `/topic` always did, and a knob reachable two ways
  has to persist the same way, or `harness past list --topic` files the run in the wrong lane and
  the ledger attributes every post-switch turn to the launch model. A bare `/config set topic`
  (no value) **clears** it — it is the one nullable live field. `/config save` **refuses** when no
  profile is mounted (detected in-container by the file's absence: `run-docker` mounts only `if
  exists`), because that write lands in the `--rm` layer and is lost on exit — a success message
  for a write that cannot persist is worse than a refusal, and the file wouldn't change this run
  either. An unwritable target (read-only `/project` under `DEEPAGENTS_JAIL=1`, where
  `save_profile`'s in-place fallback raises too) is reported, not raised; and the whole `/config`
  dispatch is wrapped so no subcommand can end the session — the same rule the turn handler
  follows.
  The source tags come from the `(Settings, SettingsSources)` pair `parse_args()` resolved
  **with the CLI tier applied**, threaded through `run_repl` — re-resolving inside `/config`
  would report every flag-set field as env/profile/default.
- **`harness doctor`** reports one resolved-config summary line built from `resolve_settings()`
  with no CLI override (reflecting what an *unflagged* run would do), ahead of its other checks.
- **File ownership** (kept deliberately separate, not merged): `.harness-config.yaml` = HITL
  only (unchanged since M3); `.harness-profile.yaml` = everything else this milestone adds.
  Neither is baked into the image (gitignored like `.env`); copy the checked-in `.example`
  template to activate. **Both are bind-mounted into `/project` by `run-docker` when present** —
  that is the only way they reach the container, and without the profile mount the container's
  `resolve_settings()` would see no profile tier at all (its in-session fields silently ignored,
  `/config save` writing into the throwaway container layer). The HITL mount is read-only; the
  **profile mount is read-write**, because `/config save` has to land on the host. A single-file
  bind mount can't be replaced by rename, so `save_profile` falls back from its atomic
  tmp+`replace` to an in-place write on `OSError`.
- **Host-only knobs are forwarded for display**: `--cpus`/`--memory`/`--pids-limit`/NetJail are
  `docker run` flags, never env vars, so `run-docker` also passes them as `-e CPUS=…` etc.
  purely so `/config`'s read-only half and `harness doctor` report what the launch actually
  applied instead of the built-in defaults. Nothing in the container acts on them.
- **Removable contract:** no `.harness-profile.yaml` present, no new CLI flags passed ⇒ every
  knob resolves exactly as it did pre-M5 (env var → built-in default). Delete
  `config_cli.py` + the profile-file branch inside `config.py`'s resolvers +
  `scripts/lib/config.{ps1,sh}` (reverting `run-docker` to direct env reads) and the harness is
  byte-for-byte pre-Milestone-5.

See `docs/milestones/complete/milestone5.md` §0.2 for the full write-up, and the rest of that doc
for the design/rationale.

### The field registry (Milestone 5.1) — one declaration per knob

M5 unified how a knob *resolves*; it did not unify how a knob is *declared*. Adding one was a
ten-site edit across `config.py`/`cli.py`/`config_cli.py` where nine sites failed **silently**
(miss `LIVE_FIELDS` and the field is classified pre-spinup; miss `_PROFILE_WRITE_ORDER` and
`/config save` drops it; miss `_CONFIG_SETTABLE_FIELDS` and `/config set` calls it unknown).
`config.FIELD_SPECS` is now the single declaration and everything derives from it.

**Adding a config field = one `FieldSpec` entry** — plus, for a field settable in-session, one
entry in `cli._LIVE_APPLIERS`. Nothing else. A test fails if you hand-write anything derivable.

What derives: `PROFILE_FIELDS` + the profile write order + each field's file-parse strategy (from
its `cast` — the four `_PROFILE_{BOOL,FLOAT,INT,STR}_FIELDS` bucket sets are gone), `LIVE_FIELDS`,
`resolve_settings`'s loop, `config.format_config_lines` (the **one** renderer both
`cli._config_display_lines` and `config_cli.format_settings_lines` now wrap), `/config set`'s
settable/pre-spinup/nullable lists and its validators, and `harness config`'s custom-posture screen
(`WIZARD_PRESPINUP_SPECS` — a new persisted pre-spinup knob gets a wizard question for free).

Two things stay hand-written on purpose, each with a coverage test instead:
- **The appliers** (`cli._LIVE_APPLIERS`) — an applier mutates the `CostTrackerMiddleware`, the
  archive connection, and the rebuilt agent, and `config.py` imports none of those. `FieldSpec.settable`
  is the declaration; the map is the behaviour; a test asserts they match **both ways**.
- **The launchers** — `run-docker.{ps1,sh}` need no host Python (deliberate), so they resolve each
  pre-spinup knob in pure shell. `test_prespinup_profile_keys_are_consumed_by_both_launchers`
  asserts every persisted pre-spinup key appears in both, which replaced the two hand-picked
  `pids_limit`/`net_jail` markers in `check-parity`.

**`choices` is enum-only, never on a bool.** It means "exactly these strings are legal" and drives
validation at *every* tier, so putting `("off","on")` on `jail` would reject `DEEPAGENTS_JAIL=1` —
the spelling both launchers pass. Bools get their off/on wizard menu from `cast is _to_bool`
instead. `test_registry_entries_are_internally_coherent` pins this.

**Enum values are now validated at every point of entry** (the one behavior change M5.1 makes, and
the fix for M5's known `mask_mode` gap): `harness config set mask_mode alow` exits 1 and writes
nothing, a hand-edited `mask_mode: alow` in the profile fails loudly at load, and
`DEEPAGENTS_MASK_MODE=alow` fails at resolve. Previously all three persisted and silently resolved
to the *opposite* mode (`mask.resolve` compares `mode == MODE_ALLOW` and takes the `else`) — fail-safe,
but with a success message.

**`/config set <enum-field>` with no value opens an arrow-key picker** over that field's `choices`
(`cli._arrow_select`, widened from M3's `choose`-request menu). Esc, no `prompt_toolkit`, or a
non-TTY falls back to the unchanged `usage: /config set <field> <value>` error. Free-text fields
(`model`, `topic`, …) have no picker — a picker over arbitrary prior strings is a different feature.

Full write-up: `docs/milestones/in-progress/milestone5.1.md`; the checkable properties (and which
test pins each) are in `milestone5.1_invariants.md` beside it.

## Interactive REPL Commands (in-container)

When a session is running, type these at the `you>` prompt:

| Command | Purpose | Example |
|---------|---------|---------|
| `/exit` | End session deterministically (no LLM involved) | `/exit` |
| `/quit` | Alias for `/exit` | `/quit` |
| `Ctrl-D` | EOF — end session | — |
| `Ctrl-C` | At idle prompt: end session. During turn: cancel that turn. | — |
| `/topic <name>` | Set or change the continual-topic label for this run (defaults to DEEPAGENTS_TOPIC) | `/topic refactor-auth` |
| `/recall [query] [--all]` | Recall past sessions. No query lists recent. Query stages context for the next turn. `--all` widens to whole archive. | `/recall authentication issue` |
| `/show` | Expand a truncated interrupt/approval context (HITL) | (used when an approval prompt is capped) |
| `/refresh [subpath]` | Pull live host edits into ephemeral workspace copy (ephemeral mode only). Omit subpath to refresh root. | `/refresh src/` |
| `/config` | Show resolved config (source-tagged); `set <field> <value>` edits one live field; `save` persists session edits to the profile | `/config set model openai:gpt-5.5` |

## Admin Commands (keyless, outside container)

Run these in the host shell; they manage the persistent harness state:

```bash
# List or manage threads (present conversation)
harness threads list [--topic LABEL]
harness threads show <thread-id>
harness threads rm <thread-id> [--yes]      # requires --yes to confirm
harness threads prune [--keep N] [--yes]    # keep N most recent, delete rest

# List or manage past sessions (archive)
harness past list [--topic LABEL]
harness past show <run-id>
harness past rm <run-id> [--yes]
harness past prune [--keep N] [--yes]
harness past topics                         # list all topics

# Unified config wizard (Milestone 5) -- pre-spinup knobs, writes .harness-profile.yaml
harness config                              # full interactive wizard, then confirms save
harness config show                         # print resolved config, no prompts
harness config set <field> <value>          # one-shot, non-interactive
harness config security                     # security-only wizard + .agentignore/NetJail quick-edit

# Run telemetry (Milestone 6) -- reads <state-dir>/usage.jsonl + session.json
harness telemetry show [--run <run-id>] [--state-dir <path>]
harness telemetry list [--topic LABEL] [--limit N]
harness telemetry pr-block [--run <run-id>]  # the markdown block open-pr.sh appends
```

"Keyless" here means no API key, no network and no model — **and, since M5 §0.1 F6
landed, no runtime stack either.** `entry.dispatch` routes each of these to its
stdlib-only module without importing `cli`, and `harness/__init__.py` resolves
`main` through a lazy `__getattr__`, so `python3 -m harness telemetry show` runs on
a bare interpreter with nothing installed. `tests/test_import_isolation.py` pins
that for `harness`, `harness.entry`, `harness.config_cli`, `harness.doctor` and
`harness.telemetry` — adding a package-level import to any of them fails there.

The host dev venv (`scripts/dev-setup.{ps1,sh}`) is therefore **not** required for
these commands any more. It is still what lets the *image-only test tiers* run
outside Docker — see "Host dev venv" above.

## Feature Toggles & Removable Contracts

Each milestone adds opt-in or removable features. This table shows which are on by default, 
how to disable them, and what behavior they enable/disable:

| Feature | Milestone | Env Var | Default | When to Set to 0 | Behavior When Off |
|---------|-----------|---------|---------|------------------|------------------|
| Past Archive | M2 | `DEEPAGENTS_ARCHIVE` | 1 | Never; use `/recall` when you don't need past runs | Sessions not recorded in `past.sqlite`; `/recall` returns nothing |
| Workspace Masking | M4 | `DEEPAGENTS_MASK` | 1 | Testing/debugging; or trusting the workspace | Agent can read all files; no empty overlays |
| Cost Tracking | M1 | n/a (auto) | on | Never; budgets are optional | No per-turn usage line; budgets ignored |
| HITL | M3 | n/a (config file) | off | (not set by env) | Only if `.harness-config.yaml` exists in project root | No approval gates; agent runs freely |
| Unified Config profile | M5 | n/a (`.harness-profile.yaml`) | off (no file) | Never; hand-edit `.env`/flags for a one-off | No `.harness-profile.yaml` present ⇒ every knob resolves exactly as it did pre-M5 (env var → default) |
| Telemetry | M6 | `DEEPAGENTS_TELEMETRY` | 1 | Rarely — the run you want telemetry for is the one you did not expect to go wrong | No `usage.jsonl`, no `session.json`, no PR block, no middleware appended, no new stderr line |

**Removable contract:** Each "off" state is byte-for-byte identical to the prior milestone 
(see [Glossary](../docs/README.md#glossary)). E.g., `DEEPAGENTS_MASK=0` ⇒ M3 parity.

## Conventions

- Keep `.ps1` and `.sh` script pairs in sync when editing one.
- Follow `rtk`-prefixed command usage per global CLAUDE.md.
