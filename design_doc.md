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
| 3 | Workflow engine — folder format + deterministic gates + side-effect steps | ✅ Built | `harness/workflows.py`: `workflows/<name>/` folders (`workflow.md` + `trigger.py`/`trigger.sh` gate + ordered steps), `WorkflowMiddleware`. `hooks.json` is the flat always-gate precursor, adapted into the same path |
| 3 | Classifier-gated triggers + context-mutation / control-flow action tiers | ⬜ Planned | Deterministic predicate gates + the side-effect action tier are built (above); the classifier gate and the context-rewrite / control-flow tiers are not |
| — | MCP tool loading (`.mcp.json`) | ✅ Built | `load_mcp_tools` (not a separate doc section) |
| 3 | Git branch/commit/push/PR lifecycle | ✅ Built | `workflows/git-branch` (session.start) + `workflows/git-pr` (session.end): branch → persist session id → commit/push → `gh pr create`, never auto-merged. Safe no-op without a repo/remote/`GH_TOKEN` |
| 5 | Multi-agent funnel (classifier→orchestrator→worker) | ⬜ Planned | Single `create_deep_agent` today |
| 6 | Token/cost tracker | ✅ Built (Milestone 1) | `harness/cost.py` (`CostTrackerMiddleware`); pricing in the `providers/` TOML registry (`[pricing]` per model, strategy per provider), not a `prices.json`. Optional energy estimate + budgets. See `design_doc_milestone1.md` |
| 7 | Headroom / Caveman / caching pipeline | ⬜ Planned | Nothing integrated |
| 8 | Observability, telemetry, telemetry-to-PR | ⬜ Planned | No trace/metrics files written |
| 9 | In-container interactive REPL (multi-turn session) | 🟡 MVP | Persistent `docker run -it` prompt loop in `harness/cli.py`: multi-turn on one `thread_id`, deterministic `/exit`, stage output — see `design_doc_mvp.md` §1a |
| 9 | Host CLI frontend (Typer/Rich) + TUI | ⬜ Planned | No `harness` CLI/TUI; interactive use is the in-container REPL above |
| 9 | HITL autonomy config (`.harness-config.yaml`) | ⬜ Planned | — |
| 10 | Security verification test suite | ⬜ Planned | Risk analysis is design-only |
| 11 | Future extensions & roadmap | 🔬 Research | By definition |
| 12 | CI pipeline for the harness repo | ⬜ Planned | Suite exists (`pytest`/`verify`/`smoke`) but nothing runs it on push/PR; no `.github/workflows/` |
| 12 | Config-validate / `harness doctor` | ⬜ Planned | `verify` checks imports only; no pre-flight check that the registry / `.mcp.json` / `hooks.json` / `workflow.md` are coherent |
| 12 | Headless one-shot-to-PR mode | ⬜ Planned | Non-TTY today only degrades to a single REPL turn; no structured-result batch entrypoint |
| 12 | Provider resilience (retry/backoff + context-overflow fallback) | ⬜ Planned | No handling of 429/5xx/network blips mid-turn; no interim plan before §7 compression lands |
| 12 | Thread / checkpoint management | ⬜ Planned | `checkpoints.sqlite` grows unbounded; no list/show/rm/prune of threads |
| 12 | Deepagents-native skills & memories wiring | ⬜ Planned | `project/agents/`,`skills/`,`memories/` are baked into the image but empty + unread by `build_agent` (dead scaffolding) |
| 12 | Cost / telemetry persistence | ⬜ Planned | Cost prints to stderr then vanishes; nothing on disk to feed §8 telemetry-to-PR or spend-over-time |
| 13 | File-read middleware (per-file context shaping) | ⬜ Planned | No read-time transform seam; `read_file` serves whole files. Planned: pipeline on the backend `read()` override; tag add/omit + in-file progressive disclosure as instances |

---

## 2. Sandboxing Strategy & Container Layout
> **Status:** 🟡 Partial — single container + conda isolation + secret provisioning + persistent workspace built; dual-container, bubblewrap jail (built but not wired in), `HarnessProfile` binds + per-agent network policy, path guard, and resource limits **planned**. See the status matrix above.

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

### Per-Agent Network Policy

> **Status:** 🔵 Planned. Extends the container-wide NetJail (`deepagent-image/netjail/`, opt-in
> `NET_JAIL=1` / `-NetJail`) with a *subtractive* per-agent layer.

