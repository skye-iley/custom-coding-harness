# Design Document: Custom Coding Harness with Deep Agents

## 1. System Overview & Objectives
*   **Purpose**: Multi-agent orchestration platform for autonomous, sandboxed code generation using the Deep Agents framework.
*   **Execution Target**: Ubuntu-based Docker container with host-persistent workspace and config mounts.
*   **Core Capabilities**:
    *   **Intelligent Routing**: Local FSM-constrained classifier routing to local or cloud expert orchestrators.
    *   **Dynamic Sandboxing**: Per-agent restricted file access via `bubblewrap` and `HarnessProfile` scoping.
    *   **Custom Workflows**: User-defined deterministic (or lightweight-classifier-gated) units that run scripts/tools and read or mutate prompt/context/state, bound to chosen points in the session lifecycle (see §3). Git automation and model routing are both instances of this engine.
    *   **Git Automation**: Deterministic branch lifecycle with mandatory human pre-review for PRs (no auto-merge) — a built-in workflow on the §3 engine.
    *   **Token Optimization**: Multi-layer compression pipeline utilizing `Headroom` (CCR), `Caveman` (Terseness), and Prompt Caching.
    *   **Provider Agnostic**: Unified model interface supporting local (Ollama/LMStudio) and cloud (Claude/GPT) providers.
    *   **Observability**: Full telemetry for token usage, financial cost, routing accuracy, and session effectiveness.

---

## Implementation Status — Built vs. Planned

> **Read this first.** This document is the *target design*, not a description of current code.
> Most of it is **not built yet**. The table below is the source of truth for what exists today;
> everything else is aspirational. Where a section below is not ✅ here, read it as a spec to build
> against. Status legend: ✅ Built · 🟡 Partial · ⬜ Planned · 🔬 Research.

**Built today (the MVP):** a single Docker image (`deepagent-harness`: Ubuntu 24.04 + uv venv +
Miniforge) that runs **one** `create_deep_agent` against a bind-mounted workspace. Model selection
is the `PROVIDERS` registry in `project/harness/providers.py`, loaded at import time from the
on-disk `project/providers/` TOML registry (explicit, or auto-selected by which API key is set —
**there is no classifier**). It loads MCP tools (`.mcp.json`), runs lifecycle shell hooks
(`hooks.json`), persists conversation state to a per-workspace SqliteSaver checkpoint (keyed by
thread id), isolates workspace dependencies in a workspace-local conda env, and receives secrets via
`--env-file` at run time. That is roughly the §1/§4 provider layer + the built parts of §2/§3.

| § | Capability | Status | Notes |
|---|------------|--------|-------|
| 1, 4 | Provider-agnostic model interface | ✅ Built | `PROVIDERS` registry; native openai/anthropic/google/deepseek + OpenAI-compatible cursor/openrouter/lmstudio |
| 1, 4 | On-disk provider/model registry (`providers/` TOML) | ✅ Built | `PROVIDERS` loaded from `project/providers/<provider>/provider.toml` + `models/*.toml` at import; add/change via TOML, no Python edit. `DEEPAGENTS_PROVIDERS_DIR` overrides (tests) |
| 1, 4 | `sync-models` registry refresh | ✅ Built | `python3 -m harness sync-models` (`harness/sync_models.py`, `scripts/sync-models.{sh,ps1}`) regenerates `models/*.toml` from provider list-models endpoints. **Dev-time only** (needs keys + network); never edits `provider.toml` |
| 1, 4 | FSM classifier routing (local↔cloud) | 🔬 Research | No classifier exists; routing is explicit or auto-by-API-key. >95% accuracy target has no baseline |
| 2 | Single container (uv venv + conda) | ✅ Built | `Dockerfile` |
| 2 | Workspace conda env isolation | ✅ Built | workspace-local `.conda/env`, `run-in-env.sh` |
| 2 | Secret provisioning (`--env-file`) | ✅ Built | `.env` gitignored, never baked into image |
| 2 | Persistent workspace + gitconfig mount | ✅ Built | `run-docker` bind-mounts workspace; mounts `~/.gitconfig` read-only |
| 2 | Conversation checkpoint (SqliteSaver) | ✅ Built | per-workspace `.deepagents/checkpoints.sqlite`, thread-keyed |
| 2 | Dual-container (orchestrator + executor) | ⬜ Planned | One container today |
| 2 | Bubblewrap executor jail | 🟡 Partial | `scripts/sandbox-exec.sh` + `bwrap` installed in image, but **not wired into agent shell calls** and unverified at runtime (no `--security-opt` in `run-docker`) |
| 2 | `HarnessProfile` dynamic bind mounts | ⬜ Planned | Fixed bind list; no per-agent profile |
| 2 | Path Guard middleware (`validate_path`) | ⬜ Planned | Snippet only; not in `main.py` |
| 2 | Resource limits (`--cpus`/`--pids-limit`/mem) | ✅ Built | `run-docker.{sh,ps1}` set `--cpus`/`--memory`/`--pids-limit` (defaults 2/4g/512, overridable). Docker host-boundary control, not a sandbox |
| 3 | Workflow lifecycle hooks (`hooks.json`) | ✅ Built | `ShellHooksMiddleware` (session/agent/model/tool events) — fire-and-forget shell side-effects, output/return ignored |
| 3 | Conditional / classifier-gated triggers + context mutation | ⬜ Planned | Hooks fire unconditionally today; no per-event predicate, no classifier gate, no prompt/context rewrite from a workflow |
| — | MCP tool loading (`.mcp.json`) | ✅ Built | `load_mcp_tools` (not a separate doc section) |
| 3 | Git branch/commit/push/PR lifecycle | ⬜ Planned | No git automation in `main.py` |
| 5 | Multi-agent funnel (classifier→orchestrator→worker) | ⬜ Planned | Single `create_deep_agent` today |
| 6 | Token/cost tracker | ✅ Built (Milestone 1) | `harness/cost.py` (`CostTrackerMiddleware`); pricing in the `providers/` TOML registry (`[pricing]` per model, strategy per provider), not a `prices.json`. Optional energy estimate + budgets. See `design_doc_milestone1.md` |
| 7 | Headroom / Caveman / caching pipeline | ⬜ Planned | Nothing integrated |
| 8 | Observability, telemetry, telemetry-to-PR | ⬜ Planned | No trace/metrics files written |
| 9 | In-container interactive REPL (multi-turn session) | 🟡 MVP | Persistent `docker run -it` prompt loop in `harness/cli.py`: multi-turn on one `thread_id`, deterministic `/exit`, stage output — see `design_doc_mvp.md` §1a |
| 9 | Host CLI frontend (Typer/Rich) + TUI | ⬜ Planned | No `harness` CLI/TUI; interactive use is the in-container REPL above |
| 9 | HITL autonomy config (`.harness-config.yaml`) | ⬜ Planned | — |
| 10 | Security verification test suite | ⬜ Planned | Risk analysis is design-only |
| 11 | Future extensions & roadmap | 🔬 Research | By definition |

