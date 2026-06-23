# providers/

Filesystem registry of model providers. Replaces the hard-coded `PROVIDERS`
list that used to live in `harness/providers.py`. Loaded at import time by
`harness/providers.py:_load_providers()`.

## Layout

```
providers/
  <provider>/            # dir name = provider id (== model spec prefix sans ":")
    provider.toml        # config shared across ALL models for this provider
    models/
      <model>.toml       # config for ONE specific model
```

## provider.toml fields

| field          | type   | required | meaning                                                       |
|----------------|--------|----------|---------------------------------------------------------------|
| `api_key_env`  | str    | yes      | env var holding the key / opting the provider in              |
| `requires_key` | bool   | yes      | `validate_credentials` enforces `api_key_env` when true       |
| `priority`     | int    | yes      | auto-selection order; lowest wins when several keys are set    |
| `default_model`| str    | no       | model stem auto-selected for this provider; omit => never auto |
| `prefix`       | str    | no       | model spec prefix; defaults to `"<dirname>:"`                  |
| `base_url_env` | str    | no       | set => OpenAI-compatible, routed via `ChatOpenAI`             |
| `pricing`      | str    | no       | cost strategy: `rate_table` \| `reported` \| `free` (default) |

`default_model` is a model file stem (e.g. `gemini-3.5-flash`), not a full spec.
The full spec handed to langchain is `prefix + stem`.

### `pricing` strategy (Milestone 1)

How the cost tracker derives dollars for this provider's models:

- `rate_table` — native providers (anthropic/openai/google/deepseek). Cost comes
  from each model's `[pricing]` table (below). A priced model with **no**
  `[pricing]` table is not fatal: it warns once, then runs with cost shown as a
  floor (set `DEEPAGENTS_PRICE_ESTIMATE` to estimate it) — never a silent `$0`.
- `reported` — the provider returns dollar cost in-band (openrouter). The tracker
  reads it off the response; the model `[pricing]` table (if any) is reference.
- `free` (default, omit) — local/self-hosted (ollama/lmstudio) or
  subscription-billed (cursor): API cost is `0`. Energy is still tracked.

## models/<model>.toml fields

| field  | type | required | meaning                                              |
|--------|------|----------|------------------------------------------------------|
| `name` | str  | no       | model id sent to the provider; defaults to file stem |

Add per-model metadata here as it becomes needed (context window, aliases,
pricing, etc.) — the loader ignores unknown keys, so new fields are non-breaking.

### `[pricing]` table (for `rate_table` providers)

USD **per million tokens**, a dated snapshot:

```toml
[pricing]
input = 1.0          # fresh (non-cached) input
output = 5.0
cache_read = 0.1     # cached-input read   } recorded now even though caching
cache_write = 1.25   # cache-creation write } isn't enabled yet (split prices)
priced_as_of = "2026-06-23"   # staleness stamp
```

Only `input`/`output` are needed to price a model; the `cache_*` fields are the
split (cached-vs-fresh) prices, priced only when the provider reports cached
tokens (caching is accounted-for, not enabled, in M1). A bucket with no rate
falls back to the `input` rate, so cached tokens are never silently dropped.

### `[energy]` table (optional, any provider)

Watt-hours **per token** — an opt-in estimate, works even for `free` local
models:

```toml
[energy]
per_input_token = 0.0002
per_output_token = 0.0006
# or one blended figure (the split pair wins when both present):
# per_token = 0.0004
source = "estimate"   # or a local-device backend name; see ENERGY_SPEC.md
```

The committed estimates are placeholders. For locally-hosted models, `source`
names the (specified, not-yet-built) device-measurement backend — see
`../../ENERGY_SPEC.md`.

Local/keyless providers (ollama, lmstudio) and ones with no chosen default
(openrouter) have no model files and no `default_model`; add a model file +
`default_model` when you pin one.

## Refreshing model files from provider APIs

`models/*.toml` can be generated from each provider's live list-models endpoint
instead of hand-written:

```bash
deepagent-image/scripts/sync-models.sh                 # all providers whose key is set
deepagent-image/scripts/sync-models.sh --dry-run       # show changes, write nothing
deepagent-image/scripts/sync-models.sh --only openai anthropic
deepagent-image/scripts/sync-models.sh --prune         # delete models the API no longer lists
```

(`.ps1` equivalent for Windows; or `python3 -m harness sync-models ...` directly.)

This is a **dev-time** step — it needs provider API keys (`project/.env`) and
network, which the sealed agent runtime does not have. It writes the model files;
commit the result. It never edits `provider.toml`, so `default_model` stays a
human choice. Metadata richness varies by provider (most return only the id;
google/openrouter/ollama add context window / pricing / family). See
`harness/sync_models.py`.
