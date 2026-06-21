# Milestone 1 Plan: Cost/Token Visibility + Resource Caps

**Status:** Planned. Successor to the MVP (interactive multi-turn REPL, validated core loop).
**Maps to:** `design_doc.md` §6 (Token Usage & Cost Tracker) and §2 (Resource Limits).
**Closes Known Limitations** (`design_doc_mvp.md` §9): "pays provider rates blind", "no per-session
turn or token ceiling", "runaway agent can consume host CPU/memory".

When this doc and `design_doc.md` disagree on *what we build next*, this doc wins (same rule the MVP
doc has over the full design).

---

## 1. Goal & Definition of Done

A session reports what it spent and refuses to spend without bound, and a container cannot exhaust
the host.

Done when:
1. Each turn prints a per-turn and cumulative token + cost line (stderr, `[harness]`-prefixed, like
   the existing stage markers — out of the agent's reply stream).
2. Session end prints a total: tokens (input / output / cached) and dollar cost.
3. An optional per-session budget (tokens or dollars) ends the REPL loudly when crossed.
4. `prices.json` drives cost; an unpriced model **fails loudly**, never silently costs `0`
   (`design_doc.md` §6 caveat).
5. `run-docker.{ps1,sh}` apply `--cpus`, `--memory`, `--pids-limit` with sane defaults, overridable.
6. Smoke test still passes (single non-interactive turn now also emits a usage line).

Explicit non-goals (stay deferred): prompt caching *strategy*, Headroom/Caveman compression,
telemetry-to-PR, `prices.json` auto-refresh from a remote, multi-agent cost attribution. We only
*account* for cached tokens if the provider reports them; we do not *introduce* caching here.

---

## 2. Cost/Token Tracker

### 2.1 Where the numbers come from

LangChain attaches `usage_metadata` to each `AIMessage` (`input_tokens`, `output_tokens`,
`total_tokens`, and `input_token_details` with `cache_read` / `cache_creation` when the provider
reports them). `run_turn` (`harness/cli.py`) already holds the invoke `result`, whose `messages`
list contains those AI messages.

**Chosen approach: post-invoke aggregation** — after `agent.invoke`, walk `result["messages"]`, sum
`usage_metadata` across any AI messages new to this turn. No LangChain callback or middleware
wiring, so it does not depend on `deepagents` exposing `on_llm_end` (the §6 stub assumed a callback;
this is lower-risk and uses data we already have in hand).

*Caveat to verify during build:* `usage_metadata` is per-LLM-call; a single turn with tool use
makes several model calls, so several AI messages accumulate in `result["messages"]`. To avoid
double-counting across turns on the same thread, track a per-thread "messages already counted"
high-water mark (count only messages appended since the previous turn), or read usage from the
turn's stream deltas. Decide empirically in step 6 below; the high-water mark is the default.

If `--stream` is set, `run_turn` returns `None` and prints raw events; pull usage from the streamed
chunks in that path, or document that streamed runs skip the per-turn line (acceptable for M1).

### 2.2 New module: `harness/cost.py`

- `load_prices(path) -> dict` — read `project/prices.json` (optional file, like `.mcp.json` /
  `hooks.json` via `loaders.py`). Record the snapshot date; warn if older than N days.
- `price_key(model_spec) -> str` — map the harness spec to a `prices.json` key. Spec is
  `provider:model` (e.g. `anthropic:claude-haiku-4-5`); §6 keys use `provider/model`. Replace the
  first `:` with `/`. For OpenAI-compatible providers (cursor/openrouter/lmstudio) the spec prefix
  and the real model differ — define the key off the spec prefix consistently and document it.
- `class UsageAccumulator` — holds `input`, `output`, `cache_read`, `cache_creation`, `cost`. Method
  `add(usage_metadata, model_spec)`; raises (loud fail) when the model key is absent from prices.
- `format_line(turn_usage, totals) -> str` — the `[harness] usage:` string.

`prices.json` schema is `design_doc.md` §6 verbatim (`input_cost_per_token`,
`output_cost_per_token`), plus an optional `cache_read_cost_per_token` so Anthropic prompt-cache
reads bill at the reduced rate instead of full input rate (§6 caveat). Missing cache rate → fall
back to input rate and note it.

### 2.3 Wiring in `harness/cli.py`

- `main()`: `prices = load_prices(Path.cwd() / "prices.json")`; build one `UsageAccumulator` for the
  session; thread it (or the prices + a session-scoped accumulator) into `run_repl`.
- `run_turn`: after `final_message_text`, extract this turn's usage, `accumulator.add(...)`, print
  `[harness] usage: turn=<…> session=<…> cost=$<…>` to stderr.
- `run_repl`: after each turn, check the optional budget; if crossed, `_stage("budget exceeded")`
  and break (same deterministic exit path as `/exit`). At `session closed`, print the session total.
- `parse_args`: add `--max-cost` (USD float) and `--max-tokens` (int), each also from env
  (`DEEPAGENTS_MAX_COST` / `DEEPAGENTS_MAX_TOKENS`), default unset = no ceiling.

### 2.4 Files touched

| File | Change |
|------|--------|
| `project/harness/cost.py` | **new** — prices load, key map, accumulator, formatting |
| `project/harness/cli.py` | wire accumulator into `main` / `run_turn` / `run_repl`; budget args + checks |
| `project/harness/loaders.py` | optional: `load_prices` here for symmetry with `load_mcp_tools` |
| `project/prices.json` | **new** — versioned snapshot, dated; seed with the models in `providers.py` defaults |
| `project/.env.example` | document `DEEPAGENTS_MAX_COST` / `DEEPAGENTS_MAX_TOKENS` |
| `deepagent-image/CLAUDE.md` | document `prices.json`, the budget env vars, the usage line |

---

## 3. Resource Caps

Pure run-script change; no image rebuild. Add to the `docker run` arg list in **both** scripts
(keep the `.ps1` / `.sh` pair in sync):

- `--cpus` (default e.g. `2`)
- `--memory` (default e.g. `4g`)
- `--pids-limit` (default e.g. `512`, blunts fork-bomb)

Each overridable: `run-docker.ps1` gains `-Cpus` / `-Memory` / `-PidsLimit` params; `run-docker.sh`
gains matching flags or env vars. Defaults live in one place near the top of each script.

| File | Change |
|------|--------|
| `deepagent-image/scripts/run-docker.ps1` | add cap params + append to `$dockerArgs` |
| `deepagent-image/scripts/run-docker.sh` | mirror exactly |
| `deepagent-image/CLAUDE.md` | document the caps + override flags |
| `design_doc.md` §2 / status matrix | flip Resource Limits ⬜ → ✅ when shipped |

These are a Docker boundary control, not a sandbox — do not describe them as sandboxing
(`design_doc_mvp.md` §5 / repo CLAUDE.md hard rule).

---

## 4. Build Order

1. `prices.json` + `cost.py` (`load_prices`, `price_key`, `UsageAccumulator`) with unit coverage for
   the key map and loud-fail-on-missing-model.
2. Wire `cost.py` into `cli.py` (per-turn line + session total), no budget yet.
3. Verify token counts against a real provider turn (resolve the double-count caveat, §2.1).
4. Budget args + ceiling enforcement in `run_repl`.
5. Resource caps in both run scripts.
6. Update `smoke` expectation (single turn emits a usage line), run `verify` / `smoke`, update docs
   and the `design_doc.md` status matrix.

---

## 5. Risks / Open Questions

- **Per-turn usage attribution.** Multi-call turns and thread-resumed history make naive summation
  double-count. Resolved in step 3; default is the per-thread counted-messages high-water mark.
- **Price staleness.** Snapshot drifts; mitigated by a dated file + an age warning, not solved.
- **`--stream` path** has no final message to read usage from — pull from chunks or document the gap.
- **OpenAI-compatible providers** route a renamed model through `ChatOpenAI`; confirm
  `usage_metadata` survives and the `price_key` mapping is unambiguous.
- **Cached tokens** only appear if the provider reports them; absent details, cache cost reads `0`
  (acceptable — we are accounting, not enabling caching).

---

## 6. Acceptance

- `run-docker` turn prints a per-turn usage/cost line; session end prints a total.
- An unpriced `--model` aborts with a clear "model not in prices.json" error, not a `$0` total.
- `--max-cost` / `--max-tokens` end the session at the ceiling with a `[harness] budget exceeded`
  marker.
- `docker inspect` on a running container shows the cpu / memory / pids limits applied.
- `smoke` and `verify` pass; `.ps1` and `.sh` scripts stay behavior-identical.
