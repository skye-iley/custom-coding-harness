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
4. Cost comes from each provider's declared pricing (per-provider strategy in the `PROVIDERS`
   registry); a model with no pricing **fails loudly**, never silently costs `0`
   (`design_doc.md` §6 caveat).
5. `run-docker.{ps1,sh}` apply `--cpus`, `--memory`, `--pids-limit` with sane defaults, overridable.
6. Smoke test still passes (single non-interactive turn now also emits a usage line).

Explicit non-goals (stay deferred): prompt caching *strategy*, Headroom/Caveman compression,
telemetry-to-PR, account-level billing/usage APIs (OpenAI `/usage`, Anthropic admin cost API) for
the live tracker (wrong granularity/latency — see §2.1), rate-table auto-refresh from a remote,
multi-agent cost attribution. We only *account* for cached tokens if the provider reports them; we
do not *introduce* caching here.

---

## 2. Cost/Token Tracker

The tracker is a **fully optional module**: a gated middleware plus a null default. With it removed
(or no pricing declared), the harness behaves byte-for-byte like the MVP. See §2.5.

### 2.1 Tokens vs. cost — two different sources

Separate the two; they do not come from the same place.

**Tokens — in-band, free, always available.** LangChain attaches `usage_metadata` to each
`AIMessage` (`input_tokens`, `output_tokens`, `total_tokens`, and `input_token_details` with
`cache_read` / `cache_creation` when the provider reports them). This *is* the provider's usage API
for our purpose — no extra call, no extra credential.

**Cost — from the provider's declared pricing strategy (§2.2), not a global price file.** Three
kinds; each `Provider` in the registry declares which it uses:

| Strategy | Providers | How cost is derived |
|----------|-----------|---------------------|
| `ReportedCost` | OpenRouter (returns cost in-band) | read the cost the provider already put on the response; no local rate table to drift |
| `RateTable` | native: anthropic / openai / google / deepseek | per-model `input` / `output` / `cache_read` rates declared in the registry; **a dated snapshot — staleness caveat from §6 stays** |
| `Free` | local: ollama / lmstudio | cost = 0 |

**Account-level billing APIs are explicitly rejected for the live tracker** (OpenAI `/usage`,
Anthropic admin cost API): account-aggregate not per-session, delayed by hours, and need a separate
admin/org credential the harness does not hold. Wrong granularity, latency, and cred surface for a
per-turn display. (A later, separate reconciliation feature could use them — not M1.)

*Token-attribution caveat (verify during build):* `usage_metadata` is per-LLM-call; one turn with
tool use makes several model calls. Accumulating via the `after_model` middleware hook (§2.5) counts
each call once as it happens, sidestepping the "which messages are new this turn" problem that a
post-invoke `result["messages"]` walk would have. `--stream` path: confirm chunks still carry
`usage_metadata`; if not, document that streamed runs skip the per-turn line (acceptable for M1).

### 2.2 Pricing lives in the on-disk TOML registry (`project/providers/`)

> **Updated for the registry refactor.** `PROVIDERS` is no longer a hard-coded Python list — it is
> loaded at import from `project/providers/<provider>/provider.toml` + `models/<model>.toml`
> (`harness/providers.py:_load_providers`). So pricing data must live in those TOML files, **not** a
> Python rate map. The earlier draft of this section hard-coded a `{model: rates}` dict on the
> `Provider` dataclass; that is superseded by the layout below.

`PROVIDERS` is already the single source of truth for model routing; pricing joins it so a
custom/third-party provider is fully described by its registry files — no second file to edit.

- **Strategy on the provider** — `provider.toml` declares which pricing strategy the provider uses,
  e.g. `pricing = "rate_table"` (native anthropic/openai/google/deepseek), `pricing = "reported"`
  (openrouter, cost in-band), or `pricing = "free"` (ollama/lmstudio; the default when omitted).
- **Rates on the model** — for `rate_table` providers each `models/<model>.toml` carries a
  `[pricing]` table (`input`, `output`, `cache_read` per token/Mtok) plus a `priced_as_of` date.
  `sync-models` already pulls pricing for the providers that return it (google/openrouter/ollama),
  so these snapshots are partly auto-populated and refreshable; hand-fill the rest.

The loader (`_load_provider`) reads the strategy + per-model `[pricing]` tables into the in-memory
registry. `harness/providers.py` adds a `pricing: Pricing` field on `Provider` (and per-model rate
data on the model entries) built **from the TOML**, then `cost.py` does the math.

