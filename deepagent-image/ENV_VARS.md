# Environment Variables Reference

All container env vars for the harness. Set in `project/.env` (passed via `--env-file`) 
or at runtime (`docker run -e VAR=value`).

Launcher-side vars (host-side only, read by run-docker.sh/ps1 before `docker run`) 
are in [Launcher Environment](./CLAUDE.md#launcher-environment-host-side-not-env).

**Persisted alternative to hand-editing `.env` (Milestone 5):** every var below has a
matching field in `project/.harness-profile.yaml` (copy from `.harness-profile.yaml.example`),
resolved through `harness/config.py`'s `CLI flag > env var > profile file > default`
precedence. Write it with `harness config` / `harness config security` (host wizard) or the
in-session `/config set ... ` + `/config save`, instead of editing `.env` by hand for knobs
you want to persist across runs. See "Unified config" in `CLAUDE.md` for the full picture.

## Provider Authentication (Mutually Exclusive or Complementary)

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `OPENAI_API_KEY` | OpenAI API key (GPT-4, GPT-4o, etc.) | string | unset | `sk-proj-...` |
| `ANTHROPIC_API_KEY` | Anthropic Claude API key | string | unset | `sk-ant-...` |
| `GOOGLE_API_KEY` | Google Gemini API key (free tier available) | string | unset | `AIza...` |
| `CURSOR_API_KEY` | Cursor editor integration (requires CURSOR_BASE_URL) | string | unset | — |
| `OPENROUTER_API_KEY` | OpenRouter (requires OPENROUTER_BASE_URL) | string | unset | — |
| `LMSTUDIO_API_KEY` | LM Studio (keyless; requires LMSTUDIO_BASE_URL) | string | — | (not used) |
| `OLLAMA_API_KEY` | Ollama (keyless; local; requires OLLAMA_HOST) | string | — | (not used) |
| `DEEPSEEK_API_KEY` | DeepSeek API key | string | unset | — |

## Base URLs (Required for OpenAI-Compatible Providers)

| Var | Purpose | Example |
|-----|---------|---------|
| `CURSOR_BASE_URL` | Cursor API endpoint | `https://api.cursor.sh/v1` |
| `OPENROUTER_BASE_URL` | OpenRouter endpoint | `https://openrouter.ai/api/v1` |
| `LMSTUDIO_BASE_URL` | LM Studio local endpoint | `http://localhost:1234/v1` |
| `OLLAMA_HOST` | Ollama daemon (host-side); inside container use `host.docker.internal` | `http://host.docker.internal:11434` |

## Model Selection

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_MODEL` | Explicit model spec (overrides auto-selection) | string | unset | `openai:gpt-4o`, `google_genai:gemini-2-flash` |
| `DEEPAGENTS_PROVIDER_TIER` | Rate-limit tier (e.g., "free", "tier1") | string | unset | See `providers/<provider>/provider.toml` |

## Conversation & Memory (Milestone 2)

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_THREAD_ID` | Present thread id; unset = fresh per run (`session-<ts>`) | string | unset | `my-refactor` |
| `DEEPAGENTS_TOPIC` | Continual-topic label for recall scoping | string | unset | `feature-x` |
| `DEEPAGENTS_ARCHIVE` | Enable/disable past archive (M2) | 0/1 | 1 | — |

## Cost & Token Tracking (Milestone 1)

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_MAX_COST` | Session cost ceiling (USD) | float | unset | `5.00` |
| `DEEPAGENTS_MAX_TOKENS` | Session token ceiling | int | unset | `100000` |
| `DEEPAGENTS_PRICE_ESTIMATE` | Fallback price for unpriced models (USD/Mtok) | float | unset | `0.01` |
| `DEEPAGENTS_ELECTRICITY_RATE` | Electricity cost (USD/kWh); converts energy to cost | float | unset | `0.15` |

## Workspace & Shell (Sandbox Boundary)

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `AGENT_WORKSPACE` | Workspace root inside container | path | `/project/workspace` | — |
| `DEEPAGENTS_SHELL_ENV_ALLOW` | Extra env vars to expose to agent shell (comma/space list, trailing `*` = prefix) | string | empty | `MYAPP_URL,MYAPP_*` |
| `DEEPAGENTS_STATE_DIR` | Harness state root (checkpoints.sqlite, past.sqlite); outside workspace by default | path | `$WORKSPACE/.deepagents` | `/project/state` (set by run-docker) |

## Workspace Visibility & Secret Masking (Milestone 4)

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_MASK` | Enable workspace masking scan + empty-overlay mounts | 0/1 | 1 | — |
| `DEEPAGENTS_MASK_MODE` | Visibility mode: "deny" (default) or "allow" | string | deny | — |
| `DEEPAGENTS_AGENTIGNORE` | Override in-workspace config filename | string | `.agentignore` | `.maskignore` |
| `DEEPAGENTS_JAIL` | Route all fs tools + the shell through the bubblewrap jail (slice H). Requires the narrow seccomp profile; `run-docker` passes it and fails closed if absent. On an AppArmor host it also needs the narrowed LSM profile — `run-docker` selects it automatically once loaded (`scripts/install-apparmor-profile.sh`), see `DEEPAGENTS_JAIL_APPARMOR` | 0/1 | 0 (off) | `1` |
| `DEEPAGENTS_NS_GUARD` | Shell-tool denylist for the namespace syscalls the jail's seccomp profile re-permits container-wide. A tripwire, not containment | `0`/`1`/`warn` | tracks `DEEPAGENTS_JAIL` | `warn` |

## Human-in-the-Loop (Milestone 3)

HITL is **only active if `.harness-config.yaml` exists** in the project root and is mounted 
by run-docker. Env vars control M3 runtime behavior; see `.harness-config.yaml.example` 
for the config file schema.

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_HEADLESS` | Batch mode: run to completion, emit JSON, no interactive prompt | 0/1 | 0 | — |
| `DEEPAGENTS_MAX_RETRIES` | Resilience: exponential backoff retries on 429/5xx | int | 3 | — |
| `DEEPAGENTS_RETRY_BASE` | Resilience: backoff base (seconds) | float | 1.0 | — |

## Rate Limiting & Request Pacing

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_RPM` | Requests per minute (overrides registry) | int | unset | — |
| `DEEPAGENTS_TPM` | Tokens per minute (overrides registry) | int | unset | — |
| `DEEPAGENTS_TOKENS_PER_REQUEST` | Estimated tokens per request (TPM → RPM conversion) | int | unset | `2000` |

## Debug & Tracing

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_DEBUG` | Dump partial checkpointer state on turn failure | 0/1 | 0 | — |
| `LANGSMITH_TRACING` | Enable LangSmith tracing (requires LANGSMITH_API_KEY) | true/false | false | — |
| `LANGSMITH_API_KEY` | LangSmith API key | string | unset | — |
| `LANGSMITH_PROJECT` | LangSmith project name | string | unset | — |

## Backend Configuration (Advanced)

| Var | Purpose | Type | Default | Example |
|-----|---------|------|---------|---------|
| `DEEPAGENTS_PROVIDERS_DIR` | Override registry path (tests only) | path | `<project>/providers` | — |
| `DEEPAGENTS_WORKFLOWS_DIR` | Override workflows path | path | `<project>/workflows` | — |
| `DEEPAGENTS_HOOK_TIMEOUT` | Workflow gate/step timeout (seconds, <=0 disables) | float | 30.0 | — |

---

## Not in `.env` — Launcher Environment (Host-Side)

These are read by `run-docker.sh` / `run-docker.ps1` **before** `docker run` and affect 
only container startup. They are **never** passed into the container via `--env-file`.

See [Launcher Environment](./CLAUDE.md#launcher-environment-host-side-not-env) in CLAUDE.md.

| Sh Var | Ps1 Param | Purpose | Default |
|--------|-----------|---------|---------|
| `MAP_HOST_USER` | — | Auto-map host uid:gid on native Linux (fixes bind-mount permissions) | auto-detect |
| `HOST_UID` / `HOST_GID` | — | Override detected uid:gid for mapping | `id -u` / `id -g` |
| `CPUS` | `-Cpus` | Docker CPU limit | `2` |
| `MEMORY` | `-Memory` | Docker memory limit | `4g` |
| `PIDS_LIMIT` | `-PidsLimit` | Docker PID limit (fork-bomb guard) | `512` |
| `EPHEMERAL` | `-Ephemeral` | Mount throwaway workspace copy; revert on close | off |
| `SAVE_WORKSPACE` | `-SaveWorkspace` | Ephemeral + snapshot to workspace-logs/<ts>/ | off |
| `DEEPAGENTS_MODEL` | `-Model` | Model spec, forwarded into the container as `-e` even when absent from `.env` | from profile/`.env` |
| `DEEPAGENTS_MASK_MODE` | `-MaskMode` | Mask visibility mode (`deny`\|`allow`) | from profile/`.env` |
| `DEEPAGENTS_JAIL` | `-Jail` | bwrap fs jail on/off | from profile/`.env` |
| `DEEPAGENTS_JAIL_APPARMOR` | `-JailApparmor` | AppArmor stance for the jail | from profile/`.env` |
| `AUTONOMY` | `-Autonomy` | Write/update `autonomy_level` in `.harness-config.yaml` before launch (`strict`\|`guided`\|`autonomous`); creates the file if absent | unset (no-op) |

(Milestone 5, C3: `-Model`/`-MaskMode`/`-Jail`/`-JailApparmor` resolve `-Flag > host env >
project/.env > .harness-profile.yaml > default` via `scripts/lib/config.{ps1,sh}`, the same
precedence `harness/config.py` uses container-side. `-Autonomy` is different — it's an imperative
write to `.harness-config.yaml`, not a resolved value, and setting it turns HITL on for the run
if it wasn't already. See "Unified config" in `CLAUDE.md`.)
| `NET_JAIL` | `-NetJail` | Deny-all-egress network jail (see netjail/README.md) | off |
| `DEEPAGENTS_JAIL_APPARMOR` | — | AppArmor stance for the bwrap jail. **Unset = auto (slice J):** `run-docker` asks the daemon what confines a container; no LSM → passes nothing; LSM in force + `deepagent-userns` loaded → selects it; LSM in force + not loaded → **aborts pre-flight** with the install command, never falling back to `unconfined`. `unconfined` = works everywhere at the cost of dropping the **whole** `docker-default` profile, not just its `deny mount,`. Any other value = a host-loaded profile name. Load the profile on the **daemon's** host: `sudo scripts/install-apparmor-profile.sh` | unset (auto) |