The container-wide NetJail (egress allowlist + host-service forwarders) is the **outer bound**:
if *any* agent in the run needs a domain or host service, it is declared once in
`allowed-domains.txt` / `host-services.txt` and the whole container can reach it. That is correct
for the shared bound, but it means a lightweight or untrusted subagent inherits the full reach of
the most-privileged agent. The per-agent policy narrows that reach back down for individual agents.

`HarnessProfile` gains a `network` field, applied per agent when its tools are constructed:

```python
@dataclass
class NetworkPolicy:
    egress: bool = True                 # False → withhold proxy env → no external domains
    host_services: set[str] | None = None  # forwarder names this agent may use; None = all, set() = none
    allow_net_tools: bool = True        # gate http/fetch/MCP-over-network tools for this agent

# on HarnessProfile:
network: NetworkPolicy = NetworkPolicy()
```

**Enforcement is Tier-1 (app-layer, env-based).** The policy shapes the shell-tool env in
`_agent_shell_env()` (`harness/agent.py`) and the tool list in `build_agent()`, per agent:

*   `egress=False` → withhold `HTTP_PROXY` / `HTTPS_PROXY` / `http_proxy` / `https_proxy` from that
    agent's shell env. Under NetJail the egress proxy is the **only** path out (the container is on an
    `--internal` network with direct internet denied), so an agent with no proxy env cannot reach any
    external domain — even ones the container allows.
*   `host_services` → withhold the `deepagent-fwd-<name>` hostnames (and the auto-set `OLLAMA_HOST`)
    for forwarders not in the set, so the agent cannot dial host daemons it wasn't granted.
*   `allow_net_tools=False` → omit http/fetch and network-transport MCP tools from that agent's
    toolset entirely.

> **This is a policy, not a cage — same trust caveat as the MVP shell (`design_doc_mvp.md` §5).**
> All subagents run in-process, sharing one network namespace and one proxy. Env-withholding stops a
> *cooperative* agent and a prompt-injected one that only knows the documented env, but an agent that
> hardcodes the proxy IP or a forwarder address can still reach it. It is meaningful **only while
> `NET_JAIL=1`**: without the jail the container has open egress and withholding proxy env changes
> nothing (direct internet still works). For a kernel-enforced per-agent boundary — or for true
> per-domain subsets, which one shared tinyproxy cannot attribute to an in-process caller — the agent
> must move to its own container/netns (the Dual-Container / Executor-Sandbox direction above). That
> is the strong-isolation successor, out of scope for the in-process Tier-1 mechanism specified here.

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
> **Status:** 🟡 Partial — the **deterministic slice** is built (`harness/workflows.py`): the
> `workflows/<name>/` folder format, predicate gates (`trigger.py` in-process / `trigger.sh`
> subprocess), the side-effect action tier, and the git branch/commit/push/PR lifecycle below (the
> canonical workflow, shipped as the paired `git-branch` + `git-pr` folders). `hooks.json` survives as
> the flat always-gate precursor, adapted into the same engine. Still **planned**: the classifier gate
> and the context-mutation / control-flow action tiers. See the status matrix above.

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
        model. Requires hooks to *return* a value the middleware applies, not just fire. (Distinct
        from the **read-boundary** transform seam of §13, which shapes one tool's output — `read_file`
        — rather than the whole outgoing context.)
    *   **Control-flow** *(planned)* — alter dispatch: select/swap the model (model routing),
        short-circuit a turn, or veto a tool call.

> **Built vs. planned.** Built: the folder format, the **deterministic** gate layer (predicate —
> `trigger.py`/`trigger.sh`), and the **side-effect** action tier, on all seven events. Planned: the
> **classifier** gate kind, and the **context-mutation / control-flow** action tiers — the work that
> turns "run a command on an event" into a gate that *judges intent* and an action that *rewrites
> context or alters dispatch*.

### Workflow format (on disk)
Each workflow is a **self-contained folder named after the workflow**, discovered under a
`workflows/` root (sibling to skills/agents; `project/workflows/<name>/` in the harness, overridable
via `DEEPAGENTS_WORKFLOWS_DIR`). The folder is the unit you copy, version, and share — same shape
every time:

