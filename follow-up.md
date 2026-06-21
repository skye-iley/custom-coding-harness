# Follow-up — provider/model registry restructure

Branch: `restructure-provider/model-info`

## What was asked

Move providers + models out of the hard-coded `PROVIDERS` list in
`harness/providers.py` into a folder structure:

```
providers/
  <providername>/        # one provider
    provider.toml        # config across all models for the provider
    models/<model>.toml  # config for a specific model
```

## What was done

- New on-disk registry at `deepagent-image/project/providers/` (one dir per
  provider; `provider.toml` + `models/*.toml`). README in that dir documents
  the schema.
- `harness/providers.py` no longer hard-codes the list. It loads `PROVIDERS`
  from the registry at import time (`_load_providers` / `_load_provider`),
  sorted by a new `priority` field. Public API unchanged: `Provider`,
  `PROVIDERS`, `choose_model`, `validate_credentials`, `resolve_chat_model`.
- Deleted the `DEFAULT_*_MODEL` module constants — defaults now live in each
  `provider.toml` as `default_model` (a model stem, expanded to `prefix+stem`).
- Docs updated: `deepagent-image/CLAUDE.md` (Layout + Model routing).

## "True cause of the issue" (step 3)

This task was a **restructure request, not a bug**. There was no defect to
reproduce; the "issue" is that provider/model config was a single Python literal
(`PROVIDERS: list[Provider]`), which mixes data with code and is awkward to
extend per-model. Verified the *current* behavior first (order, defaults,
auto-selection, validation) and then confirmed the file-backed loader
reproduces it exactly (see Testing).

## Testing

Ran in the WSL sandbox with `python3` (3.12). Could not build/run the Docker
image or the full harness here (`dotenv`/`deepagents` not installed in the
sandbox venv, and Docker not exercised), so tested `providers.py` in isolation
by importing the file directly:

- Registry load: order + every `(prefix, api_key_env, default_model,
  requires_key, base_url_env)` tuple matches the old hard-coded list **exactly**.
- `choose_model`: explicit arg, `DEEPAGENTS_MODEL` env, and priority-based
  auto-selection (google > anthropic > openai) all correct.
- `validate_credentials`: raises on missing required key, passes for keyless
  ollama, emits the "no known provider prefix" note for unknown prefixes.
- Model discovery: `models/*.toml` parsed into `Provider.models`.
- Failure modes: missing required field, `default_model` with no matching model
  file, and an empty registry each raise a clear `SystemExit`.

All assertions passed. **Not yet verified:** `docker build` + a live run inside
the container (the `COPY project/ .` step should include `providers/`, but this
was not executed here).

## Decisions & trade-offs

1. **Config format = TOML (stdlib `tomllib`).** No new dependency (Python 3.11+,
   image runs 3.12), unlike YAML which would need `pyyaml`. Trade-off: `tomllib`
   is read-only — fine, the harness only reads. The repo's *other* config files
   are YAML; chose consistency-with-the-runtime (no deps) over
   consistency-with-repo. Switchable later if YAML is preferred.

2. **Registry lives at `project/providers/`, not `harness/providers/`.** A
   `providers/` package dir would collide with the `providers.py` module name.
   `project/` is the container WORKDIR/`/project` and already holds run-time
   config (AGENTS.md, .mcp.json), and `COPY project/ .` ships it into the image.

3. **`prefix` derived from dir name** (`<dir>:`) but overridable via
   `prefix` in `provider.toml`. All current prefixes equal `dirname + ":"`.

4. **`priority` is now explicit** (int) instead of implicit list order, because
   directory iteration is unordered. Lowest wins. Kept the same effective order.

5. **`default_model` is a stem in `provider.toml`** (single source: the provider
   picks its default by name; model files describe models). Considered marking
   the default inside the model file (`default = true`); rejected to avoid two
   ways to express the same thing.

6. **Per-model files are intentionally minimal** (just `name`). The loader
   ignores unknown keys, so metadata (context window, pricing, aliases) can be
   added later without code changes. The structure is in place per the request
   even though no per-model config is consumed yet.

## Missing info I had to fill in

- The request didn't specify a config file format → chose TOML (decision 1).
- Didn't specify where the folder lives → `project/providers/` (decision 2).
- Didn't specify what goes in per-model files → minimal `name` for now
  (decision 6).
- No "issue" existed to reproduce → treated step 3 as verifying behavior parity.

## Suggested next steps

- `docker build` + smoke run to confirm `providers/` is shipped and loads in the
  container.
- Add real per-model metadata once a consumer needs it (e.g. cost/token tracking
  — there are `cost.py` pyc artifacts suggesting a cost module exists/existed).
- Consider a tiny unit test committed under the repo (none exist yet) so the
  parity check above runs in CI rather than ad hoc.
