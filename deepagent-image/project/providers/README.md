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

`default_model` is a model file stem (e.g. `gemini-3.5-flash`), not a full spec.
The full spec handed to langchain is `prefix + stem`.

## models/<model>.toml fields

| field  | type | required | meaning                                              |
|--------|------|----------|------------------------------------------------------|
| `name` | str  | no       | model id sent to the provider; defaults to file stem |

Add per-model metadata here as it becomes needed (context window, aliases,
pricing, etc.) — the loader ignores unknown keys, so new fields are non-breaking.

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