```
workflows/
  git-session-lifecycle/        # folder name == workflow name
    workflow.md                 # manifest: description + ordered plan (required)
    trigger.py                  # gate, fixed basename, lives in the folder (required)
    create-branch.sh            # a step script (optional, local)
    open-pr.sh                  # a step script (optional, local)
```

*   **`workflow.md` — the manifest (required).** A short prose description, plus frontmatter that
    *is* the trigger × hook × action triple made concrete:
    ```markdown
    ---
    name: git-session-lifecycle     # must equal the folder name
    hook: session.start             # one of the 7 hook points above
    gate: trigger.py                # fixed basename; always ./trigger.{py,sh} in this folder
    steps:                          # run in listed order, only if the gate passes
      - ./create-branch.sh          # relative path → resolved against the workflow folder
      - ./open-pr.sh
      - /opt/harness/git/notify.sh  # absolute path → run as-is
    ---
    Branch at session start, open a PR at session end. Never auto-merges.
    ```
*   **`trigger` — the gate (required, fixed basename).** **Every** workflow folder has a gate file
    with the *same fixed basename* `trigger`, always resolved inside the folder — never elsewhere,
    never renamed. The extension picks the contract:
    *   **`trigger.py` (canonical, in-process).** The harness is Python (Deep Agents), so the gate
        runs **inside the harness process** — it can read the live prompt/context/state and call the
        classifier **directly** (same interpreter, no subprocess marshaling), then return a verdict
        (`bool`, or a richer decision once the control-flow / context-mutation tiers land). This is
        the gate kind the planned action tiers need; it runs in the harness venv (`/opt/venv`), so it
        is **engine code — stdlib + the engine API only, never workspace deps** (the two-stack rule).
    *   **`trigger.sh` (allowed, subprocess).** A side-effect-free predicate over what a subprocess
        can see — `git status`, a file check, an env var — signalling via **exit code: `0` = run the
        steps, non-zero = skip**. Isolated and language-agnostic, but it cannot see live in-memory
        state; reach for it only for pure file/git/env gates.
    The engine resolves `trigger.py` first, then `trigger.sh`; exactly one must exist.
*   **Steps — the action (ordered).** Listed in `steps:` and run top-to-bottom only after the gate
    passes. Each entry is a path: **relative paths resolve against the workflow folder** (the common
    case — keep step scripts beside `workflow.md`), **absolute paths run as-is** (share one script
    across workflows, or call into the harness). Steps may therefore live in the folder *or* anywhere
    on disk; the `trigger` gate may not.

This is the authoring format (built — `harness/workflows.py:load_workflows`); `hooks.json` is the
flat precursor (one event → one unconditional command, no folder, no gate), adapted into a synthetic
always-gate workflow so both run on one path. The git lifecycle below is the first workflow expressed
this way.

### Canonical workflow: Git session lifecycle
A deterministic workflow whose body is git side-effects — the worked example the engine generalizes.
Because a workflow binds to **one** hook point, it ships as a **paired set of two folders** that share
state via `<workspace>/.deepagents/session.env` (written at start, reused at end):
`git-branch` on `session.start` and `git-pr` on `session.end`. Each is a safe no-op without its
prerequisites (a git repo / a clean tree / a remote / `gh` + `GH_TOKEN`).

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

### Canonical workflow: the Funnel (control-flow + routing)
The second worked example — and the one that exercises the **classifier-gate** and **control-flow**
tiers the git lifecycle does not. A **funnel** is a workflow bound to a **per-user-input** hook
(`agent.start`, or `model.start` to act before context is sent) whose gate decides — by any
criterion — and whose action **selects which model/agent the (optionally transformed) input is
dispatched to**. It is to routing what the git lifecycle is to side-effects: the canonical instance
the engine generalizes. It needs no new hook point — `agent.start` already fires once per user
input.

*   **Gate = the routing criterion (any kind).** A deterministic predicate (`trigger.py`
    rule/keyword match), a dedicated classifier-only model, or a full LLM — all the same gate slot,
    returning a richer verdict than `bool` once the control-flow tier lands. The FSM/Qwen traffic
    classifier of §4 is **one such criterion, not a prerequisite**: a funnel ships with a trivial
    deterministic gate and upgrades to a classifier/LLM criterion without changing shape.
*   **Action = control-flow (route to 1-of-N targets).** The step selects/swaps the model or
    sub-agent for this turn (the §3 control-flow tier) instead of running a side-effect; the
    `model="…#tag"` selection of §4 is the mechanism.