---

## 2. Sandboxing Strategy & Container Layout
> **Status:** 🟡 Partial — single container + conda isolation + secret provisioning + persistent workspace built; dual-container, bubblewrap jail (built but not wired in), `HarnessProfile` binds, path guard, and resource limits **planned**. See the status matrix above.

### Dual-Container Boundary
*   **Orchestrator Container**: Hosts Deep Agents runtime and coordinates agent execution. No mount to host Docker socket (`/var/run/docker.sock`).
*   **Executor Sandbox**: Commands run inside ultra-restrictive nested jail powered by `bubblewrap` (bwrap).

### Bubblewrap Configuration

> **Prerequisites.** `bubblewrap` is installed in the orchestrator Dockerfile
> (`apt-get install -y bubblewrap`), and `scripts/sandbox-exec.sh` implements the two-phase wrapper
> below (installed as `/usr/local/bin/sandbox-exec`). Still unverified at runtime: `bwrap` nested inside
> Docker also needs unprivileged user namespaces enabled on the host *and* the container must not
> block the `clone`/`unshare` syscalls. In practice that means running the orchestrator with
> `--security-opt seccomp=unconfined` (or a custom profile that permits `unshare`/`clone` with
> `CLONE_NEW*`), or enabling `kernel.unprivileged_userns_clone=1`. **Validate that `bwrap
> --unshare-all true` actually runs in the deployed image before building features on top of it** —
> if it fails silently, the executor is not sandboxed.

Shell tool executions wrapped dynamically via host execution template. The bind-mount list is generated per-agent based on the `HarnessProfile`:
```bash
bwrap \
  --ro-bind /usr /usr \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin \
  --ro-bind /sbin /sbin \
  --dir /tmp \
  --proc /proc \
  --dev /dev \
  {DYNAMIC_BIND_MOUNTS} \
  --chdir /workspace \
  --unshare-all \
  --uid 1000 \
  --gid 1000 \
  /bin/bash -c "{agent_command}"
```
*   `{DYNAMIC_BIND_MOUNTS}`: Replaces the global `/workspace` bind.
    *   **Full Access**: `--bind /workspace /workspace`
    *   **Scoped Access**: `--bind /workspace/src/components /workspace/src/components` (only allows access to specific sub-dirs).
    *   **Read-Only Scoping**: `--ro-bind /workspace/docs /workspace/docs`.
*   `--unshare-all`: Isolates net, ipc, uts, pid, user namespaces.
*   `--ro-bind`: Prevents injection or corruption of system paths.

> **Two execution phases — network must be conditional, not always-off.** `--unshare-all` removes
> the network namespace, which is correct for running *agent-authored code* (defense against
> exfiltration). But dependency resolution (`pip install`, `conda env create`, `npm install`) needs
> the network, and it runs through the same execution wrapper. Resolve by splitting into two
> profiles instead of a blanket `--unshare-all`:
> *   **Install phase** — network allowed (omit `--unshare-net`; keep `--unshare-user/pid/ipc/uts`),
>     writes limited to the workspace env dir (`/workspace/.conda`, caches). Invoked explicitly for
>     setup steps.
> *   **Execution phase** — full `--unshare-all` (no NIC) for running tests and agent-authored
>     commands.
>
> The `HarnessProfile` selects the phase; the default for arbitrary agent commands is the
> network-isolated execution phase.

### Path Guard Middleware
Pre-flight check in Python tool execution class prevents symlink escapes and directory traversal:
```python
import os

def validate_path(target_path: str, base_dir: str = "/workspace") -> str:
    abs_target = os.path.realpath(target_path)
    abs_base = os.path.realpath(base_dir)
    # Use commonpath, NOT startswith: startswith("/workspace") also matches
    # "/workspace-evil", allowing a sibling-directory escape.
    if os.path.commonpath([abs_target, abs_base]) != abs_base:
        raise PermissionError("Path out of sandboxed workspace bounds")
    return abs_target
```
> **Defense-in-depth only.** This check is racy (TOCTOU: a symlink can be swapped between
> `realpath` validation and the actual open). It is a guard rail, not the security boundary — the
> bubblewrap bind-mount whitelist is the real boundary. Where possible, open with `O_NOFOLLOW` /
> resolve-and-hold a file descriptor rather than re-deriving the path after the check.