`Pricing` is a small tagged interface (`ReportedCost` | `RateTable` | `Free`) with one method:
`cost(usage_metadata, model_spec) -> float | None`. Price is **per-model**, keyed by the bare model
(the part after the prefix). `cost()` **raises loudly** when a `RateTable` provider is asked to
price a model whose TOML has no `[pricing]` table — never returns `0` silently (§6 caveat).
`ReportedCost` returns the in-band figure; `Free` returns `0`.

Rates are a dated snapshot: the per-model `priced_as_of` stamps the age and the harness warns when
stale. `prices.json` is dropped; the model TOMLs are the source of truth.

**Addendum — official vs. estimated rates (split provenance).** A rate snapshot carries its
*provenance* so a displayed dollar figure never silently passes a guess off as a confirmed price:

- **Top-level `[pricing]` = official** — vendor-published. Rates `sync-models` pulls from a provider
  API count as official (the provider/aggregator publishes them). Shown plain: `cost=$0.0450`.
- **Nested `[pricing.estimate]` = best-effort** — hand-filled, not vendor-confirmed; same fields plus
  an optional `source` note. Shown with a `~` prefix and `(est)` tag: `cost=~$0.0123 (est)`.

`rates_from_toml` resolves them: **official wins** when both are present; with no official rates it
falls back to the estimate sub-table and records `ModelRates.pricing_source` (`"official"` |
`"estimate"` | `None`). An unmarked table is read as an estimate — we never promote a guess to
official. The accumulator bumps `estimated_calls` (which already drives the `~`/`(est)` marking for
the runtime `DEEPAGENTS_PRICE_ESTIMATE` knob) when a `RateTable` price comes from an estimate table;
a `ReportedCost` in-band figure is the real bill and is never tagged. Migrating an estimate to
official is a one-line registry edit: move `[pricing.estimate]` → `[pricing]` once confirmed.

### 2.3 New module: `harness/cost.py`

Holds the *math and plumbing only* — the rate data lives in the registry (§2.2).

- the `Pricing` types (`ReportedCost`, `RateTable`, `Free`) and the `cost()` dispatch.
- `class UsageAccumulator` — running `input` / `output` / `cache_read` / `cache_creation` / `cost`;
  `add(usage_metadata, provider)` delegates pricing to `provider.pricing.cost(...)`.
- `format_line(turn_usage, totals) -> str` — the `[harness] usage:` string.
- `class BudgetExceeded(Exception)` — raised when a ceiling is crossed (caught in `run_repl`, §2.5).

### 2.4 `pricing` field, where it is read

`cost.py` imports the `Pricing` types; `providers.py` imports them to populate each `Provider`. To
avoid a circular import (providers ↔ cost), the `Pricing` types live in `cost.py` and `providers.py`
imports *from* `cost.py` (one direction). `cost.py` must not import `providers.py`.

### 2.5 Wiring — one gated middleware, null default (modularity seam)

The tracker plugs in exactly like `ShellHooksMiddleware` (`harness/hooks.py`): an `AgentMiddleware`
appended to the agent's middleware list. **`run_turn` is not touched.**

```
class CostTrackerMiddleware(AgentMiddleware):
    after_model  -> accumulator.add(usage_metadata, provider); if over budget: raise BudgetExceeded
    after_agent  -> print "[harness] usage: turn=… session=… cost=$…" to stderr (per turn)
```

`harness/cli.py` changes, total:
- `main()`: build the middleware **only if** the resolved provider has non-`Free` pricing (or a
  budget flag is set); append it to the `middleware` list passed to `build_agent`. Otherwise append
  nothing → null behavior.
- `run_repl()`: it already wraps each turn in `try/except KeyboardInterrupt`; add a sibling
  `except BudgetExceeded` → `_stage("budget exceeded")`, print session total, break (same
  deterministic exit as `/exit`). When the tracker is absent the exception is never raised, so the
  clause is inert.
- `parse_args`: add `--max-cost` (USD float) / `--max-tokens` (int), also from env
  (`DEEPAGENTS_MAX_COST` / `DEEPAGENTS_MAX_TOKENS`); default unset = no ceiling.

**Remove-without-functional-change check:** delete `cost.py`, drop the `pricing=` defaults
(they default to `Free`) and the middleware-append + budget args in `cli.py`. The one residue is the
`except BudgetExceeded` clause; it is inert when nothing raises it. Gate it behind the
middleware-enabled flag if you want literally zero residue — a 1-line conditional, the only price of
"fully modular."

### 2.6 Files touched