*   **Optional transform = context-mutation.** Before dispatch the same workflow may rewrite the
    outgoing context (the §3 context-mutation tier) — compress it (§7) or inject retrieved files.
    "Potentially transformed text routed to the next area" is exactly *context-mutation action →
    control-flow action* composed in one per-input workflow.

So **classifier routing (§4), the multi-agent funnel (§5), and compression (§7) are one mechanism**:
a per-input workflow over the planned gate / context-mutation / control-flow tiers. Specific
criteria (classifier, LLM) and specific transforms (Headroom, Caveman) are pluggable instances — the
way `git-branch` / `git-pr` are instances of the side-effect tier.

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

> **The funnel is a workflow, not a separate engine.** The classifier→orchestrator→worker funnel is
> the §3 **Funnel** canonical workflow: a workflow on a per-input hook (`agent.start` /
> `model.start`) whose **gate** is the routing criterion (deterministic fn, classifier-only model,
> or LLM) and whose **control-flow action** dispatches the (optionally context-mutated, §7) input to
> one of N models/agents. The structural options below are the *targets* a funnel routes between; the
> §4 classifier is **one gate criterion, not a prerequisite** — a funnel ships with a deterministic
> gate first, then upgrades the criterion in place. This is the same generalization as
> git-lifecycle → workflow engine: routing is a *specific usage* of the funnel, the funnel a
> *specific usage* of the workflow engine.

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

> **As a workflow tier.** Compression is the §3 **context-mutation** action tier: a per-input
> workflow step that rewrites the outgoing context before it reaches the model — the same seam a
> funnel's optional transform uses (§3 / §5). Split the two: the **capability** (a context-transform
> middleware seam on the engine, applied per input) is the completeness-tier feature; Headroom and
> Caveman below are **pluggable instances** of that tier and are individually optional/swappable.
> Building the seam is what makes "compression" a product capability; picking a specific filter is a
> later, replaceable choice. (§13 is the sibling seam at the **read boundary** — same seam-vs-instances
> split, applied to `read_file` output instead of the whole outgoing context; its progressive
> disclosure is CCR applied per file.)

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


---

## 12. Operational Hardening & Automation Roadmap
> **Status:** ⬜ Planned — these are the gaps surfaced by an audit of the built MVP + Milestone 1
> against the full vision: the loop works and is config-driven, but it is not yet *operated* like a
> product (no CI, no pre-flight validation, no headless/automation entrypoint, no resilience, no
> lifecycle management of the state it accumulates). Each item below is independently shippable and
> ordered roughly by leverage. Several are bridges to already-planned sections — cross-refs noted.

### 12.1 CI pipeline for the harness repo
*Why.* The harness already ships a real test suite — `project/tests/` (pytest, host-runnable +
image-only tiers), `scripts/verify.{ps1,sh}`, `scripts/smoke.{ps1,sh}` — but nothing runs it
automatically. Every change to provider routing, the cost math, or the workflow engine can regress
silently until a human remembers to build + run locally. There is no `.github/workflows/`.

*Design.* A GitHub Actions workflow on push + PR:
1. **Host-tier tests** (no Docker): `python3 -m pytest tests/` for the stdlib-only modules
   (`test_cost`, `test_sync_models`, `test_providers`, `test_loaders`, `test_import_isolation`) — the
   `importorskip` guards already make the image-only modules skip cleanly off-image.
2. **Image build + image-tier tests**: `docker build --target test` then run the full suite inside
   (exercises `test_agent`/`test_cli`/`test_workflows` against the real runtime layer).
3. **Smoke**: build `--target runtime`, run a keyless smoke turn (exits 0 by design — the git
   workflows are safe no-ops without a remote/`gh`).
4. **Script-parity lint**: assert every `scripts/*.ps1` has a matching `*.sh` (and flag drift) — the
   "keep the pair in sync" rule is currently honour-system.

*Touch points.* New `.github/workflows/ci.yml`; no source change. Optionally a tiny
`scripts/check-parity.{sh,ps1}` for step 4.

*Done when.* PRs show pass/fail status; a deliberately broken cost calc fails CI; the parity check
fails if a `.ps1`/`.sh` pair drifts.

