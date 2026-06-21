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

---

# Follow-up — auto-pull model lists into providers/ (2nd task)

## What was asked

"Have the system pull model info for all available models and put it in
`providers/`."

## What was done

- New `harness/sync_models.py`: queries each provider's live list-models
  endpoint and writes `providers/<provider>/models/<id>.toml`.
- Wired as a subcommand: `python3 -m harness sync-models [--only ...]
  [--dry-run] [--prune]` (dispatch added in `harness/__main__.py`; the default
  no-arg path is still the agent run loop).
- Wrapper scripts `scripts/sync-models.sh` + `.ps1` (run in the harness image
  with the host `providers/` dir bind-mounted so writes land in the repo).
- Docs: `providers/README.md` (refresh section) + `deepagent-image/CLAUDE.md`
  (Model routing + scripts list).

## Design

- **Generation-time, not runtime.** Needs API keys + network, which the sealed
  agent container deliberately lacks. So it's a dev command; output is committed.
- Per-provider fetch = pure `parse_*(json) -> [ModelInfo]` + thin `_get_json`
  (urllib, **no new dependency**). Endpoints table in `ENDPOINTS`:
  openai/deepseek/cursor/lmstudio (OpenAI-compat), anthropic (x-api-key +
  version header), google (`?key=`, filtered to `generateContent`), openrouter
  (rich: context_length + pricing), ollama (`/api/tags`, local).
- **Non-destructive**: never touches `provider.toml`; `default_model` stays a
  human choice. Idempotent (writes only changed files). `--prune` opt-in to
  delete models the API no longer returns. `--dry-run` writes nothing.
- Model id → filename sanitized (ids carry `/` and `:`); the real id is kept in
  the file's `name` field, which the loader already uses to build the spec.
- Unknown metadata written verbatim; the loader ignores unknown keys, so richer
  per-model fields are non-breaking.

## Testing (offline, WSL sandbox python3 3.12)

Could not hit live endpoints (no keys/network) or run the Docker wrappers here.
Tested the logic with mocked JSON / monkeypatched `fetch_models`:

- All five `parse_*` parsers → correct `ModelInfo` (id + extras), incl. google
  `generateContent` filtering and openrouter pricing/context_length.
- `model_filename` sanitizes `/` and `:`; `render_model_toml` output round-trips
  through `tomllib` (incl. escaped quotes).
- Endpoint resolution: base default vs `*_BASE_URL` override, query/bearer/
  x-api-key header assembly, providers lacking a base correctly return None.
- `sync_provider`: writes new/changed only, idempotent re-run (0 writes),
  `--prune` removes stale files, `--dry-run` writes nothing.
- `sync_models_main`: skips key-providers whose key is unset, returns 0.
- `py_compile` on all changed Python; `bash -n` on the wrapper.

**Not verified:** real API calls + the Docker wrappers (need keys, network, and
a built image — not available in the sandbox).

## Decisions & trade-offs

1. **urllib over the openai/langchain SDKs** for fetching — keeps it
   dependency-free and uniform across providers; downside is hand-rolled auth
   per provider (already captured in the `ENDPOINTS` table).
2. **Run inside the image, mount only `project/providers`** (not full project) —
   minimal surface; writes persist to the repo. PROVIDERS_DIR resolves to the
   mounted dir.
3. **`--prune` is opt-in.** Default keeps unknown/hand-added model files so a
   transient API hiccup or a manually-pinned model isn't silently deleted.
4. **"All available models" = what the account/key can list** (tier-gated), not
   every model in existence — provider APIs only return accessible models.
5. **Dir derived as `prefix` minus `:`** — matches the registry convention;
   breaks only if someone overrides `prefix` to differ from its dir name (noted
   in code).

## Missing info filled in

- Where to surface it (script vs subcommand) → both, as offered last turn.
- Which metadata to capture → id always + whatever each API gives (context
  window/pricing/family where available); sparse for id-only providers.
- Pull cadence → on-demand dev command (not automated), since it needs secrets.

## Suggested next steps

- Run `scripts/build.ps1` then `scripts/sync-models.ps1 --dry-run` with real
  keys to confirm live endpoints + container wiring.
- Optional: a scheduled/CI job (with keys) that runs `--dry-run` and opens a PR
  when the model list drifts.
- Optional: feed `context_window`/pricing into a cost module (the `cost.py` pyc
  artifacts suggest one exists/existed).
