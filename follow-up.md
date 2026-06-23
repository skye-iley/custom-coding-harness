# Milestone 1 — Follow-ups

Cost/token visibility + resource caps, plus the requested **energy tracker** and
**split / cached pricing** additions. Records decisions, trade-offs, and missing
info to revisit.

## Could not run here (environment limits)

- **Docker is unreachable from this WSL distro** (`docker: command not found` —
  Docker Desktop WSL integration is off for this distro), so the in-container
  steps were **not** executed by me: image build, `verify`, `smoke`, a real
  `run-docker` turn, and `docker inspect` of the applied caps. Please run
  `scripts/build.{ps1,sh}` then `scripts/smoke.{ps1,sh}` to exercise the tracker
  unit tests in-image, and a real `run-docker` turn with keys to see the live
  `[harness] usage:` line.
- **The harness can't be imported on the dev host** (no langchain/dotenv/
  deepagents installed locally). The cost math is therefore unit-tested via a
  path-based bootstrap (`tests/test_cost.py` / `tests/test_sync_models.py` load
  `harness.cost` / `harness.providers` / `harness.sync_models` by file path,
  bypassing `harness/__init__` which pulls those deps). All **36 tests pass
  locally** (`python3 tests/test_cost.py`, `python3 tests/test_sync_models.py`).

## Decisions & trade-offs

- **`Pricing` types live in `cost.py`; `providers.py` imports from it** (one
  direction) to avoid the providers↔cost cycle (design §2.4).
- **`AgentMiddleware` import in `cost.py` is guarded** (`try/except
  ModuleNotFoundError` → `object`). Lets the pure math import on a bare
  interpreter for tests; the real base class is used in-container. Trade-off: if
  langchain is genuinely broken in-image, the middleware silently subclasses
  `object` — caught by the `smoke` import check (`from harness.cost import
  CostTrackerMiddleware` + a real run).
- **Rates are USD per *million* tokens** in the TOML (human-friendly for
  hand-fill); `cost.py` divides by 1e6. `sync-models` converts OpenRouter's
  per-token prices to per-Mtok on write.
- **Missing pricing is loud-but-non-fatal** (per the addendum, overriding the
  design's "fail loudly/abort"): a `rate_table` model with no `[pricing]` warns
  **once** on stderr, then runs with session cost shown as a *floor* (unpriced
  calls excluded, surfaced as `(+N unpriced)`), never a silent `$0`.
- **"Prompt for an optional estimation" → an env knob, not a blocking prompt.**
  `DEEPAGENTS_PRICE_ESTIMATE` (USD/Mtok) prices otherwise-unpriced models. Chose
  this over an interactive `input()` inside `after_model` because a mid-turn
  blocking prompt would corrupt the REPL/stream and break non-TTY (smoke/CI)
  runs. The one-time warning tells the user the knob exists. Revisit if a true
  interactive prompt is wanted (would need to fire at the `you>` prompt, not
  mid-call).
- **Split / cached prices:** `[pricing]` carries `input`, `output`, `cache_read`,
  `cache_write` + `priced_as_of`. `cache_*` are recorded now even though caching
  isn't enabled (per request: "include cached as a data field even if not
  implemented"). Cached tokens are priced only if the provider reports them in
  `usage_metadata.input_token_details`; a bucket with no rate falls back to the
  `input` rate so tokens are never silently dropped.
- **Energy tracker:** optional per-model `[energy]` table (Wh/token), tracked for
  **any** provider including local `free` ones. `DEEPAGENTS_ELECTRICITY_RATE`
  (USD/kWh) converts energy → electricity cost on the usage line. Single blended
  `per_token` or a `per_input_token`/`per_output_token` split (split wins).
- **Local-device measured energy is specified, not built** (per request): see
  `deepagent-image/ENERGY_SPEC.md` and `cost.py:measure_local_energy_wh()` (raises
  `NotImplementedError`). Spec covers the measurement window (the existing
  `before_model`/`after_model` bracket), backends keyed by `[energy] source`
  (`nvidia_smi`/`rapl`/`powermetrics`/`ipmi`), integration to Wh, and open issues
  (attribution/baseline, sampling overhead, permissions, remote local servers).
- **Tracker gating / removability:** the middleware is appended only when the
  resolved model has non-`free` pricing, an energy estimate, or a budget is set;
  otherwise nothing is appended (null = MVP). The `except BudgetExceeded` clause
  in `run_repl` is inert when no tracker is present.
- **Cursor → `pricing = "free"`** (it bills by subscription, not per-token):
  reports tokens/energy but `$0`, avoiding a fake per-token rate. Switch to
  `rate_table` + a `[pricing]` table if a metered plan is used.
- **OpenRouter → `pricing = "reported"`** (cost in-band). **UNCONFIRMED** that
  LangChain's `ChatOpenAI` surfaces OpenRouter's cost field (design §5 risk). The
  code probes `usage_metadata` and `response_metadata` (top-level `cost` and
  nested `usage.cost`); if none is present, calls read as *unpriced* (loud, not
  $0). **TODO:** verify against a live OpenRouter turn; if absent, either enable
  the in-band cost opt-in or switch OpenRouter to `rate_table` (the synced
  `[pricing]` tables already provide per-Mtok rates as a fallback).
- **Resource caps** added to both run scripts as a Docker host-boundary control
  (defaults cpus=2, memory=4g, pids-limit=512), overridable via env (`.sh`) or
  params (`.ps1`). Explicitly **not** a sandbox. Disk quota + wall-clock timeout
  from design §2 remain out of scope (still planned).

## Missing / assumed info (verify before trusting)

- **Dollar rates are best-effort snapshots, NOT vendor-confirmed.** Hand-filled
  `[pricing]` for the default models of the `rate_table` providers
  (anthropic `claude-haiku-4-5`, openai `gpt-5.5`, google `gemini-3.5-flash`,
  deepseek `deepseek-v4-flash` + `deepseek-v4-pro`), stamped
  `priced_as_of = 2026-06-23`. **All other models stay unpriced** and will
  exercise the warn-once path until filled (or refreshed via `sync-models`).
  Verify the numbers against current price sheets.
- **Energy per-token figures are placeholders** (order-of-magnitude, `source =
  "estimate"`). No authoritative per-token Wh figure was available; replace with
  measured/vendor data when known.
- **Token-attribution caveat (design §2.1) not verified against a live turn.**
  Accumulation is per model call via `after_model` reading the last AIMessage's
  `usage_metadata`; confirm counts on a real multi-call (tool-using) turn.
- **`--stream` path:** usage is read from the final message; streamed runs may not
  carry `usage_metadata` per chunk. Per-turn line may be absent under `--stream`
  (acceptable for M1, design §5) — not verified live.

## Line-ending note (repo hygiene)

The working tree was entirely CRLF while the repo blobs are **mixed** (most
`.py`/`.toml` are LF; `cli.py`, `CLAUDE.md`, `run-docker.{sh,ps1}` were CRLF).
To keep diffs to real changes I normalized the files I edited to **LF**
(matching their sibling modules). A repo-wide fix (a `.gitattributes` with
`* text=auto eol=lf` + `git add --renormalize .`) is out of scope here but
recommended to stop the churn recurring.