### Workspace Environment Isolation
To prevent dependency conflicts between the orchestrator runtime and the target project:
*   **Decoupled Runtimes**: The Orchestrator runs in a fixed system-level Python virtual environment.
*   **Local Environment Manager**: Utilize Miniforge/Conda within the container to manage workspace-specific dependencies.
*   **Workspace-Local Envs**: Environments are stored within the persistent workspace directory (e.g., `/workspace/.conda/env`), ensuring they survive container restarts.
*   **Execution Wrapper**: All agent-triggered shell commands are routed through a wrapper that automatically activates the workspace-local environment before execution.

### Persistent Storage Strategy

To ensure workspace and configuration persistence across container deployments:
*   **Workspace Mounting**: Start container with `-v /host/path/to/workspace:/workspace`. All agent work inside `/workspace` is automatically persisted on host filesystem.
*   **Config Persistence**: Mount config into the **agent** user's home, not `/root` — the container
    runs `USER agent` (uid 10001), so `/root`-targeted mounts are unreadable and ignored:
    *   `-v /host/.gitconfig:/home/agent/.gitconfig:ro`
    *   `-v /host/agent-telemetry/{session-id}.json:/workspace/.agent_telemetry.json` (per-session
        file; do **not** share one host file across concurrent sessions — they race on writes).

> **Do not mount host `~/.ssh` into an autonomous-agent container.** Full private-key exposure to a
> codegen agent contradicts the secret-scrubbing posture (§10) and is the highest-impact escape
> path if the agent is compromised. For Git push, use one of:
> *   a **scoped, per-session deploy key** (write access to a single repo) injected as a secret and
>     removed at teardown, or
> *   a **short-lived token** via a credential helper (`gh auth` / `GIT_ASKPASS`), never persisted to
>     disk in the workspace.

This guarantees agent session state, Git identity, and accumulated metrics survive container teardown.

### Resource Limits
> **Status:** 🟡 Partial (Milestone 1) — CPU / memory / PID caps are **built**:
> `run-docker.{sh,ps1}` pass `--cpus`/`--memory`/`--pids-limit` (defaults
> 2/4g/512, overridable). Disk quota and wall-clock timeouts below remain
> **planned**. Docker host-boundary control, not a sandbox (`design_doc_mvp.md` §5).

Isolation is not just filesystem/network — an agent loop can also exhaust host resources. Apply
hard caps at container start (independent of bubblewrap):
*   **CPU / memory**: `docker run --cpus=N --memory=Mg --memory-swap=Mg` to bound a runaway loop.
*   **Processes**: `--pids-limit=512` to contain fork bombs (the executor also has its own PID
    namespace via `--unshare-pid`).
*   **Disk**: cap workspace growth (e.g. a size-limited volume or quota) so logs/build artifacts
    cannot fill the host disk.
*   **Wall-clock**: a per-session and per-command timeout (the harness kills the session after
    `max_session_seconds` / a single command after `max_command_seconds`) to stop infinite loops.

### Secret Provisioning

API keys and tokens must reach the orchestrator without leaking to the agent or to disk:
*   **Runtime injection only**: pass secrets via `--env-file project/.env` at `docker run` time.
    **Never bake them into the image** — `.dockerignore` excludes `.env*` from the build context.
