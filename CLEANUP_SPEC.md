# Cleanup & Simplification Spec

Status: WIP — branched `fix/docs-redundancy-simplification`. Prioritized by impact + effort.

---

## PRIORITY 1: DOCUMENTATION CONSOLIDATION (High Impact, Low Effort)

### 1.1 Root CLAUDE.md — Redirect Launcher Vars

**File:** `CLAUDE.md`  
**Lines:** Search for "Launcher environment" section (currently duplicates `deepagent-image/CLAUDE.md` §Commands)  
**Change:** Replace the full table with a forward reference.

**Old (current, ~30 lines):**
```markdown
### Launcher environment (host-side, **not** `.env`)
| Env (`.sh`) | ... [full table] ...
```

**New:**
```markdown
### Launcher environment (host-side, **not** `.env`)

Host-side launcher variables (MAP_HOST_USER, CPUS, MEMORY, NET_JAIL, etc.) are 
documented in detail in **[deepagent-image/CLAUDE.md — Launcher environment](./deepagent-image/CLAUDE.md#launcher-environment-host-side-not-env)**.

The key distinction: `.env` is container-bound (via `--env-file`); launcher vars 
are read by the host shell *before* `docker run` and affect container startup 
parameters only.
```

**Impact:** ~20 line reduction, single source of truth.

---

### 1.2 .env.example — Provider Explanations

**File:** `deepagent-image/project/.env.example`  
**Lines:** 2–9 (provider keys section)  
**Change:** Expand each provider's comment with use-case guidance.

**Old:**
```
OPENAI_API_KEY=
CURSOR_API_KEY=
# Free lower-power model and web API
GOOGLE_API_KEY=
```

**New:**
```
# OpenAI (GPT-4, best general capability, paid per token)
OPENAI_API_KEY=

# Cursor (editor-integrated, proprietary, paid; requires CURSOR_BASE_URL)
CURSOR_API_KEY=

# Google Gemini (free tier available, good for testing; set GOOGLE_API_KEY)
GOOGLE_API_KEY=

# Anthropic Claude (paid, strong reasoning; requires ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=

# Ollama (local-only, keyless, requires host daemon; set OLLAMA_HOST=http://host.docker.internal:11434)
OLLAMA_API_KEY=
```

**Impact:** Reduces confusion for first-time setup. Guides users to best provider for their use case.

---

### 1.3 "Removable Contract" Glossary

**File:** `docs/README.md`  
**Location:** Add new "Glossary" section at the end (before Archive).

**Add:**
```markdown
## Glossary

**Removable contract:** A feature whose code can be deleted and the harness 
reverts to prior-milestone behavior *byte-for-byte*, with no residual coupling. 
Examples:
  - `DEEPAGENTS_MASK=0` disables M4 masking, leaving M3 unchanged.
  - `DEEPAGENTS_ARCHIVE=0` disables M2 past archive, falling back to M1.
  - Deleting `archvie.py` + `memadmin.py` and rewiring defaults reverts to M1.

Removable contracts ensure features can be disabled or removed without leaving 
dead code or partial state behind. Each milestone doc lists its removable contract 
as part of "Def-of-Done."
```

**Update all milestone docs:** Replace full explanations of the contract with a one-liner 
"See glossary" forward reference.

**Impact:** ~8 explanations replaced with 1 definition = ~40 line reduction.

---

### 1.4 IMMEDIATE_TODO.md — Mark Complete

**File:** `IMMEDIATE_TODO.md`  
**Change:** This doc is a completed work record (hostmap fix is built & shipped). Mark as archive.

**New front-matter:**
```markdown
# ✅ COMPLETED — Native-Linux Bind-Mount Permission Fix

**Completed:** 2026-07-23  
**Shipped in:** feat/milestone_4 slices A–G (not yet merged to main)  
**Test coverage:** `project/tests/test_hostmap.py` (decision matrix + unit tests)  
**Implementation:** `scripts/lib/hostmap.sh`, `run-docker.sh` lines 63–76, `run-docker.ps1` (Windows no-op)

This document records the problem, design, and solution for archival. The fix is production-ready.
```

**Impact:** Removes stale work-in-progress from active docs.

---

### 1.5 M4 Config Knobs — New Section in deepagent-image/CLAUDE.md

