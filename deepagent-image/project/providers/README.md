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
| `requires_key` | bool   | yes      | `validate_credentials` enforces `api_key_env` when true; also gates auto-selection (see below) |
| `priority`     | int    | yes      | auto-selection order; lowest wins among available providers    |
| `default_model`| str    | no       | model stem auto-selected for this provider; omit => never auto |
| `prefix`       | str    | no       | model spec prefix; defaults to `"<dirname>:"`                  |
| `base_url_env` | str    | no       | set => OpenAI-compatible, routed via `ChatOpenAI`             |
| `pricing`      | str    | no       | cost strategy: `rate_table` \| `reported` \| `free` (default) |
| `[limits]`     | table  | no       | plan rate limits → proactive request pacing (see below)      |

`default_model` is a model file stem (e.g. `gemini-3.5-flash`), not a full spec.
The full spec handed to langchain is `prefix + stem`.

### Auto-selection: two gates

When neither `--model` nor `DEEPAGENTS_MODEL` is set, `choose_model` walks the
registry by ascending `priority` and takes the first provider that passes **both**:

1. **`default_model` is set** — omit it and the provider is never auto-picked
   (lmstudio, openrouter).
2. **The provider is available** (`providers.provider_available`) — `requires_key
   = true` needs a non-empty `api_key_env`; `requires_key = false` is *always*
   available, since a local daemon has no credential to detect.

Gate 2 is why **ollama is the default** (`priority = 0`, `default_model =
"gemma4"`): a keyless provider used to be gated on `api_key_env` like a keyed one,
which made it permanently unselectable. An unconfigured run now picks a local
model rather than spending a cloud free-tier quota. Trade-off: auto-selection
effectively always succeeds, so a host with no daemon running fails at connect
time instead of with a clean "No model configured" error.

### `[limits]` — plan rate limits (request pacing)

Declares the provider's plan RPM/TPM so the harness paces every model call under
the ceiling (`harness/ratelimit.py`), instead of only reacting to 429s. **Inert
until a tier is selected** — ships as data, changes nothing by default.

```toml
[limits]
# tier = "free"               # activate here, or via DEEPAGENTS_PROVIDER_TIER
tokens_per_request = 12000    # estimate for TPM→rate (best-effort TPM pacing)
rpm = 30                      # optional top-level (used when no tier block matches)
tpm = 15000
[limits.free]                 # optional per-tier blocks
rpm = 30
tpm = 15000
[limits.tier1]
rpm = 1000
tpm = 1000000
```

- **RPM is exact** (a minimum interval between calls). **TPM is best-effort**: the
  `tokens_per_request` estimate turns tokens/min into a request rate; the stricter
  of RPM/TPM binds. A tight free tier can pace to ~1 call/minute — that's the real
  ceiling, surfaced rather than 429-thrashed.
- Active tier: `DEEPAGENTS_PROVIDER_TIER` env, else the `tier` key. A matching
  `[limits.<tier>]` block wins; otherwise top-level `rpm`/`tpm` apply.
- Env overrides (highest precedence, work even with no `[limits]` at all):
  `DEEPAGENTS_RPM`, `DEEPAGENTS_TPM`, `DEEPAGENTS_TOKENS_PER_REQUEST`.
- Numbers change and differ per model — **confirm against your provider console.**

### `pricing` strategy (Milestone 1)

How the cost tracker derives dollars for this provider's models:

- `rate_table` — native providers (anthropic/openai/google/deepseek). Cost comes
  from each model's `[pricing]` (official) or `[pricing.estimate]` (best-effort)
  table — see "Official vs. estimated prices" below. A priced model with **no**
  pricing table is not fatal: it warns once, then runs with cost shown as a
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

### Canonical layout (consistent across every model file)

Every `models/*.toml` follows the same shape, whether hand-written or emitted by
`sync-models`: `name`, a metadata block, an official `[pricing]` table, and an
`[energy]` table. A hand-filled `[pricing.estimate]` sub-table is added only when
present. **A field with no value is written as a commented placeholder
(`# field =`), not omitted** — so each file shows exactly what can be filled, and
all files read the same. An all-commented (empty) table means "no data recorded"
and is treated exactly like an absent table by the loader (no rates, no energy).
Files are written with **LF** line endings on every platform.

`sync-models` **merges, never clobbers**: a refresh overlays freshly-fetched
official rates and provider metadata onto the existing file but preserves the
hand-filled `[pricing.estimate]` and `[energy]` tables (no API returns those).

```toml
name = "example-model"

# Model metadata (commented = not recorded).
# display_name =
context_window = 200000

# USD per million tokens. Top-level [pricing] = official (vendor-published
# / sync-pulled). [pricing.estimate] = hand-filled, not vendor-confirmed
# (shown ~/(est)). See providers/README.md.
[pricing]
input = 0.8
output = 4.0
# cache_read =
# cache_write =
priced_as_of = "2026-06-24"

# Watt-hours per token (optional estimate; tracked even for free models).
[energy]
# per_input_token =
# per_output_token =
# source =
```

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

#### Official vs. estimated prices

A rate is one of two provenances, and the harness **marks which** so a shown
dollar figure never hides that it rests on a guess:

- **`[pricing]`** (top-level) = **official** — vendor-published. Rates
  `sync-models` pulls from a provider API count as official too. Shown plain:
  `cost=$0.0450`.
- **`[pricing.estimate]`** (nested sub-table) = **best-effort** — hand-filled,
  not vendor-confirmed. Same fields, plus an optional `source` note. Shown with a
  `~` prefix and an `(est)` tag: `cost=~$0.0123 (est)`.

```toml
[pricing.estimate]
input = 0.9
output = 4.5
priced_as_of = "2026-05-01"
source = "hand-filled estimate"   # optional: where the guess came from
```

**Official wins** when both tables are present. A table with no official
top-level rates is read as the estimate — we never present an unmarked guess as
confirmed. Promote a model from `[pricing.estimate]` to `[pricing]` once its
figures are vendor-confirmed. The runtime `DEEPAGENTS_PRICE_ESTIMATE` knob is a
third, transient kind of estimate (no registry edit) and is marked the same way.

### `[energy]` table (optional, any provider)

Watt-hours **per token** — an opt-in estimate, works even for `free` local
models:

```toml
[energy]
per_input_token = 0.0002
per_output_token = 0.0006
# or one blended figure (the split pair wins when both present):
# per_token = 0.0004
source = "estimate"   # or a local-device backend name; see docs/specs/energy.md
```

The committed estimates are placeholders. For locally-hosted models, `source`
names the (specified, not-yet-built) device-measurement backend — see
`docs/specs/energy.md`.

`ollama` carries one hand-written model file (`models/gemma4.toml`) because it is
the auto-selection default; the stem is an Ollama **tag**, so a locally-tagged
variant needs its own file to be a known spec. `lmstudio` and `openrouter` still
have no model files and no `default_model`; add a model file + `default_model`
when you pin one.

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