*   **Not in the workspace bind**: secrets live in the orchestrator's environment, never under
    `/project/workspace` (which is host-mounted and visible to the agent's tools).
*   **Scoped Git credentials**: use a per-session deploy key or short-lived token for pushes, not
    long-lived host SSH keys (see Config Persistence above).
*   **Scrubbing on the way out**: trace/metrics writers mask key-shaped strings before persisting
    (see §10 *Telemetry Leakage*).

---

## 3. Custom Deterministic Workflows & Git Lifecycle
> **Status:** 🟡 Partial — the shell lifecycle hook engine (`hooks.json` → `ShellHooksMiddleware`) is
> built, but only as fire-and-forget side-effects fired unconditionally on every event. Conditional /
> classifier-gated triggers and workflow-driven context mutation are **planned**; the git
> branch/commit/push/PR automation below (the canonical workflow) is **not** built. See the status
> matrix above.

### Workflow Engine (the general abstraction)
A **custom workflow** is a user-defined unit that runs at a chosen point in the session lifecycle.
Git automation (below) and model routing (§4) are not bespoke subsystems — they are the two
first-party workflows on this one engine. A workflow is fully described by three things:

1.  **Trigger — *when* it may run.** Bound to one lifecycle event (the *hook point*, below). On that
    event the engine evaluates a user-set **gate** before running the body:
    *   **Deterministic predicate** — a condition over available state: a prompt regex/keyword, a
        file or `git status` check, an env var, a turn/cost counter, or `always`. Pure, no model call.
    *   **Classifier gate** — a lightweight, constrained-output agent (the §4 traffic classifier is
        the reference case) emitting one token from a fixed set; the workflow runs only on a matching
        verdict. Used when the decision needs intent/complexity judgement a predicate can't express.
    A workflow with `always` + a shell body is exactly today's `hooks.json` entry.

2.  **Hook point — *where* in the lifecycle it binds.** The same events `ShellHooksMiddleware`
    already exposes, named by intent:
    *   `session.start` / `session.end` — once around the whole run (fire in `cli.main()`).
    *   `agent.start` / `agent.end` — once per user input (per `.invoke`).
    *   `model.start` — **before the prompt/context is sent to the model** (the injection/rewrite and
        routing point); `model.end` — after each LLM call (every reasoning step).
    *   `tool.start` / `tool.end` — around each tool execution.
    "Between turns" = `agent.end` of turn *N* into `agent.start` of *N+1*; "before context is sent"
    = `model.start`.

3.  **Action — *what* it does.** In capability order:
    *   **Side-effect** *(built)* — run a shell command, script, or tool; result discarded
        (`subprocess.run(..., check=False)`). This is the whole of `hooks.json` today.
    *   **Context mutation** *(planned)* — read and rewrite the outgoing prompt/context or shared
        state (inject a system note, redact, compact, attach retrieved files) before it reaches the
        model. Requires hooks to *return* a value the middleware applies, not just fire.
    *   **Control-flow** *(planned)* — alter dispatch: select/swap the model (model routing),
        short-circuit a turn, or veto a tool call.

> **Built vs. planned.** Today: unconditional shell side-effects on the seven events above. Planned:
> the gate layer (predicate + classifier) and the context-mutation / control-flow action tiers — the
> work that turns "run a command on an event" into "run a *workflow*."

### Workflow format (on disk)
Each workflow is a **self-contained folder named after the workflow**, discovered under a
`workflows/` root (sibling to skills/agents; `project/workflows/<name>/` in the harness, overridable
via `DEEPAGENTS_WORKFLOWS_DIR`). The folder is the unit you copy, version, and share — same shape
every time:

```
workflows/
  git-session-lifecycle/        # folder name == workflow name
    workflow.md                 # manifest: description + ordered plan (required)
    trigger.sh                  # gate, fixed name, lives in the folder (required)
    create-branch.sh            # a step script (optional, local)
    open-pr.sh                  # a step script (optional, local)
```

*   **`workflow.md` — the manifest (required).** A short prose description, plus frontmatter that
    *is* the trigger × hook × action triple made concrete:
    ```markdown
    ---
    name: git-session-lifecycle     # must equal the folder name
    hook: session.start             # one of the 7 hook points above
    gate: trigger.sh                # the fixed-name gate; always ./trigger.sh in this folder
    steps:                          # run in listed order, only if the gate passes
      - ./create-branch.sh          # relative path → resolved against the workflow folder
      - ./open-pr.sh
      - /opt/harness/git/notify.sh  # absolute path → run as-is
    ---
    Branch at session start, open a PR at session end. Never auto-merges.
    ```
*   **`trigger.sh` — the gate (required, standard name).** **Every** workflow folder has a gate file
    with the *same fixed name* (`trigger.sh`), always resolved inside the folder — never elsewhere,
    never renamed. The engine runs it on each fire of `hook` and reads its **exit code: `0` = run the
    steps, non-zero = skip** (the one contract for both gate kinds — a deterministic predicate is a
    few lines of shell; a classifier gate is `trigger.sh` shelling out to the classifier and exiting
    on its verdict). A shebang lets it be any interpreter; the name stays `trigger.sh`.
*   **Steps — the action (ordered).** Listed in `steps:` and run top-to-bottom only after the gate
    passes. Each entry is a path: **relative paths resolve against the workflow folder** (the common
    case — keep step scripts beside `workflow.md`), **absolute paths run as-is** (share one script
    across workflows, or call into the harness). Steps may therefore live in the folder *or* anywhere
    on disk; `trigger.sh` may not.

This is the planned authoring format; today's `hooks.json` is the flat precursor (one event → one
unconditional command, no folder, no gate). The git lifecycle below is the first workflow expressed
this way.

### Canonical workflow: Git session lifecycle
A deterministic, `session.start`/`session.end` workflow whose body is git side-effects — the
worked example the engine above generalizes.

### Deterministic Branching
*   **Naming Pattern**: `agent/{provider}/{session-id}`
*   **Session ID**: `sha256(user_id + workspace_path + timestamp)[:12]`
    *   The `timestamp` makes each session unique (avoids branch-name collisions across runs on the
        same workspace). Compute the `session-id` **once at session init and persist it**; teardown
        must reuse the stored value, not recompute it — otherwise the pushed branch name won't match
        the one created at start.

### Git Lifecycle Flow

```
[Start Session] ──> Fetch origin/main ──> Create branch agent/cloud/fe89a2
                                                               │
[Agent Edits]   ──> Track tokens/cost ──> Run sandbox command  │
                                                               ▼
[End Session]   ──> Stage mutations ──> Push branch ──> Create GitHub PR
```

1.  **Session Start (Init)**:
    *   Assert workspace is clean: `git status --porcelain` (stash/abort if dirty).
    *   Retrieve remote main: `git fetch origin main`
    *   Checkout target branch: `git checkout -b agent/{provider}/{session-id} origin/main`
    *   Record base commit hash as parent revision pointer.
2.  **Session Close (Teardown)**:
    *   Stage local changes (excluding `.agent_telemetry` and blocklisted configs): `git add .`
    *   Commit changes: `git commit -m "agent(session-{session-id}): automated codebase mutations"`
    *   Push to upstream: `git push origin agent/{provider}/{session-id}`
    *   Invoke GitHub PR API or execute `gh` CLI:
        ```bash
        gh pr create \
          --title "Agent Run: {session-id}" \
          --body "Automated PR generated by Deep Agents. Cost={cost}, Tokens={tokens}. **Note: Manual review required. Auto-merge disabled.**" \
          --base main \
          --head agent/{provider}/{session-id}
        ```
    *   **Merge Policy**: The system must **never** auto-merge PRs. Human pre-review is mandatory before main branch integration.