**File:** `deepagent-image/CLAUDE.md`  
**Location:** After "Human-in-the-loop (Milestone 3)" section, add new subsection.

**Add:**
```markdown
## Workspace visibility / secret masking (Milestone 4)

> **Status: in-progress** — code on `feat/milestone_4`, slices A–G landed.
> Full spec in `docs/milestones/in-progress/milestone4.md`.

The harness can enforce a trust boundary on the workspace filesystem:

**Config knobs** (set in `project/.env` or at container runtime):
- `DEEPAGENTS_MASK` (default 1): Enable/disable the masking scan and empty-overlay mounts.
  Set to 0 for Milestone 3 parity (byte-for-byte unchanged).
- `DEEPAGENTS_MASK_MODE` (default "deny"): Visibility mode.
  - "deny": Agent sees everything except masked paths (present-but-empty).
  - "allow": Agent sees only allow-listed paths (requires `.agentignore` to opt paths in).
- `DEEPAGENTS_AGENTIGNORE` (default ".agentignore"): Override the in-workspace config filename.

**Quick start: In-workspace `.agentignore` (gitignore syntax):**
```
# Comments start with #
# Default globs (*.pem, *.key, .env, .aws/credentials, etc.) are always masked.

# Unmask a specific file (deny mode only):
!important-config.yaml

