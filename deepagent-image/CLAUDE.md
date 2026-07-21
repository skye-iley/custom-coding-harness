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
.\scripts\run-docker.ps1                  # opens straight to the you> prompt
.\scripts\run-docker.ps1 "your task"      # runs that task first, then drops to the prompt
.\scripts\run-docker.ps1 -WorkspacePath C:\path\to\repo "your task"
```

`run-docker.ps1` refuses to start without `project\.env`. It bind-mounts the workspace to
`/project/workspace`, seeds missing `environment.yml` / `.gitignore` / `run-in-env.sh`, and runs
the container with `-it` so the in-container REPL (`harness/cli.py:run_repl`) has a TTY for its
`you>` prompt loop — the container stays up across turns until `/exit`/`/quit`/Ctrl-D. A redirected
stdin (CI, piped smoke runs) drops `-t` and the harness itself degrades to a single non-interactive
turn (see `docs/milestones/complete/mvp.md` §1a).

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
  agent rebuild). **`missing_price` and `permission_denied` are recognized config
  keys but not yet enforced** — `missing_price` would need `cost.py` to raise an
  interrupt, which the acyclic import guard (cost imports no sibling) forbids, so it
  belongs in a separate reader middleware; `permission_denied` rides on the §2/§10
  path-guard/NetJail gates. Both are follow-ups.
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
  value, source, timestamps — **never the context payload**) to
  `<workspace>/.agent_telemetry/interrupts.jsonl`, git-ignored and git-pr-excluded.

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
- **NetJail is deny-all by default.** When you implement a feature that needs a host service
  (e.g. a daemon on the Docker host) or internet access (a model API, package registry, git
  remote), you MUST also grant it in the jail or it silently breaks under `NET_JAIL=1` /
  `-NetJail`: add a `<name> <port>` line to `netjail/host-services.txt` for a host service, or
  the bare domain to `netjail/allowed-domains.txt` for an egress destination. Anything not
  listed is blocked. Don't widen the allowlist beyond what the feature needs.

## Conventions

- Keep `.ps1` and `.sh` script pairs in sync when editing one.
- Follow `rtk`-prefixed command usage per global CLAUDE.md.