### 12.2 Config validation — `harness doctor`
*Why.* `verify` proves the venv imports; it does **not** prove the on-disk config is coherent. A
`provider.toml` whose `default_model` names a missing model TOML, a `rate_table` provider missing a
`[pricing]` table, a malformed `.mcp.json`, or a bad `workflow.md` manifest are only discovered at
run time (the workflow loader already `SystemExit`s loudly — but after a container start). A
pre-flight validator catches all of it in one cheap, keyless command.

*Design.* `python3 -m harness doctor` (subcommand alongside `sync-models`), wired through
`cli.dispatch`. Checks, all non-fatal-reporting (collect + summarize, exit non-zero if any error):
- **Registry**: every `provider.toml` parses; each non-null `default_model` resolves to a real
  `models/<model>.toml`; `rate_table` providers have `[pricing]` (or `[pricing.estimate]`) with
  `priced_as_of`; flag stale `priced_as_of`.
- **Credentials**: report which providers have their key / `*_BASE_URL` set (no values printed —
  secret-hygiene rule).
- **Optional config**: `.mcp.json`, `hooks.json` parse; every `workflows/<name>/workflow.md` passes
  the same manifest parser used at load (name match, known hook, exactly one gate file).
- Reuse the existing loaders (`providers._load_providers`, `loaders.*`, the workflow manifest parser)
  so validation can't drift from runtime behaviour.

*Touch points.* `harness/cli.py` (dispatch + arg), a new `harness/doctor.py` (pure, stdlib-only —
keeps the host-runnable tier); `scripts/verify.{ps1,sh}` optionally call it; `test_doctor.py`.

*Done when.* `doctor` flags a registry with a dangling `default_model` and a `rate_table` model
missing pricing; passes clean on the shipped registry; runs with no keys/network.

### 12.3 Headless one-shot-to-PR mode
*Why.* Today a non-TTY stdin only *degrades* the interactive REPL to one turn (a CI fallback, not a
feature). With the git lifecycle (§3) and cost tracker (§6/M1) now built, the missing capstone is a
first-class **headless** mode that lets *other* automation drive the harness: run a task to
completion, do the branch→commit→PR lifecycle, and emit a machine-readable result. This is the
entrypoint the scheduled/remote-agent use case needs.

*Design.* A `--headless` (a.k.a. batch) path in `cli.main`, distinct from the degraded single turn:
- Input: a single task (arg/`DEEPAGENTS_TASK`) or a task file (one task per line / JSON array).
- Runs the turn(s), lets the `session.end` git-pr workflow run, then emits a **structured result on
  stdout** (JSON): final message, token/cost totals (from the §6 accumulator), thread id, branch,
  PR URL (if created), and a clear exit code (0 ok / non-zero on agent error / budget exceeded).
- Stage markers + usage stay on stderr (unchanged), so stdout is clean JSON for piping.
- Honours the existing budget ceilings (`--max-cost`/`--max-tokens`) as hard stops.

*Touch points.* `harness/cli.py` (`--headless`, a `run_batch` beside `run_repl`, JSON result
serializer); `scripts/run-docker.{ps1,sh}` gain a headless pass-through (drop `-t`, keep `-i`);
`test_cli.py` (result schema, exit codes). Builds on §3 (git-pr) and §6 (cost totals).

*Done when.* `run-docker --headless "task"` prints one JSON object + exits with a meaningful code;
piping a task file runs each and aggregates; a budget-exceeded run exits non-zero with the partial
total in the JSON.

### 12.4 Provider resilience — retry/backoff + context-overflow fallback
*Why.* A long persistent session is one transient `429`/`5xx`/network blip away from dying
mid-turn, and one long conversation away from a hard context-window error. The §7 compression
pipeline is the eventual answer to context pressure but is research-stage; there is no interim
guard, and no retry on transient provider failures at all.

*Design.* Two narrow, optional behaviours, off by default-equivalent (safe):
- **Transient-error retry**: bounded exponential backoff (e.g. 3 tries, jitter) around the per-turn
  model invoke for retryable statuses (429 / 5xx / connection reset). Caps from env
  (`DEEPAGENTS_MAX_RETRIES`, `DEEPAGENTS_RETRY_BASE`). A turn that still fails returns to the prompt
  with a `[harness] provider error` marker instead of crashing the REPL.
- **Context-overflow stopgap**: catch the provider's context-length error and apply a *minimal*
  interim policy — trim/summarize the oldest turns and retry once — explicitly flagged as the
  pre-§7 placeholder so it is replaced, not entrenched, when Headroom lands.