---

## 4. Model Routing & Provider Abstraction
> **Status:** 🟡 Partial — the `PROVIDERS` provider abstraction is built; the FSM traffic classifier and local/cloud orchestrator split are **planned/research** (see the MVP-framing note below). See the status matrix above.

> **As a workflow.** Routing is the reference **classifier-gated, control-flow** workflow on the §3
> engine: bound at `session.start` / `model.start`, gated by the traffic classifier below, acting by
> selecting the model. The pipeline here is the concrete instance; §3 is the general shape.

### Router Architecture
*   **Deep Agents API**: Utilizes `create_deep_agent` from the `deepagents` package to initialize agents with defined backends, profiles, and middleware.
*   **Traffic Classifier (The Gatekeeper)**:
    *   **Model**: Lightweight local LLM (e.g., Qwen2.5-Coder-7B).
    *   **Analysis**: Parses prompt for complexity, intent, and required scope.
    *   **Constraint**: Implements **Constrained Output (FSM)**. The model is forced to output exactly one category token from the set `{TRIVIAL, MODERATE, COMPLEX, CRITICAL}`. No reasoning or prose is permitted.
    *   **Categories**:
        *   `TRIVIAL`: formatting, simple regex, documentation updates $\rightarrow$ Local Orchestrator.
        *   `MODERATE`: Single-file logic changes, bug fixes in isolated functions $\rightarrow$ Local Orchestrator.
        *   `COMPLEX`: Cross-file refactoring, architectural changes, new feature design $\rightarrow$ Cloud Orchestrator.
        *   `CRITICAL`: Security patches, core system migrations $\rightarrow$ Cloud Orchestrator (highest reasoning model).

> **MVP framing.** The local-classifier routing in this section (FSM-constrained Qwen, knowledge
> distillation, the >95% routing-accuracy target in §10) is a later-phase research track, not the
> first deliverable — and >95% is aspirational with no baseline yet. For the MVP, route by an
> explicit signal (caller-supplied category, or a single cloud model) and treat the classifier as an
> optimization to add once there is a labeled "Gold Set" to measure it against.

*   **Expert Orchestrator Roles**:
    *   **Local Orchestrator**: Optimized for speed and cost. Handles `TRIVIAL` and `MODERATE` tasks.
    *   **Cloud Orchestrator**: Optimized for reasoning. Handles `COMPLEX` and `CRITICAL` tasks.
    *   **Specialized Profiles**: `HarnessProfile` defines specific toolsets for roles (e.g., *Architect* has full project read access; *Coder* has limited write access to specific directories).
*   **Routing Pipeline**:
    1.  **Ingestion**: Prompt arrives $\rightarrow$ Classifier analyzes.
    2.  **Decision**: Classifier outputs category $\rightarrow$ Router selects `model` string (e.g., `local-coder#main` vs `claude-3.5#main`).
    3.  **Dispatch**: `create_deep_agent` is called with the selected model, `LocalShellBackend`, and appropriate `HarnessProfile`.
    4.  **Execution**: Selected orchestrator takes control of the session.
*   **Provider Config**:
    *   Model-specific configurations defined via the `model` parameter in `create_deep_agent` (e.g., `model="gpt-4#agent-tag"`).
    *   Backend environments are inherited through `LocalShellBackend` configuration, ensuring consistent execution context.
    *   **Built today:** the provider/model set is the on-disk `project/providers/` TOML registry
        (`<provider>/provider.toml` + `models/<model>.toml`), loaded into `PROVIDERS` by
        `harness/providers.py` at import. Per-model metadata (context window, output limit, and —
        for some providers — pricing) is pulled by the dev-time `sync-models` command into the model
        TOMLs; the loader ignores unknown keys, so new metadata is non-breaking. See
        `project/providers/README.md`.


---

## 5. Agent Architecture & Framework Comparison
> **Status:** ⬜ Planned — a single Custom `create_deep_agent` runs today; the classifier→orchestrator→worker multi-agent funnel is **not** built. See the status matrix above.

### Structural Options
*   **Base Deep Agents Core**:
    *   *Nature*: Pre-configured agent templates provided by the `deepagents` library.
    *   *Pros*: Rapid deployment, stable tool-handling, minimal configuration.
    *   *Cons*: Limited flexibility in reasoning loops; potential token inefficiency for niche tasks.
    *   *Best For*: Generic worker agents (e.g., "File Reader", "Test Runner").

*   **Pi Agent Framework**:
    *   *Nature*: Hyper-lightweight, prompt-driven agents focusing on minimal state and high-speed response.
    *   *Pros*: Extremely low token overhead, high latency performance.
    *   *Cons*: Lower reasoning depth; prone to loop failures on complex logic.
    *   *Best For*: The Traffic Classifier and trivial task handlers.

*   **Custom Agents (via `create_deep_agent`)**:
    *   *Nature*: Bespoke agents utilizing custom `HarnessProfile`, specific `middleware`, and tailored system prompts.
    *   *Pros*: Absolute control over tool access, output formatting, and reasoning steps.
    *   *Cons*: Higher development overhead; requires manual tuning of prompts.
    *   *Best For*: Expert Main Orchestrators.