# Mask an additional path:
**/*.backup

# Designated-secret floor — can never be negated:
#!floor:
secrets/prod.key
.ssh/
#!floor-end
```

See `docs/features/workspace_visibility.md` (§3) for full `.agentignore` syntax and examples.

**Removable contract:** Set `DEEPAGENTS_MASK=0` and the harness behaves byte-for-byte like M3.
```

**Impact:** M4 features discoverable from main harness CLAUDE.md; users know they exist.

---

### 1.6 M3/M4 Cross-Reference Fix

**File:** `docs/milestones/complete/milestone3.md`  
**Location:** End of §0 (Implementation Status) or new §0.1.

**Add:**
```markdown
**M4 follow-up (Milestone 4, in-progress):** Slices D (permission_denied interrupt wiring) 
of M4 complete the S4 path-guard integration deferred here. When M4 merges, this gap is closed; 
`on_path_denied` escalates denials to the HITL approval loop. See 
`docs/milestones/in-progress/milestone4.md` (§11.3 "Escalation deferred") for current state.
```

**Impact:** Readers know M3 gaps are addressed in later work.

---

## PRIORITY 2: CODE CLEANUP (Moderate Impact, Low Effort)

### 2.1 Extract Middleware Compat Utility

**New file:** `deepagent-image/project/harness/_compat.py`

**Content:**
```python
"""Compatibility layer for optional dependencies.

Gracefully degrades when langchain is absent (e.g., on a bare test host).
"""

def compat_import(module_name: str, class_name: str):
    """Import a class, return object if the module is absent.
    
    Used when a module defines an AgentMiddleware but must work on a bare 
    host without langchain installed (e.g., for unit tests of pure logic).
    
    Args:
        module_name: Full module path, e.g. "langchain.agents.middleware.types"
        class_name: Class to import, e.g. "AgentMiddleware"
    
    Returns:
        The imported class, or object if ModuleNotFoundError.
    """
    try:
        mod = __import__(module_name, fromlist=[class_name])
        return getattr(mod, class_name)
    except (ImportError, ModuleNotFoundError, AttributeError):
        return object
```

**Update four files:**

| File | Lines | Old | New |
|------|-------|-----|-----|
| `cost.py` | 30–33 | try/except block | `AgentMiddleware = compat_import("langchain.agents.middleware.types", "AgentMiddleware")` |
| `archive.py` | 33–36 | same | same |
| `hitl.py` | 43–46 | same | same |
| `workflows.py` | 30–33 | same | same |

**Add at top of each file:**
```python
from harness._compat import compat_import
```

**Impact:** ~16 lines of boilerplate → 1 line per file. Easier to maintain, clear intent.

---

### 2.2 Refactor CLI Arg Parsing — Extract Env Defaults

**File:** `deepagent-image/project/harness/cli.py`  
**Location:** Before `parse_args()` function (line ~60).

**Add new function:**
```python
def _env_defaults() -> dict:
    """Build argparse defaults from environment variables."""
    return {
        "thread_id": os.getenv("DEEPAGENTS_THREAD_ID") 
                     or f"session-{datetime.now():%Y%m%d-%H%M%S}",
        "topic": os.getenv("DEEPAGENTS_TOPIC"),
        "headless": os.getenv("DEEPAGENTS_HEADLESS", "").strip().lower() in _TRUTHY,
        "max_cost": _env_float("DEEPAGENTS_MAX_COST"),
        "max_tokens": _env_float("DEEPAGENTS_MAX_TOKENS"),
        "task": os.getenv("DEEPAGENTS_TASK", "").split() if os.getenv("DEEPAGENTS_TASK") else [],
    }
```

**Modify `parse_args()`:** Replace individual `default=os.getenv(...)` calls with:
```python
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(...)
    defaults = _env_defaults()
    parser.set_defaults(**defaults)
    
    # Simpler add_argument calls, no inline env logic:
    parser.add_argument("task", nargs="*", help="Task for the agent...")
    parser.add_argument("--thread-id", help="Present thread id...")
    # ... etc
```

**Impact:** ~30-line reduction; centralizes env-var logic; easier to test.

---

### 2.3 Simplify Hostmap Input Collection

**File:** `deepagent-image/scripts/run-docker.sh`  
**Location:** Lines 63–76 (input collection).

**Current:**
```bash
_uname_s="$(uname -s 2>/dev/null || echo unknown)"
_is_wsl="$(_detect_is_wsl)"
_docker_os="unknown"
if [[ -z "${MAP_HOST_USER:-}" && "$_uname_s" == "Linux" && "$_is_wsl" != "1" ]]; then
  _docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo unknown)"
fi
if [[ "$(_should_map_host_user "$_uname_s" "$_is_wsl" "$_docker_os" "${MAP_HOST_USER:-}")" == "1" ]]; then
  ...
fi
```

**New (in `scripts/lib/hostmap.sh`):** Add wrapper function:
```bash
_should_map_host_user_auto() {
    """Auto-detect whether to map, collecting all inputs."""
    local uname_s="$(uname -s 2>/dev/null || echo unknown)"
    local is_wsl="$(_detect_is_wsl)"
    local docker_os="unknown"
    
    # Only probe docker on native Linux (skip the daemon call otherwise)
    if [[ "$uname_s" == "Linux" && "$is_wsl" != "1" && -z "${MAP_HOST_USER:-}" ]]; then
        docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo unknown)"
    fi
    
    _should_map_host_user "$uname_s" "$is_wsl" "$docker_os" "${MAP_HOST_USER:-}"
}
```

**Simplify call in `run-docker.sh`:**
```bash
if [[ "$(_should_map_host_user_auto)" == "1" ]]; then
    HOST_UID="${HOST_UID:-$(id -u)}"
    HOST_GID="${HOST_GID:-$(id -g)}"
fi
```

**Impact:** Caller logic shrinks; testability unchanged (pure function still exists for unit tests).

---

## PRIORITY 3: DOCUMENTATION ADDITIONS (Moderate Impact, Moderate Effort)

### 3.1 Canonical Env-Var Reference

**New file:** `deepagent-image/ENV_VARS.md`

**Structure:**
```markdown
# Environment Variables Reference

All container env vars for the harness. Set in `project/.env` (passed via `--env-file`) 
or at runtime (`docker run -e VAR=value`).

Launcher-side vars (host-side only, read by run-docker.sh/ps1 before `docker run`) 
are in [Launcher Environment](./CLAUDE.md#launcher-environment-host-side-not-env).

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
| `NET_JAIL` | `-NetJail` | Deny-all-egress network jail (see netjail/README.md) | off |
```

**Link from `.env.example` (new line at top):**
```
# Full environment variable reference: see ENV_VARS.md
# This file only shows examples for common settings.
```

**Link from deepagent-image/CLAUDE.md (in relevant sections):**
```markdown
See [ENV_VARS.md](./ENV_VARS.md) for the authoritative reference of all environment variables.
```

**Impact:** Single source of truth for all env vars; organized by concern; users know what exists.

---

### 3.2 Interactive Commands Reference

**File:** `deepagent-image/CLAUDE.md`  
**Location:** New subsection under "Commands (PowerShell...)" or new "REPL Commands" section.

**Add:**
```markdown
## Interactive REPL Commands (in-container)

When a session is running, type these at the `you>` prompt:

| Command | Purpose | Example |
|---------|---------|---------|
| `/exit` | End session deterministically (no LLM involved) | `/exit` |
| `/quit` | Alias for `/exit` | `/quit` |
| `Ctrl-D` | EOF — end session | — |
| `Ctrl-C` | At idle prompt: end session. During turn: cancel that turn. | — |
| `/topic <name>` | Set or change the continual-topic label for this run (defaults to DEEPAGENTS_TOPIC) | `/topic refactor-auth` |
| `/recall [query] [--all]` | Recall past sessions. No query lists recent. Query stages context for the next turn. `--all` widens to whole archive. | `/recall authentication issue` |
| `/show` | Expand a truncated interrupt/approval context (HITL) | (used when an approval prompt is capped) |
| `/refresh [subpath]` | Pull live host edits into ephemeral workspace copy (ephemeral mode only). Omit subpath to refresh root. | `/refresh src/` |

## Admin Commands (keyless, outside container)

Run these in the host shell; they manage the persistent harness state:

```bash
# List or manage threads (present conversation)
harness threads list [--topic LABEL]
harness threads show <thread-id>
harness threads rm <thread-id> [--yes]      # requires --yes to confirm
harness threads prune [--keep N] [--yes]    # keep N most recent, delete rest

# List or manage past sessions (archive)
harness past list [--topic LABEL]
harness past show <run-id>
harness past rm <run-id> [--yes]
harness past prune [--keep N] [--yes]
harness past topics                         # list all topics
```

(Requires the harness venv: `source deepagent-image/.venv/bin/activate` or install locally)
```

**Impact:** Users discover commands without guessing or reading source.

---

### 3.3 Feature Toggles Table

**File:** `deepagent-image/CLAUDE.md`  
**Location:** New subsection after "Commands..." sections.

**Add:**
```markdown
## Feature Toggles & Removable Contracts

Each milestone adds opt-in or removable features. This table shows which are on by default, 
how to disable them, and what behavior they enable/disable:

| Feature | Milestone | Env Var | Default | When to Set to 0 | Behavior When Off |
|---------|-----------|---------|---------|------------------|------------------|
| Past Archive | M2 | `DEEPAGENTS_ARCHIVE` | 1 | Never; use `/recall` when you don't need past runs | Sessions not recorded in `past.sqlite`; `/recall` returns nothing |
| Workspace Masking | M4 | `DEEPAGENTS_MASK` | 1 | Testing/debugging; or trusting the workspace | Agent can read all files; no empty overlays |
| Cost Tracking | M1 | n/a (auto) | on | Never; budgets are optional | No per-turn usage line; budgets ignored |
| HITL | M3 | n/a (config file) | off | (not set by env) | Only if `.harness-config.yaml` exists in project root | No approval gates; agent runs freely |

**Removable contract:** Each "off" state is byte-for-byte identical to the prior milestone 
(see [Glossary](../docs/README.md#glossary)). E.g., `DEEPAGENTS_MASK=0` ⇒ M3 parity.
```

**Impact:** Clearer for users which features are active, how to toggle them.

---

### 3.4 Quick-Start: `.agentignore` Examples

**File:** `deepagent-image/CLAUDE.md`  
**Location:** New subsection under "Workspace visibility / secret masking (Milestone 4)".

**Add (after the knobs table):**
```markdown
### Quick-Start: In-Workspace `.agentignore` File

The `.agentignore` file (gitignore syntax) in your workspace root controls which paths 
the agent sees. Default globs (`.env`, `*.pem`, `.aws/credentials`, etc.) are **always 
masked** — even `.agentignore` cannot unmask them.

**Example 1: Deny mode (default) — Agent sees everything except masked paths:**
```
# .agentignore (in workspace root)

# Mask additional sensitive files
private/notes.md
config/secrets.yaml

# Unmask a default glob (deny mode only):
!.env.local                    # this .env variant is readable

# Designated-secret floor (can never be negated):
#!floor:
id_rsa
.ssh/
~/.aws/credentials
#!floor-end
```

**Example 2: Allow mode — Agent sees *only* what you explicitly allow:**
```
# .agentignore with #!mode:allow

#!mode:allow

# Paths the agent CAN see — PLAIN patterns, not negation. In allow mode a
# plain pattern match IS the allow-list entry (the resolver flips it visible);
# `!`-negation has no special meaning here and does NOT allow-list a path.
src/
tests/
README.md
package.json

# Everything else is hidden (including defaults like .env)
```

**Defaults (always masked, can't be unmasked):**
```
.env, .env.*, *.pem, *.key, id_rsa, id_ed25519, .ssh/, .aws/credentials, .netrc,
.npmrc, .git-credentials, credentials.json, *.p12, *.pfx
```

See `docs/features/workspace_visibility.md` (§3, full syntax) for advanced rules.
```

**Impact:** Users can immediately write an `.agentignore` without reading the full spec.

---

## PRIORITY 4: SCRIPT PAIR SYNCHRONIZATION DOCS (Low Impact, Low Effort)

### 4.1 Script Pair Maintenance Note

**File:** Root `CLAUDE.md`  
**Location:** New subsection under "Build & run (PowerShell primary...)".

**Add:**
```markdown
### Script Pair Maintenance (`.ps1` ↔ `.sh`)

This repo maintains **parallel PowerShell and Bash scripts** for cross-platform compatibility:
- `build.ps1` ↔ `build.sh`
- `verify.ps1` ↔ `verify.sh`
- `smoke.ps1` ↔ `smoke.sh`
- `run-docker.ps1` ↔ `run-docker.sh`

**Sync rule:** When you edit one, **keep the pair in sync**. Both must implement the 
same logic and support the same flags. This is a known maintenance burden; a 
cross-platform wrapper was considered but rejected due to the depth of Windows/Unix 
differences (file paths, robocopy vs. rsync, registry vs. env var probing, etc.).

**Known sync points (track when editing):**
- `run-docker.ps1` ↔ `run-docker.sh`: ephemeral workspace copy, NetJail setup, state-dir derivation.
- `smoke.ps1` ↔ `smoke.sh`: pytest invocation, image staging, artifact handling.
- Both: launcher environment defaults (CPUS, MEMORY, PIDS_LIMIT).

**Verification:** `./scripts/check-parity.sh` (bash) / `.\scripts\check-parity.ps1` (ps1) 
validates critical sections match (see script for the parity rules).
```

**Impact:** Explicitly acknowledges the burden; guides new contributors; flags high-sync areas.

---

## Implementation Order

1. **Priority 1** (docs): 1 day
   - 1.1–1.6: Consolidate / redirect / archive docs.
   - Update all milestone docs to use glossary ref.
   - Largest immediate win: removes confusion, single sources of truth.

2. **Priority 2** (code cleanup): 1 day
   - 2.1–2.3: Extract utility, refactor args, simplify hostmap.
   - Low risk: no behavior change.
   - High clarity: fewer lines, clearer intent.

3. **Priority 3** (docs additions): 1.5 days
   - 3.1–3.4: New canonical references + quick-starts.
   - High impact for new users.
   - One-time content creation, no ongoing sync burden.

4. **Priority 4** (script docs): 0.5 days
   - 4.1: Document the pair sync burden + verification tool.
   - Prevents future confusion.

**Total estimated effort:** 4 days.

---

## Acceptance Criteria

- [ ] All milestone docs use glossary forward-ref for "removable contract" (≤1 occurrence per doc).
- [ ] `.env.example` has provider explanations for every key.
- [ ] `deepagent-image/ENV_VARS.md` exists, canonical, linked from `.env.example` + CLAUDE.md.
- [ ] M4 config knobs documented in `deepagent-image/CLAUDE.md` with examples.
- [ ] REPL + admin command reference in `deepagent-image/CLAUDE.md`.
- [ ] `.agentignore` quick-start in `deepagent-image/CLAUDE.md` (examples given).
- [ ] Feature toggles table in `deepagent-image/CLAUDE.md`.
- [ ] Script pair sync burden documented in root CLAUDE.md + `check-parity.*` referenced.
- [ ] `harness/_compat.py` extract refactors 4 modules (cost, archive, hitl, workflows).
- [ ] CLI arg parsing refactored: `_env_defaults()` extracted, no behavior change.
- [ ] Hostmap wrapper added to `scripts/lib/hostmap.sh`, caller in `run-docker.sh` simplified.
- [ ] IMMEDIATE_TODO.md marked as complete / archived.
- [ ] All changes pass tests (no test changes needed; all are docs/refactor).
- [ ] No functional changes; all shifts are documentation or code organization.