| File | Change |
|------|--------|
| `project/harness/cost.py` | **new** — `Pricing` types, `cost()` dispatch, `UsageAccumulator`, `CostTrackerMiddleware`, `BudgetExceeded`, `format_line` |
| `project/providers/<provider>/provider.toml` | add `pricing = "rate_table"|"reported"|"free"` (default `free`) |
| `project/providers/<provider>/models/<model>.toml` | add `[pricing]` table (`input`/`output`/`cache_read` + `priced_as_of`) for `rate_table` providers |
| `project/harness/providers.py` | read the `pricing` strategy + per-model `[pricing]` from TOML in `_load_provider`; add `pricing: Pricing` to `Provider`; build it from the loaded data (native → `RateTable`, openrouter → `ReportedCost`, ollama/lmstudio → `Free`) |
| `project/harness/sync_models.py` | (already pulls pricing where the API returns it) ensure it writes the `[pricing]` table + `priced_as_of` |
| `project/harness/cli.py` | conditional middleware append in `main`; `except BudgetExceeded` + session total in `run_repl`; budget args in `parse_args` |
| `project/.env.example` | document `DEEPAGENTS_MAX_COST` / `DEEPAGENTS_MAX_TOKENS` |
| `deepagent-image/CLAUDE.md` | document the pricing strategies in the registry, budget env vars, the usage line, and the "tracker is removable" contract |

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

1. `cost.py`: `Pricing` types (`ReportedCost` / `RateTable` / `Free`), `cost()` dispatch,
   `UsageAccumulator`, with unit coverage for per-model lookup and loud-fail-on-missing-rate.
2. Declare `pricing` strategy in each `provider.toml` and per-model `[pricing]` tables in
   `models/*.toml` (native `rate_table` with dated rates, openrouter `reported`, local `free`);
   read them in `_load_provider` and add the `pricing: Pricing` field to `Provider` built from that
   TOML data.
3. `CostTrackerMiddleware` + conditional append in `cli.py` (per-turn line + session total),
   no budget yet. Verify token counts against a real provider turn (resolve the per-call
   attribution caveat, §2.1). Confirm OpenRouter's in-band cost field and that LangChain surfaces
   it before locking `ReportedCost`.
4. `BudgetExceeded` + ceiling enforcement (`after_model` raise / `run_repl` catch); budget args.
5. Resource caps in both run scripts.
6. Update `smoke` expectation (single turn emits a usage line), run `verify` / `smoke`, update docs
   and the `design_doc.md` status matrix.

---

## 5. Risks / Open Questions

- **Per-call usage attribution.** A turn fans out into several model calls. The `after_model` hook
  counts each call once as it fires, avoiding the "which messages are new" problem of a post-invoke
  walk. Verify in step 3.
- **`ReportedCost` (OpenRouter) is unconfirmed.** Need to verify OpenRouter returns cost in-band
  *and* that LangChain's `ChatOpenAI` surfaces it (it may need `usage`/`extra_body` opt-in, and the
  field may land in `response_metadata` rather than `usage_metadata`). If it does not surface, fall
  back to a `RateTable` for OpenRouter. Gates step 3.
- **Rate staleness.** `RateTable` is a dated snapshot; mitigated by a `priced_as_of` stamp + age
  warning, not solved.
- **`--stream` path** has no final message to read usage from — confirm chunks carry usage or
  document the gap.
- **OpenAI-compatible providers** route a renamed model through `ChatOpenAI`; confirm
  `usage_metadata` survives and the per-model rate lookup keys off the right name (bare model after
  the prefix).
- **Cached tokens** only appear if the provider reports them; absent details, cache cost reads `0`
  (acceptable — we are accounting, not enabling caching).
- **Circular import** providers ↔ cost: `Pricing` types live in `cost.py`; `providers.py` imports
  from `cost.py` only (§2.4).

---

## 6. Acceptance

- `run-docker` turn prints a per-turn usage/cost line; session end prints a total.
- A `RateTable` model with no rate aborts with a clear "no pricing for <model>" error, not a `$0`
  total.
- `--max-cost` / `--max-tokens` end the session at the ceiling with a `[harness] budget exceeded`
  marker.
- **Removing the tracker** (delete `cost.py`, default `pricing` to `Free`, drop the cli wiring)
  leaves the harness behaving exactly as the MVP — verified by the smoke test passing unchanged.
- `docker inspect` on a running container shows the cpu / memory / pids limits applied.
- `smoke` and `verify` pass; `.ps1` and `.sh` scripts stay behavior-identical.