*   **Hybrid Combination (Recommended)**:
    *   **Classifier (Pi-style)** $\rightarrow$ **Orchestrator (Custom)** $\rightarrow$ **Worker (Base)**.
    *   Ensures a "funnel" of efficiency: fast classification, precise planning, and reliable execution.

### Alternative Lightweight Patterns
*   **Plan-and-Execute**: Decouples the planning phase from the execution phase. Prevents the agent from "forgetting" the goal during long tool-call sequences, reducing redundant tokens.
*   **Finite State Machine (FSM) Agents**: Use deterministic state transitions for specific workflows (e.g., "Branch $\rightarrow$ Edit $\rightarrow$ Test $\rightarrow$ PR"). Eliminates reasoning overhead for predictable paths.
*   **ReAct (Reason+Act)**: The standard iterative loop. Useful for discovery but token-heavy; should be reserved for `COMPLEX` tasks.

---

## 6. Token Usage & Cost Tracker
> **Status:** ✅ Built (Milestone 1) — `harness/cost.py` (`CostTrackerMiddleware`)
> reports per-turn + session tokens/cost/energy and enforces optional budgets.
> Pricing lives in the `providers/` TOML registry (per-provider strategy +
> per-model `[pricing]` table incl. split cache prices), not a `prices.json`. A
> priced model with no rate is loud (warn-once + floor), never silent `$0`. An
> optional per-model energy estimate is tracked (measured local-device energy is
> specified, not built — see `ENERGY_SPEC.md`). See `design_doc_milestone1.md`.

### Callback Implementation
Deep Agents provides hooks to monitor execution streams:
```python
# Deep Agents tracking implementation utilizing underlying framework callbacks
class TokenCostTracker:
    def on_llm_end(self, response, **kwargs) -> None:
        # Accumulate prompt_tokens, completion_tokens, and query pricing model
        pass
```

### Reference Schema (`prices.json`)
Local dictionary for calculating financial cost of session:
```json
{
  "anthropic/claude-3-5-sonnet-20241022": {
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015
  },
  "openai/gpt-4o": {
    "input_cost_per_token": 0.0000025,
    "output_cost_per_token": 0.000010
  }
}
```
> **Staleness & coverage caveats.** Hardcoded prices drift as providers change rates — treat
> `prices.json` as a versioned snapshot (record its date; refresh on a schedule) and fail loudly on
> a model key that isn't present rather than silently costing it at `0`. Also account for tokens the
> simple prompt/completion split misses: **cached-input tokens** (Anthropic prompt caching, billed at
> a reduced rate) and any reasoning/thinking tokens, or per-task cost will read low.

---

## 7. Token Optimization Pipeline (Headroom & Caveman)
> **Status:** ⬜ Planned — neither Headroom, Caveman, nor prompt caching is integrated. See the status matrix above.

### Headroom Context Compression Layer
*   **Tool**: Integrate `chopratejas/headroom` inside the orchestrator container environment.
*   **Integration Vectors**:
    *   **Proxy Mode**: Execute `headroom proxy --port 8787` inside Docker. Route all Deep Agents outbound model traffic through the local proxy endpoint.
    *   **Adapter**: Bind via Deep Agents model wrapping to transparently prune outgoing messages.
    *   **Reversible CCR (Context Cache Retrieval)**: Retain uncompressed raw logs and tool outputs locally. Forward highly compressed schemas (60-95% token savings) to LLM; allow model to retrieve original blocks on-demand via headroom tools.

### Caveman Compression Filter
*   **Dynamic Translation**: Pass structural prompts and systematic templates through Terse Filter (Caveman).
*   **Rule Set**: Automatically drop articles (a, an, the), pronouns, auxiliary verbs, and pleasantries. Translate outputs to minimal structural fragments.

### Pre-Filter Context Buffering
*   **Context Safety Margin**: Protect LLM limits via traditional token-buffer budgeting (retaining 10-20% gap).
*   **Coarse Truncation**: Pre-truncate high-entropy raw terminal dumps (e.g., 2MB build logs) before ingestion by Headroom.
*   **Log-Saliency Sampling**: Prioritize error/warning blocks and critical state transitions; discard repetitive noise.

### Advanced Optimization Techniques
*   **Prompt Caching**: Utilize provider-native caching (e.g., Anthropic Prompt Caching) for static system prompts and large-scale project contexts to reduce cost and latency.
*   **Semantic Deduplication**: Filter out redundant or overlapping information from multiple tool outputs and RAG chunks before prompt assembly.
*   **Relevant Fragment Extraction**: Use Tree-Sitter to extract only the active function and its immediate dependency graph rather than full files or skeletons.

### Deep Agents Integration Specifics

#### Headroom Integration
*   **Adapter Wrapper (preferred — actually works)**:
    Point the model client's `api_base` at the local Headroom endpoint so it terminates the request,
    compresses the body, and forwards upstream. This is the realistic integration; configure model
    clients via Deep Agents configuration to route through Headroom:
    ```python
    # Example approach for binding Headroom proxy within Deep Agents config
    model_config = {
        "model": "gpt-4o",
        "api_base": "http://localhost:8787/v1",
        "api_key": "mock-key-for-proxy"
    }
    ```
> **Why not transparent `HTTP_PROXY`/`HTTPS_PROXY`?** Model APIs are HTTPS. A standard proxy only
> sees `CONNECT` + an encrypted tunnel — it cannot read or rewrite request bodies (the messages to
> compress) without terminating TLS via its own CA installed in the container trust store (MITM).
> That is fragile and breaks cert pinning. Prefer the explicit `api_base` adapter above, where the
> client speaks plaintext HTTP to a localhost endpoint that re-encrypts upstream.