*Touch points.* `harness/cli.py` `run_turn` (wrap the invoke; reuse the existing
`try/except KeyboardInterrupt`/`BudgetExceeded` structure — add sibling clauses); a small
`harness/resilience.py` for the backoff/classification helpers (pure, unit-testable);
`test_resilience.py`. Interim context policy cross-refs §7 (to be superseded).

*Done when.* A stubbed `429`-then-success turn completes after backoff; a stubbed permanent error
returns to the prompt without killing the session; a simulated context-overflow trims + retries
once and is labelled interim.

### 12.5 Thread / checkpoint management
*Why.* Conversation state persists in `<workspace>/.deepagents/checkpoints.sqlite`, keyed by
`DEEPAGENTS_THREAD_ID`. It grows unbounded and there is no way to see what threads exist, inspect
one, reset one, or prune old ones — resuming requires *knowing* the id. This completes the memory
feature already shipped.

*Design.* A `harness threads` subcommand group (keyless, operates on the local sqlite):
- `list` — thread ids + turn count + last-modified.
- `show <id>` — summary of a thread (first/last turn, counts).
- `rm <id>` / `prune --older-than <N>d` — delete a thread / bulk-prune (with a confirm or `--yes`,
  per the irreversible-action rule).

*Touch points.* New `harness/threads.py` (queries the SqliteSaver schema directly), `cli.dispatch`
wiring, `test_threads.py` against a tmp sqlite. No change to the run path.

*Done when.* `threads list` shows seeded threads; `rm`/`prune` delete only the targeted rows;
guarded against deleting everything without explicit confirmation.

### 12.6 Deepagents-native skills & memories — wire or document
*Why.* The Dockerfile bakes `project/agents/`, `project/skills/`, `project/memories/` into the
image, but they are **empty scaffolding and unread** — `build_agent` only loads `AGENTS.md`. This is
latent intent not captured anywhere in the docs, and distinct from the §5 multi-agent funnel and the
§11 vector-DB "Long-Term Project Memory": it is deepagents' *own* skills / subagent / memory
mechanism. Leaving dead dirs in the image is a maintenance trap.

*Design.* Decide explicitly, document either way:
- **Option A (wire it):** load `skills/` and `memories/` into `create_deep_agent` per the deepagents
  API, and treat `agents/{coding-agents,orchestrators,pre-orchestrators,lightweight-tool-callers}`
  as the on-disk staging ground for the §5 funnel roles. Smallest first step: load skills into the
  single MVP agent (no funnel yet).
- **Option B (defer cleanly):** keep the dirs only if they are referenced; otherwise remove the
  `COPY` lines and the empty trees until §5 lands, and note the intended layout here so it is not
  lost.

*Touch points.* `harness/agent.py` (`build_agent` — load skills/memories if Option A); `Dockerfile`
(COPY lines); `deepagent-image/CLAUDE.md` (describe the dirs). Cross-refs §5 + §11.

*Done when.* The dirs are either functionally loaded (with a test asserting a seeded skill reaches
the agent) or removed, and `deepagent-image/CLAUDE.md` states which and why — no silent dead
scaffolding.

### 12.7 Cost / telemetry persistence
*Why.* The cost tracker (§6 / M1) prints per-turn + session usage to stderr, then it is gone. There
is no on-disk record, so there is nothing to feed §8's telemetry-to-PR, no spend-over-time, and no
post-hoc reconciliation. This bridges the built cost tracker to the planned observability layer.

*Design.* An optional sink on the existing `CostTrackerMiddleware` / session-end path: append a
per-session record to `<workspace>/.agent_telemetry/usage.jsonl` (thread id, model, per-turn +
total tokens by kind, cost + provenance `official|estimate|reported`, energy if present,
timestamps). Off unless the tracker is active (preserves the "removable = byte-for-byte MVP"
contract). Honour the secret-scrubbing requirement from §10 before any disk write. `.agent_telemetry/`
is already git-ignored (workspace `.gitignore`) and already excluded by the git-pr workflow, so it
never lands in an agent commit.

*Touch points.* `harness/cost.py` (a small `UsageSink` writing JSONL; called from the session-end
print), `cli.py` (pass the workspace path / enable flag); `test_cost.py` (record shape, scrubbing,
no-write-when-tracker-absent). Feeds §8; reuses §6 data structures.