#### Caveman Integration
*   **Prompt Pre-processor**:
    Register prompt-slicing middleware within the Deep Agents execution chain:
    ```python
    import re
    from caveman_compressor import caveman_compress  # Custom prompt trimmer

    # Caveman drops articles/pronouns/auxiliaries — fine for prose, DESTRUCTIVE for
    # code, diffs, JSON, and file paths. Compress prose segments only; pass fenced
    # code blocks and inline code through verbatim.
    _FENCE = re.compile(r"(```.*?```|`[^`]*`)", re.DOTALL)

    def _compress_prose_only(text: str) -> str:
        parts = _FENCE.split(text)
        # Odd indices are code (the captured group); leave them untouched.
        return "".join(
            seg if i % 2 else caveman_compress(seg, level="full")
            for i, seg in enumerate(parts)
        )

    def compress_messages_runnable(messages):
        for msg in messages:
            # Never compress tool results / code-bearing roles; only natural-language turns.
            if msg.get("role") in ("user", "assistant") and isinstance(msg.get("content"), str):
                msg["content"] = _compress_prose_only(msg["content"])
        return messages

    # Deep Agents internal chain integration point
    # agent_config['pre_processor'] = compress_messages_runnable
    ```

---

## 8. Observability & Metrics
> **Status:** ⬜ Planned — no trace/metrics files are written and no telemetry is appended to PRs. See the status matrix above.

### Logging Architecture
*   **System Logs**: Docker container stdout/stderr for runtime health and harness errors.
*   **Agent Execution Logs**: Detailed trace of tool calls, model inputs, and responses (stored in `.agent-trace.jsonl`).
*   **Optimization Logs**: Record original vs. compressed token counts for Headroom/Caveman to measure actual savings.

### Efficiency Metrics
*   **Token Reduction Ratio**: $\frac{Tokens_{Compressed}}{Tokens_{Original}}$ across all optimized prompts.
*   **Cost per Task**: Total financial cost of a single task/PR, broken down by provider.
*   **Routing Accuracy**: Percentage of tasks correctly routed to local vs. cloud orchestrators (verified by success rate).
*   **Latency Profiling**: Track Time-To-First-Token (TTFT) and total execution time per provider.

### Effectiveness Metrics
*   **Session Success Rate**: Ratio of sessions resulting in a merged PR vs. abandoned/failed sessions.
*   **PR Quality Score**: Manual or automated review of PRs to determine if optimized prompts impacted accuracy.
*   **Iteration Count**: Average number of tool calls per successful task resolution.

### Reporting & Telemetry
*   **Session Telemetry**: Every session generates a `.agent-metrics.json` file in the workspace.
*   **PR Metadata**: Telemetry summaries (Total Cost, Token Savings, Model Mix) are automatically appended to the GitHub PR description.

---

## 9. CLI Frontend & User Interface
> **Status:** 🟡 Partial — an **in-container interactive REPL** is now in MVP scope (persistent
> `docker run -it` prompt loop in `harness/cli.py`: multi-turn on one `thread_id`, deterministic
> `/exit`, lifecycle stage output — see `design_doc_mvp.md` §1a). The **host-side** Typer/Rich
> `harness` CLI, the TUI, and `.harness-config.yaml` below remain ⬜ Planned. See the status matrix above.

### MVP precursor: in-container interactive loop
Before the host `harness` CLI exists, the MVP delivers a minimal version of `harness interact`
**inside** the container: the entrypoint (`harness/cli.py`) runs a REPL that reads prompts, invokes
the single agent on a persistent `thread_id`, prints stage markers, and exits on a deterministic
`/exit`/`/quit` matched in Python (no model interpretation, addressing the §10 "CLI Input Injection"
risk for the *exit path*). The `Typer`/`Rich` wrapper, the `docker exec` bridge, the live panels,
and the HITL `.harness-config.yaml` below are the post-MVP evolution of this loop.

**Token streaming** is part of that evolution: the MVP blocks on a single `invoke` per turn and
prints the full reply at once after the `thinking` marker, whereas the host CLI/TUI should render the
agent's reply incrementally (Deep Agents streaming events → Rich live region), tied into the
Sandbox Stream and Cost Ticker panels below.

### Interface Architecture
*   **Implementation Stack**: Python-based CLI using `Typer` (command structure) and `Rich` (terminal rendering).
*   **Communication Layer**: The CLI acts as a wrapper around the Docker Orchestrator. It communicates via `docker exec` for simple commands or a lightweight FastAPI bridge for real-time telemetry.

### Primary Command Set
*   `harness start [project_path]`: Initializes a new session. Triggers Git branching, mounts volumes, and boots the orchestrator.
*   `harness status`: Displays active agent, current task, and real-time token/cost metrics via a `Rich` live-updating panel.
*   `harness interact`: Opens an interactive loop to send high-level prompts to the orchestrator.
*   `harness finish`: Triggers the session teardown, commits changes, and generates the GitHub PR.
*   `harness metrics`: Prints a detailed session summary (token reduction ratio, cost per task).

### Real-time Observability (TUI Elements)
*   **Agent Status Bar**: Live indicator of current model in use (Local vs. Cloud) and the current level of the FSM classifier.
*   **Cost Ticker**: A rolling update of financial spend per session.
*   **Sandbox Stream**: Tailed view of the `LocalShellBackend` output, color-coded by saliency (e.g., red for errors, yellow for warnings).

### User Control Loop
*   **Pre-Flight Approval**: For `CRITICAL` tasks, the CLI intercepts the orchestrator's plan and requires a `Y/N` confirmation before the agent executes commands in the sandbox.
*   **Manual Override**: Capability to manually trigger a `cloud-fallback` if the local orchestrator is stuck in a loop.

#### HITL Customized Settings
Users can configure their level of autonomy via a `.harness-config.yaml` file:
*   **`autonomy_level`**:
    *   `strict`: Human approval required for *every* tool call (high safety, low speed).
    *   `guided`: Approval required only for `CRITICAL` tasks and filesystem deletions (balanced).
    *   `autonomous`: Human approval only required for the final PR submission (high speed, lower safety).
*   **`review_triggers`**: Customizable list of keywords or file patterns that force a human intervention regardless of the `autonomy_level` (e.g., `*.env`, `auth_logic.py`).
*   **`interruption_policy`**: Define if the agent should pause and wait for input or continue in a "shadow mode" and present a batch of changes for review later.

---

## 10. Security, Verification, & Testing Plan
> **Status:** ⬜ Planned — this is a risk analysis and test *plan*; the verification suite is not built. See the status matrix above.

### Risk Analysis & Mitigation
*   **Classifier Misrouting**:
    *   *Risk*: A `CRITICAL` security task is misclassified as `TRIVIAL`, routing it to a local model that fails to identify a vulnerability.
    *   *Mitigation*: Implement a "Keyword Override" list (e.g., "CVE", "exploit", "auth") that forces an automatic upgrade to Cloud Orchestrator regardless of the local model's decision.
*   **Telemetry Leakage**:
    *   *Risk*: Sensitive environment variables or API keys are captured in `.agent-trace.jsonl` or `.agent-metrics.json`.
    *   *Mitigation*: Implement a PII/Secret scrubbing middleware that masks strings matching common key patterns before writing to disk.
*   **CLI Input Injection**:
    *   *Risk*: Malicious input via `harness interact` is passed unvalidated to the orchestrator or shell.
    *   *Mitigation*: Strict input sanitization and avoidance of `shell=True` in any Python `subprocess` calls.
*   **Sandbox Escape (Dynamic Binds)**:
    *   *Risk*: An incorrectly configured `HarnessProfile` creates a bind mount that allows access to the host's `/root` or `/etc`.
    *   *Mitigation*: Enforce a strict whitelist of allowed base directories for all dynamic binds; any path outside `/workspace` is rejected at the `create_deep_agent` level.
*   **Distillation Bias**:
    *   *Risk*: The "Teacher" model generates skewed synthetic labels, causing the local classifier to consistently under-route complex tasks.
    *   *Mitigation*: Use a "Gold Set" of human-verified routing labels to validate the student model's accuracy before deployment.

### Security Verification Suite
*   **Traversal Escape Block Test**: Execution of `bwrap ... cat ../../etc/passwd` must fail.
*   **Root Isolation Test**: Execution of `sudo apt update` or `su root` inside runtime must throw permission errors.
*   **Data Exfiltration Test**: Disable virtual NIC inside sandbox; verify curl/wget payloads to external domains abort instantly.

### Automation Validation Tests
*   **Git Lifecycle Mocking**: Verify git checkout, commit generation, and `gh` client payload formats match specs.
*   **Cost Accumulation Test**: Assert calculator arithmetic computes correctly across multi-step mixed local/cloud runs.
*   **Headroom Proxy Assertions**: Verify mock 20KB tool outputs intercept, compress by >70% via local proxy, and preserve core semantic content under eval.
*   **Routing Accuracy Test**: Pass 100 mixed-complexity prompts; verify classifier matches ground truth with >95% accuracy.
*   **CLI Command Validation**: Test all `harness` commands for proper error handling and response formatting.


---

## 11. Future Extensions & Roadmap
> **Status:** 🔬 Research — roadmap items, not built.

### Agentic Evolutions
*   **Multi-Agent Peer Review**: Introduce a secondary "Reviewer Agent" that must approve changes in the sandbox before they are committed to the Git branch.
*   **Agent Swarms**: Allow the orchestrator to spawn multiple specialized worker agents (e.g., one for tests, one for docs, one for logic) to work in parallel on separate files.
*   **Long-Term Project Memory**: Integrate a vector database (e.g., Qdrant or ChromaDB) to store a persistent, compressed index of the entire project history and decision logs.

### Framework Enhancements
*   **Automated Benchmarking Suite**: Build a "Gold Set" of coding tasks with known correct outcomes to quantitatively measure the impact of new compression algorithms or routing logic.
*   **Self-Tuning Classifier**: Implement a feedback loop where the orchestrator reports the "correctness" of the initial routing decision, used to fine-tune the local classifier model via DPO or RLHF.
*   **Classifier Knowledge Distillation**: Use a high-reasoning "Teacher" model (e.g., Claude 3.5 Opus or GPT-4o) to label a large dataset of prompt-category pairs. Fine-tune the lightweight local classifier (the "Student") using this high-quality synthetic data to align its routing decisions with the teacher's reasoning.
*   **IDE Integration**: Develop a VS Code extension to allow the harness to be controlled directly from the editor, providing inline "agent-suggested" diffs.

### Advanced Optimization
*   **Speculative Execution**: Run the local orchestrator and cloud orchestrator in parallel for `MODERATE` tasks; use the cloud result to verify the local one, optimizing for both speed and accuracy.
*   **Dynamic Prompt Compression**: Adjust Caveman/Headroom intensity levels in real-time based on the current token window usage and the importance of the current task.