*Done when.* An active-tracker session writes one well-formed JSONL line per session; a tracker-off
run writes nothing; secrets never appear in the file.


---

## 13. File-Read Middleware (per-file context shaping)
> **Status:** ⬜ Planned — the read tool serves whole files verbatim (only the model-supplied
> `offset`/`limit` trim them); there is no read-time transform seam. See the status matrix above.

> **Three context seams — do not conflate.** This is a distinct third axis from §3 and §7:
> *   **§7 compression** rewrites the *entire outgoing message list* before a model call
>     (Headroom/Caveman) — global, role-agnostic.
> *   **§3 context-mutation** rewrites the *prompt / shared state* at a workflow **hook point**
>     (`model.start`, `tool.end`) — turn-scoped.
> *   **This section** transforms *one tool's output* — `read_file` — as a file crosses into context,
>     with awareness of **where inside the file** content lives (regions, sections, line ranges). It
>     is the read-boundary analogue of §7's reversible CCR: serve less of a file up front, let the
>     model pull more on demand.

### The seam (the capability)
Deep Agents routes every `read_file` call through a backend `read(path, offset, limit)` method (today
`deepagents.backends.LocalShellBackend`, which the harness already subclasses as
`_WorkspaceShellBackend` in `harness/agent.py`). The capability is a **registered, ordered
read-transform pipeline** applied to that read result before it returns to the model — the single
**designated insertion point** for read-time shaping, the same way §7 is the seam for outgoing-context
compression.

*   **Shape.** Each transform is a pure `(ReadRequest, ReadResult) -> ReadResult` step; the pipeline
    is an ordered, config-declared list (like §3 `steps:`). An **empty pipeline returns the file
    byte-for-byte** — the removable-seam contract shared with §7 and the M1 cost tracker: off ⇒
    behaviour identical to today.
*   **Where it plugs.** Override the **public** `read()` on `_WorkspaceShellBackend` to run the
    pipeline around `super().read(...)`. Preferred over wrapping the `read_file` *tool* because
    `read()` is the single choke point **every** read_file call funnels through, and it is public —
    stable across a deepagents upgrade, unlike the `_resolve_path` override that needs the
    construction-time guard (`agent.py`). The earlier `min(limit, N)` line clamp is the trivial
    degenerate case of one such transform; this generalizes it to content-aware steps.
*   **Two-stack rule.** Transforms run in the harness venv (`/opt/venv`) → **stdlib + engine API
    only**, never workspace deps (Tree-Sitter etc. would ship as a harness dependency, not a
    workspace one).

### Pluggable instances
The seam is the product capability; specific transforms are swappable instances (mirrors §7's
seam-vs-Headroom/Caveman split):

1.  **Tag-gated add / omit.** In-file content markers curate what the agent sees, per file, with no
    external config: e.g. `<!-- agent:hide -->…<!-- /agent:hide -->` regions stripped from the
    returned content; `<!-- agent:note: … -->` surfaced or hoisted. Repo authors steer attention at
    the source, not in harness config. Markers are comment-syntax so they stay invisible to normal
    tooling.
2.  **Progressive disclosure within a single file.** The first read of a large file returns a
    **map** — headings / outline / a symbol skeleton with line ranges (Tree-Sitter "relevant
    fragment extraction", §7) — not the full body; the model then expands a named region or range on
    demand. This is §7's reversible CCR applied at the read boundary: bounds context on big files
    while keeping the full text one tool call away. Needs per-thread memory of what has already been
    disclosed (reuse the SqliteSaver checkpoint state, §2/§9), so an expand request resolves against
    the same file view.

### Build notes
*   *Touch points.* `harness/agent.py` (`_WorkspaceShellBackend.read` override + pipeline wiring), a
    new `harness/read_pipeline.py` (the pipeline + transform registry, pure/stdlib so it stays in the
    host-runnable test tier), `tests/test_read_pipeline.py` + a case in `test_agent.py`. Config
    surface mirrors §3 (declare active transforms + order).
*   *Done when.* An empty pipeline returns a file byte-for-byte (identical to today); the tag-strip
    transform omits a `hide` region and leaves untagged content unchanged; a progressive-disclosure
    read of a large file returns the outline only, a follow-up expand returns the requested region,
    and the full body is never served unbidden; every transform is pure + unit-tested off-image.


