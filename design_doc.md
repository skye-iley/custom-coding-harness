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
> Much of it is **not built yet** — including most of what makes it a *multi-agent* harness
> (routing, funnel, per-agent isolation, telemetry, compression). The table below is the source of
> truth for what exists today;
> everything else is aspirational. Where a section below is not ✅ here, read it as a spec to build
> against. Status legend: ✅ Built · 🟡 Partial · ⬜ Planned · 🔬 Research.

**Built today:** a single Docker image (`deepagent-harness`: Ubuntu 24.04 + uv venv + Miniforge)
that runs **one** `create_deep_agent` against a bind-mounted workspace. Model selection is the
`PROVIDERS` registry in `project/harness/providers.py`, loaded at import time from the on-disk
`project/providers/` TOML registry (explicit, or auto-selected by which API key is set — **there is
no classifier**). It loads MCP tools (`.mcp.json`), runs lifecycle shell hooks (`hooks.json`) and
`workflows/` folders, persists conversation state to a SqliteSaver checkpoint (keyed by thread id),
isolates workspace dependencies in a workspace-local conda env, and receives secrets via
`--env-file` at run time. That is the §1/§4 provider layer + the built parts of §2/§3.

On top of that MVP baseline, six milestones have shipped — cost/energy tracking + budgets (M1),
present/past memory (M2), human-in-the-loop (M3), the workspace trust boundary: masking, path
guard, bwrap jail, `doctor`, CI, security suite (M4/M4.1), and the unified config surface + its
field registry (M5/M5.1). **Still one agent, one trust boundary, no routing and no telemetry sink**
— which is what the Core-vs-Peripheral section below is about.

> **Keeping this table honest.** It drifts silently: a milestone lands, the row it makes obsolete
> is three screens away, and nothing fails. When a milestone completes, re-read this table before
> closing it out — a ⬜ row for something that shipped is worse than no row, because it is read as
> a decision not to build.

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
| 2 | Conversation checkpoint (SqliteSaver) | ✅ Built | `checkpoints.sqlite` in the harness **state dir**, thread-keyed. Defaults to `<workspace>/.deepagents/`; `DEEPAGENTS_STATE_DIR` relocates it and `run-docker` points it at `/project/state`, outside the workspace mount (M2) |
| 2 | Dual-container (orchestrator + executor) | ⬜ Planned | One container today |
| 2 | Workspace visibility / secret masking | ✅ Built (Milestone 4) | `harness/mask.py`: `.agentignore` (gitignore-parity), 3-tier policy, deny/allow modes, un-negatable designated-secret floor, docker mount-mask (masked paths present-but-empty). `DEEPAGENTS_MASK=0` ⇒ M3 parity. See `docs/milestones/complete/milestone4.md` |
| 2 | Bubblewrap fs-tool jail | ✅ Built, **opt-in** (Milestone 4 slice H) | `DEEPAGENTS_JAIL=1` re-execs the harness into a bwrap mount namespace (`harness/jail.py`), so every fs tool — shell included — inherits an allow-list bind whitelist; `/project` read-only, floor overmounted empty, state dir out of reach. **Off by default:** needs `--security-opt seccomp=seccomp/userns.json` (docker-default + 5 relaxed syscalls) on the outer container, an operator's trade. `harness/nsguard.py` is the shell-seam tripwire for those syscalls. Gate verified on Docker Desktop/WSL2 **and, since M4.1, on a live AppArmor-confined Ubuntu host and in CI** (`smoke` pins `JAIL_CHECK=1`) |
| 2 | AppArmor profile for the jail | ✅ Built (Milestone 4.1) | `apparmor/deepagent-userns` = moby's `docker-default` with only its `deny mount,` narrowed; `apparmor-sync --check`, installer, and `run-docker`/`doctor` wiring all built. **Measured on a live AppArmor host** (2026-08-14, Ubuntu VM, kernel `7.0.0-29-generic`, Docker 29.7.2) — the rule set is measured, not derived, and CI's `smoke` job loads the profile and pins `JAIL_CHECK=1`, so the jail is a red/green gate. SELinux untested and **named as such** at run time (`docs/features/selinux_compatibility.md`) |
| 2 | The kernel's procfs gate (third gate) | ✅ Built (Milestone 4.1 fork J5) | `mount_too_revealing()` refuses bwrap's fresh `--proc` while Docker's `maskedPaths`/`readonlyPaths` cover the container's procfs — EPERM, no LSM denial, independent of seccomp *and* AppArmor. Both launchers and both smoke scripts pass `--security-opt systempaths=unconfined` under `DEEPAGENTS_JAIL=1`; `DEEPAGENTS_JAIL_SYSTEMPATHS=default` keeps the masks as the LSM-only control. `classify_bwrap_failure` has a third `procfs` class (told from `lsm` by errno) |
| 2 | `HarnessProfile` dynamic bind mounts | ⬜ Planned | Fixed bind list; no per-agent profile |
| 2 | Path Guard middleware (`validate_path`) | ✅ Built (Milestone 4 slice C/D) | `harness/pathguard.py` commonpath traversal guard on the fs tools. A denial always prints `path-guard DENIED` to stderr (HITL or not) and, under HITL, appends to `<state-dir>/denials.jsonl` — outside the workspace, so the agent can't truncate the record. **Audit-only:** never offers an interactive approve, because every denial it can currently raise is a true workspace escape |
| 2 | Resource limits (`--cpus`/`--pids-limit`/mem) | ✅ Built | `run-docker.{sh,ps1}` set `--cpus`/`--memory`/`--pids-limit` (defaults 2/4g/512, overridable). Docker host-boundary control, not a sandbox |
| 2 | NetJail — container-wide deny-all egress jail (opt-in) | ✅ Built | `run-docker -NetJail` / `NET_JAIL=1`: agent on an `--internal` net, socat host-service forwarders (`host-services.txt`) + domain-allowlisted tinyproxy egress (`allowed-domains.txt`), **fail-closed** if the proxy config doesn't load. `smoke -NetJail` exercises it. Verified on Docker Desktop; see `netjail/README.md` |
| 2 | Per-agent network policy (`HarnessProfile.network`) | ⬜ Planned | NetJail is all-or-nothing per run; no per-agent *subtractive* egress / host-service / net-tool gating (Tier-1 env-based; §2) |
| 2 | Config-driven allowlist selection (`@group` + `enabled.txt`) | ⬜ Planned | Entries enabled by hand-uncommenting; no group tags / machine-written selection for a startup menu (§2) |
| 3 | Workflow engine — folder format + deterministic gates + side-effect steps | ✅ Built | `harness/workflows.py`: `workflows/<name>/` folders (`workflow.md` + `trigger.py`/`trigger.sh` gate + ordered steps), `WorkflowMiddleware`. `hooks.json` is the flat always-gate precursor, adapted into the same path |
| 3 | Classifier-gated triggers + context-mutation / control-flow / pause (HITL) action tiers | 🟡 Partial | Deterministic predicate gates + the side-effect action tier are built, and the **pause tier shipped in Milestone 3** (as `hitl.PauseMiddleware`, not a `workflows.py` step — steps can't suspend the graph). The classifier gate and the context-rewrite / control-flow tiers are still not built |
| — | MCP tool loading (`.mcp.json`) | ✅ Built | `load_mcp_tools` (not a separate doc section) |
| 3 | Git branch/commit/push/PR lifecycle | ✅ Built | `workflows/git-branch` (session.start) + `workflows/git-pr` (session.end): branch → persist session id → commit/push → `gh pr create`, never auto-merged. Safe no-op without a repo/remote/`GH_TOKEN` |
| 5 | Multi-agent funnel (classifier→orchestrator→worker) | ⬜ Planned | Single `create_deep_agent` today |
| 6 | Token/cost tracker | ✅ Built (Milestone 1) | `harness/cost.py` (`CostTrackerMiddleware`); pricing in the `providers/` TOML registry (`[pricing]` per model, strategy per provider), not a `prices.json`. Optional energy estimate + budgets. See `docs/milestones/complete/milestone1.md` |
| 7 | Headroom / Caveman / caching pipeline | ⬜ Planned | Nothing integrated |
| — | Rate limiting / request pacing | ✅ Built | Two layers: reactive server-honoured backoff (`resilience.retry_after_seconds` reads `Retry-After`/`retry_delay`) and proactive pacing of **every** model call via `harness/ratelimit.py` + langchain's `InMemoryRateLimiter`, from `provider.toml` `[limits]`. **Inert until a tier is selected** (`tier` in TOML or `DEEPAGENTS_PROVIDER_TIER`) |
| — | Ephemeral workspace + live refresh | ✅ Built | `run-docker -Ephemeral`/`EPHEMERAL=1` mounts a throwaway copy (revert on close, `-SaveWorkspace` snapshots first); `harness/refresh.py` pulls live host edits into it mid-run via `/refresh` or the `refresh_workspace` tool. Inert on a normal run |
| 8 | Observability, telemetry, telemetry-to-PR | ⬜ Planned | No `usage.jsonl`/`.agent-metrics.json` sink, no trace file, nothing appended to PR descriptions. The **data** exists — M1's tracker computes per-turn tokens/cost/energy and M2 persists a per-session ledger on the `past.sqlite` `sessions` row; this row is the missing sink + PR surface. Do not confuse with the HITL audit trails (`interrupts.jsonl`, `denials.jsonl`), which are built but are an approval record, not telemetry |
| 9 | In-container interactive REPL (multi-turn session) | 🟡 Partial | MVP-scope loop, since extended. Persistent `docker run -it` prompt in `harness/cli.py`: multi-turn on one `thread_id`, deterministic `/exit`, stage output (`docs/milestones/complete/mvp.md` §1a), plus `/config`, `/recall`, `/topic`, `/refresh`, `/show` and the HITL prompts. Not the §9 TUI |
| 9 | Host CLI frontend (Typer/Rich) + TUI | 🟡 Partial | No Typer/Rich CLI and **no TUI**; interactive use is the in-container REPL above. What does exist is a set of keyless argparse subcommands usable from the host — `harness config` / `config security` / `doctor` / `threads` / `past` / `mask-scan` — routed by `dispatch`. Running those on a host *without* the runtime stack installed is M5 §0.1 F6 (`fix/f6-lazy-entrypoints`, PR #44) |
| 9 | Unified config surface (flags + `/config` + wizard, one precedence chain) | ✅ Built (Milestones 5 + 5.1) | One resolver (`harness/config.py`): CLI flag > env > `.harness-profile.yaml` > default, provenance-tagged. In-session `/config` for live knobs, `harness config` wizard for the ones fixed at container start. M5.1 made one `FieldSpec` registry the single declaration everything derives from, and validates enum values at every point of entry. See `docs/milestones/complete/milestone5.md` |
| 9 | Human-in-the-loop (interrupt spine + `ask_human` tool + `.harness-config.yaml`) | ✅ Built (Milestone 3) | LangGraph `interrupt()` over the SqliteSaver checkpoint; one human channel, all 3 trigger sources (deterministic pause middleware, `ask_human` tool, system `provider_error`). Off unless `.harness-config.yaml` exists. `permission_denied` shipped **audit-only** in M4 slice D (see Path Guard above). **Still not implemented:** `missing_price` system event, `shadow` policy, clock-pause on interrupt, host TUI. See §9 + `docs/milestones/complete/milestone3.md` |
| 10 | Security verification test suite | ✅ Built (Milestone 4 slice G) | `test_mask`, `test_pathguard`, `test_jail`, `test_nsguard`, `test_seccomp`, `test_apparmor`, `test_doctor` — the boundary invariants of `docs/milestones/complete/milestone4.md` §19, run in CI. The committed seccomp/AppArmor artifacts are asserted offline, so a widened profile fails the host tier |
| 11 | Future extensions & roadmap | 🔬 Research | By definition |
| 12 | CI pipeline for the harness repo | ✅ Built (Milestone 4 slice F) | `.github/workflows/ci.yml`: `host-tests`, `image-tests`, `smoke`, `parity` (the `.ps1`↔`.sh` guard). Since M4.1 fork J2 the `smoke` job loads the narrowed AppArmor profile into the runner kernel and pins `JAIL_CHECK=1`, so the bwrap jail is a **gate**, not an environmental skip; a masks-on control runs beside it non-gating. The `apparmor-load-probe` job that measured this was deleted once it answered |
| 12 | Config-validate / `harness doctor` | ✅ Built (Milestone 4 slice E) | `harness/doctor.py`: pre-flight validation of the resolved config, mask policy, state-dir placement (errors when an in-container state dir lands inside the workspace), and the seccomp/AppArmor artifacts + a live bwrap unshare probe |
| 12 | Headless one-shot-to-PR mode | ✅ Built (Milestone 3) | `cli.run_batch` / `--headless` (`DEEPAGENTS_HEADLESS`): run task(s) to completion, emit one JSON result on stdout, meaningful exit code. **PR URL not yet in the JSON** (git-pr logs it to stderr at session.end). Pulled in as M3 prereq P2 |
| 12 | Provider resilience (retry/backoff + context-overflow fallback) | ✅ Built (Milestone 3) | `harness/resilience.py` + `cli._invoke_resilient`: bounded jittered backoff on 429/5xx/reset (`DEEPAGENTS_MAX_RETRIES`/`_RETRY_BASE`) + one-shot context-overflow trim (pre-§7 stopgap). Pulled in as M3 prereq P1 |
| 12 | Thread / checkpoint management | 🟡 Partial | **Shipped in Milestone 2** (`docs/milestones/complete/milestone2.md`): fresh-by-default present thread + separate on-demand `past.sqlite` archive, **and** the `harness threads`/`harness past` list/show/rm/prune lifecycle CLI (§2.6). Automatic/policy-based GC still deferred (manual prune only) |
| 12 | Deepagents-native skills & memories wiring | 🟡 Partial | **Milestone 2** shipped the accumulating on-demand "past" archive (`past.sqlite`) and (§2.7) **disambiguates** it from deepagents' native `memories/` surface — no dead scaffolding now implies M2 wired the latter. `project/agents/`,`skills/`,`memories/` native wiring (Option A/B) remains deferred to §12.6 |
| 12 | Cost / telemetry persistence | 🟡 Partial | **Milestone 2** (§2.3) shipped the first slice: a per-session token/cost ledger on the `past.sqlite` `sessions` row (provenance-tagged, NULL when keyless). §8 telemetry-to-PR export of that ledger still deferred |
| 13 | File-read middleware (per-file context shaping) | ⬜ Planned | No read-time transform seam; `read_file` serves whole files. Planned: pipeline on the backend `read()` override; tag add/omit + in-file progressive disclosure as instances |

---

## Product Identity — Core vs. Peripheral

> Orthogonal to the ✅/🟡/⬜/🔬 build-status matrix above. That matrix says *what's built*; this says
> *what the harness isn't itself without*, regardless of build order. Some core items ship late
> because they have real dependencies — later ≠ optional. Anything not listed here is default
> priority: built when it's next in line, not because it defines the product.

### Core identity — dependency chain (ship order = dependency order)

1. **M4 slice H — bwrap fs-tool jail** (`docs/milestones/complete/milestone4.md` §4H/§11.4;
   design_doc §2 Bubblewrap Configuration). **Built, opt-in (`DEEPAGENTS_JAIL=1`)** — the real
   allow-list boundary, routing **both** shell and file tools through the jail via a re-exec of the
   harness into a bwrap namespace. Everything below that claims per-agent isolation is aspirational
   *unless the jail is on*; with it off — the default, because it needs a narrow seccomp relaxation
   on the outer container — the boundary remains the docker mount-mask (deny-list,
   present-but-empty), not a sandbox. Items 2–3 below can now build against a real bind boundary,
   but must not assume it is enabled.
2. **`HarnessProfile`** (§2 "HarnessProfile dynamic bind mounts", §4 "Specialized Profiles") —
   per-agent scoped tool/bind config. Depends on (1): profile bind-scoping is only real once bwrap
   enforces it; today there's a fixed bind list and no profile object at all.
3. **Per-agent `NetworkPolicy`** (§2 "Per-Agent Network Policy") — subtractive egress/host-service/
   net-tool gating per agent, layered on the container-wide NetJail (built). Depends on (2) —
   `NetworkPolicy` is a field on `HarnessProfile`. App-layer/env-based, not kernel-enforced, so it's a
   real but soft boundary on its own; meaningful mainly paired with (1).
4. **Config-driven allowlist selection** (§2, `@group` / `enabled.txt`) — small, near-independent;
   makes (3)'s container-wide allowlist operable from a menu instead of hand-edited comment toggles.
5. **Routing gate — LLM-or-script based, not the FSM classifier** (§3 "Funnel", §4 "Router
   Architecture"). Core identity is *routing exists* (a per-input gate selects a target), not the
   specific Qwen/FSM-constrained-output implementation — that stays 🔬 research, no accuracy baseline.
   Ship a deterministic-predicate or plain-LLM-call gate first (§3 already specs this: "a funnel ships
   with a trivial deterministic gate and upgrades to a classifier/LLM criterion without changing
   shape"); the FSM classifier is one future gate criterion, not a prerequisite.
6. **Multi-agent funnel** (§5, "classifier→orchestrator→worker") — the control-flow action of (5),
   dispatching to N targets. Depends on (5) for the gate and (2)/(3) for scoping what each funneled
   agent can touch — a funnel with no per-agent isolation is one trust boundary shared by every
   subagent, which undercuts the point of funneling.

### Core identity — independent of the chain above

- **Telemetry** (§8 Observability + §12.7 Cost/telemetry persistence) — session-level trace/metrics
  plus PR-appended summaries. The cost tracker (M1, built) already computes the data; this is the
  missing on-disk sink (`usage.jsonl`) and PR-surface (§8 "PR Metadata").
- **File-read middleware** (§13) — per-file read-time shaping (tag-gated hide/note regions,
  progressive disclosure). Standalone seam; no dependency on the chain above.

---

## 2. Sandboxing Strategy & Container Layout
> **Status:** 🟡 Partial — built: single container + conda isolation + secret provisioning +
> persistent workspace + resource limits + opt-in container-wide NetJail (deny-all egress +
> allowlist) + workspace masking + path guard + the **opt-in bwrap fs-tool jail** (M4 slice H,
> `DEEPAGENTS_JAIL=1`; all three kernel gates closed and measured on a live AppArmor host by M4.1,
> with SELinux/rootless still untested and saying so). Still **planned**:
> dual-container, `HarnessProfile` binds, per-agent network policy, config-driven allowlist
> selection. See the status matrix above.

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

> **Status:** ⬜ Planned. Extends the container-wide NetJail (`deepagent-image/netjail/`, opt-in
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

> **This is a policy, not a cage — same trust caveat as the MVP shell (`docs/milestones/complete/mvp.md` §5).**
> All subagents run in-process, sharing one network namespace and one proxy. Env-withholding stops a
> *cooperative* agent and a prompt-injected one that only knows the documented env, but an agent that
> hardcodes the proxy IP or a forwarder address can still reach it. It is meaningful **only while
> `NET_JAIL=1`**: without the jail the container has open egress and withholding proxy env changes
> nothing (direct internet still works). For a kernel-enforced per-agent boundary — or for true
> per-domain subsets, which one shared tinyproxy cannot attribute to an in-process caller — the agent
> must move to its own container/netns (the Dual-Container / Executor-Sandbox direction above). That
> is the strong-isolation successor, out of scope for the in-process Tier-1 mechanism specified here.

### Config-Driven Allowlist Selection

> **Status:** ⬜ Planned. Makes the container-wide NetJail allowlist toggleable programmatically
> (e.g. from a startup menu) instead of by hand-editing comments in `allowed-domains.txt` /
> `host-services.txt`.

Today an entry is enabled by *uncommenting* it. That does not scale to a UI: a menu would have to do
string surgery on comment characters, and it conflates "not in the catalog" with "in the catalog but
off." Split the two concerns — a static **catalog** (every known destination, human-authored) and a
small machine-written **selection** — so a menu only ever rewrites the selection.

**Hard constraint:** `run-docker` parses these files on the **host** in PowerShell / bash *before any
container exists*, so the format must stay shell-greppable — no host-side TOML/JSON parser dependency.

**Catalog** — `allowed-domains.txt` / `host-services.txt` gain an optional `@group` tag per line:

```
generativelanguage.googleapis.com   @google     # google_genai model API
api.openai.com                      @openai     # openai model API
github.com                          @git        # git push / fetch
api.smith.langchain.com             @tracing    # LangSmith telemetry
```

*   A leading `#` still means **hard-off** — absent from the catalog for this run, never selectable.
*   `@group` is the toggle unit a menu presents (one checkbox per distinct group).
*   A line with **no** `@group` is always-on **base** (e.g. an org that must always be reachable).

**Selection** — `netjail/enabled.txt`, one group per line, written by the menu (nothing else edits it):

```
google
git
```

An env override `NETJAIL_GROUPS=google,git` is honored when set, for one-off runs; `enabled.txt` is
the persisted default. (`enabled.txt` is runtime state — gitignore it.)

**Effective allowlist** = every non-`#` catalog line whose `@group` is in the selection, **plus** every
untagged (base) line. `run-docker` / `smoke` compute this in place of today's "every non-`#` line."

**Back-compatible.** The current parser already takes the first whitespace token of each non-`#` line;
this adds only "strip a trailing `@tag`, then filter by selection." A catalog with no `@group` tags and
no `enabled.txt` behaves exactly as today (all uncommented lines on), so the migration is additive.

This is the **run-level** companion to the per-agent `NetworkPolicy` above: groups decide what the
*container* may reach; `NetworkPolicy` decides which subset each *agent* may reach within that. The
startup menu is the writer of `enabled.txt`; the four scripts (`run-docker` / `smoke` × `ps1`/`sh`)
are the readers and must stay in sync.

### Path Guard Middleware
> **Status:** ✅ Built (Milestone 4 slice C/D) — `harness/pathguard.py`, on the fs tools. A denial
> always prints to stderr and, under HITL, is recorded to `<state-dir>/denials.jsonl` (outside the
> workspace, so the agent cannot truncate the evidence). Audit-only: never interactively approvable.
> The sketch below is the original spec; the shipped guard follows it. See the status matrix above.

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

### Workspace Visibility & Secret Masking
> **Status:** ✅ Built (Milestone 4 slices A–C) — `harness/mask.py`: `.agentignore` with
> gitignore-parity matching, the 3-tier policy, deny/allow modes, the un-negatable
> designated-secret floor, and the docker mount-mask that makes masked paths present-but-empty.
> `DEEPAGENTS_MASK=0` restores M3 behaviour byte-for-byte. Full spec:
> **`docs/features/workspace_visibility.md`**; shipped scope in
> `docs/milestones/complete/milestone4.md`.

The bind mount exposes the *whole* workspace tree — including secrets the user's own repo carries
(`.env`, `id_rsa`, `.aws/credentials`) — to the agent's file **and** shell tools. A policy +
enforcement stack restricts agent-visible paths:

*   **Policy** — an `.agentignore` config (gitignore parity: `**`, `!`, per-dir nesting), resolved by
    one Python matcher run as a read-only pre-flight container (no double shell reimplementation).
    Deny-list by default (agent sees all but denied paths); allow-list mode for scoped tasks (only
    listed base dirs + what the agent creates — this §2's "whitelist of allowed base directories").
*   **Designated-secret floor** — a tier of user-marked secrets that is **always blocked at every
    layer, regardless of agent / model / allow-list / bwrap setting** (no `!`, no allow-list, no
    flag). Enforced redundantly across layers. The authoritative config for it lives in the state
    dir (outside the mount, agent-unreachable); an append-only `mask_add` tool lets the agent *raise*
    protection, never lower it.
*   **Enforcement, layered:** (1) **docker mount mask** — deny-list overlay-empty, covers all tools,
    buildable now, always-on floor enforcer, but whole-tree + present-but-empty (not sandboxing);
    (2) **bwrap** — the real allow-list boundary, with **all fs-touching tools (shell + file
    read/write/edit) routed through the jail** so the in-process file tools can't bypass it (wire
    `sandbox-exec`, verify nested userns first); (3) **overlayfs view** — optional, tool-agnostic
    true absence + upper-diff write-back.

See `docs/features/workspace_visibility.md` for the tier table, config format, scanner, and sequencing.

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
> **planned**. Docker host-boundary control, not a sandbox (`docs/milestones/complete/mvp.md` §5).

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
> the flat always-gate precursor, adapted into the same engine. Still **planned**: the classifier gate,
> the context-mutation / control-flow action tiers, and the multi-stage `stages:` format (bundled with
> context-mutation — see "Multi-stage workflows" below). See the status matrix above.

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
    *   **Human-in-the-loop (pause)** *(built — Milestone 3)* — suspend the run at this hook point,
        surface a structured prompt to the human channel, and block until a typed reply returns, which
        the step routes on (approve / deny / edit). Deterministic here: the **gate** decides *whether*
        to pause (`always`, or a predicate — `*.env` touched, cost over budget, dirty `git status`), so
        a workflow encodes "always confirm before X" with no model in the loop. Built on LangGraph
        `interrupt()` + the SqliteSaver checkpoint, so the suspended turn is durable and resumable
        across process restarts. **Implementation note:** it ships as `hitl.PauseMiddleware`, not a
        `pause` step type here — workflow steps are subprocess side-effects and cannot suspend the
        graph in-process. This is the deterministic, workflow-bound half of HITL (§9); the
        agent-initiated half is the `ask_human` tool.

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

### Multi-stage workflows (`stages:`) — planned
> **Status:** ⬜ Planned. Bundle with the **context-mutation action tier** above — it is the tier
> that makes multi-stage necessary (in-memory hand-off between stages), and it requires the same
> middleware change (hooks that *return* a value the engine applies). Until it lands, the
> paired-folder pattern below is the stopgap.

**The gap.** A workflow today binds to **one** hook point (`hook:` is a single scalar). A logical
task that touches several points in the lifecycle — inject context at `model.start`, *then* emit a
follow-up at `model.end`/`agent.end`; branch at `session.start`, *then* PR at `session.end` — has no
single-folder representation. It must ship as **two independent folders** coordinating through a file
(`session.env`), even though the design already names such a set **one** workflow (see the git
lifecycle below, which the code splits into `git-branch` + `git-pr`). The terminology and the
structure disagree.

**The model.** One folder = one workflow = an **ordered list of stages**, each stage being the
existing trigger × hook × action triple. The folder name is the workflow (`git-session-lifecycle`);
a **stage** is one hook × gate × steps within it. The single-hook frontmatter stays valid as sugar
for a one-stage list — shipped folders don't change.

```markdown
---
name: git-session-lifecycle       # == folder name; the workflow
stages:                           # ordered; each stage is one hook × gate × steps
  - hook: session.start
    gate: branch.trigger.py       # per-stage gate (basename no longer fixed)
    steps: [./create-branch.sh]
  - hook: session.end
    gate: pr.trigger.py
    steps: [./open-pr.sh]
---
Branch at session start, open a PR at session end. Never auto-merges.
```

**Why it's cheap.** The internal representation does not change. `_load_workflow` returns a
**`list[Workflow]`** (one per stage) instead of one `Workflow`, each with a synthesized name
(`git-session-lifecycle:session.start`) and a `group` field = folder name for telemetry/provenance.
Everything downstream — `workflows_by_hook`, `run_hook`, `WorkflowMiddleware` — already operates on a
**flat `list[Workflow]`**, so dispatch and the middleware are untouched. The change lives entirely in
the loader + frontmatter parser.

**The one invariant that relaxes.** The fixed-basename gate (`_resolve_gate`: exactly one
`./trigger.{py,sh}` per folder) becomes **per-stage** — each stage names its `gate:`, defaulting to
`trigger.py`/`trigger.sh` for the single-stage sugar form so existing folders resolve unchanged.

**State hand-off — the real payoff.** Paired folders can only share **on-disk** state (`session.env`
via `GateContext.as_env`), which is fine for side-effect subprocess steps but not for in-process
context-mutation stages that pass live objects (the injected context an earlier stage stashed for a
later follow-up). Co-locating stages in one folder gives them a natural shared namespace — a sibling
module the stages import — instead of marshaling live state through a file. This is why multi-stage
and the context-mutation tier land together.

### Canonical workflow: Git session lifecycle
A deterministic workflow whose body is git side-effects — the worked example the engine generalizes.
Because a workflow binds to **one** hook point *today*, it ships as a **paired set of two folders**
that share state via `<workspace>/.deepagents/session.env` (written at start, reused at end):
`git-branch` on `session.start` and `git-pr` on `session.end`. This is the stopgap for the
multi-stage gap above — once `stages:` lands the pair collapses into one `git-session-lifecycle`
folder with two stages. Each is a safe no-op without its
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
    *   *Cons*: Higher development overhead; requires manual tuning of prompts. The control is real
        but not via the obvious parameter — `system_prompt=` cannot replace the SDK base prompt, and
        profile keys cannot name an Ollama tag. See "Per-agent prompts & profiles" below before
        designing around this.
    *   *Best For*: Expert Main Orchestrators.

*   **Hybrid Combination (Recommended)**:
    *   **Classifier (Pi-style)** $\rightarrow$ **Orchestrator (Custom)** $\rightarrow$ **Worker (Base)**.
    *   Ensures a "funnel" of efficiency: fast classification, precise planning, and reliable execution.

### Per-agent prompts & profiles — measured constraints

> **Measured 2026-08-17** against the installed `deepagents` (`langchain-ollama` 1.1.0), using the
> M7 raw trace as the instrument — every claim below is an observed assembled system prompt, not a
> reading of the docstrings. Probe scripts are disposable; the numbers are reproducible by setting
> `DEEPAGENTS_RAW_TRACE=file` and diffing the `--- system ---` section.
>
> `deepagents.profiles` is flagged **beta** by its own docstring. Everything here is a current-version
> observation and needs a pinning test if the funnel depends on it.

The "Custom Agents" option above promises *absolute control* over prompts via `HarnessProfile`. It is
reachable, but not through the obvious parameter, and four constraints shape how a funnel must be
built. They matter now because they are cheap to design around and expensive to retrofit.

**1. `system_prompt=` cannot replace the SDK base — it only prepends.** Assembly is
`USER` → (`BASE` or `CUSTOM`) → `SUFFIX`, joined by blank lines. `create_deep_agent(system_prompt=…)`
is the `USER` slot and always lands in front of `BASE_AGENT_PROMPT`; no value suppresses it. The
replacement lever is `HarnessProfile.base_system_prompt` (the `CUSTOM` slot). Measured on a real
turn: assembled system prompt 10271 chars → **8039** with `base_system_prompt` set, SDK base absent,
harness `USER` text still first.

**2. Neither prompt is where the tokens are.** Of that 10271: harness `BASE_SYSTEM_PROMPT` 306,
`AGENTS.md` 2227, SDK `BASE_AGENT_PROMPT` 2258 — the remaining **~5500 is middleware-injected**
(filesystem, todo, subagent). A funnel that shrinks worker context by rewriting prompts is optimizing
the smaller half; `excluded_tools` / `excluded_middleware` are the bigger lever, and the harness
already exposes the tool half as `DEEPAGENTS_LEAN_TOOLS` / `DEEPAGENTS_EXCLUDE_TOOLS`.

**3. Profile keys cannot express an Ollama tag, and a two-colon spec disables profiles silently.**
`validate_profile_key` permits at most one `:`, so `ollama:gemma4:harnesstest1` is unregisterable —
an Ollama *tag* contains the colon the grammar spends on the provider separator. Worse,
`resolve_harness_profile` returns `None` on `spec.count(":") > 1` **without consulting the registry**,
so a two-colon spec matches nothing at all — not even a provider-level registration. What decides
which path a model takes is unrelated to prompts: `providers.resolve_chat_model` returns a
constructed client only when the model has `[options]` or a rate limiter, else the bare string.

| what `create_deep_agent` receives | exact key `ollama:gemma4` | provider key `ollama` |
|---|---|---|
| string `ollama:gemma4` (one colon) | wins | fallback |
| string `ollama:gemma4:harnesstest1` (two colons) | ✗ | ✗ **nothing applies** |
| model object, identifier has no colon | wins | fallback |
| model object, identifier is a tag | ✗ | wins |

Objects degrade gracefully (provider always resolves, since the provider is read structurally rather
than parsed); two-colon *strings* fail closed and silently. **A funnel must normalize this at the
boundary** — either guarantee a model object reaches `create_deep_agent`, or fail loudly — because in
a multi-agent build the symptom is one worker quietly running an un-profiled prompt while the others
look correct.

There is no alias seam. `_harness_profile_for_model` accepts a `spec` string only when `model` is
itself that string, and the same string builds the client — so a normalized key like
`ollama:llama3.1-8b` would resolve the profile correctly and then 404 at the daemon. Renaming models
upstream (`ollama cp llama3.1:8b llama3.1-8b`) works but forks local model names from everyone else's
and costs a step per model.

**4. Profiles bake at build time, which makes per-model keys unnecessary.** The assembled prompt is
fixed when `create_deep_agent` returns; a later registration does not reach an already-built agent
(measured: agent one kept its prompt after the registry was rewritten for agent two). So the funnel
can register the profile it wants **immediately before each agent's build** and get per-agent
profiles with no key at all — sidestepping constraint 3 entirely.

Two things bound that technique:

- **Registration merges; omitted fields leak.** `_register_harness_profile_impl` merges onto an
  existing registration ("scalar fields prefer the new value, set and middleware fields union").
  Measured: a third registration setting only `system_prompt_suffix` silently inherited the second
  agent's `base_system_prompt`, and the SDK base did **not** come back. Per-agent switching must set
  every field explicitly on every registration, including passing `BASE_AGENT_PROMPT` when the
  default is what is wanted. Omission is not reset.
- **Construction is a critical section; execution is not.** `_HARNESS_PROFILES` is a plain
  module-level dict — no lock, no `ContextVar`. Register→build must not interleave across agents, or
  a builder reads a profile another builder wrote (and the merge means both end up with fields
  neither asked for, with no error). **Running** built agents in parallel is safe, since a compiled
  graph never consults the registry again.

**Design rule for the funnel.** Split the two axes rather than keying prompts on models:

- *Role* varies per agent (classifier / orchestrator / worker) → author it on the agent, via the
  subagent spec's own `system_prompt` or the `USER` slot. A plain argument, immune to all of the
  above. `GeneralPurposeSubagentProfile.system_prompt` covers the auto-added GP subagent and beats
  `profile.base_system_prompt` for that stack.
- *Model family* is shared → one `HarnessProfile` (base override, tool exclusions, suffix) registered
  **once, before any build**. Then the critical section happens exactly once at startup and the
  concurrency question does not arise.

Note `system_prompt_suffix` is applied to **every** stack it can reach — main agent, declarative
subagents, and the GP subagent each receive it on top of their own base. It is not a main-agent knob.

Sequential per-agent registration additionally requires each agent be its **own** `create_deep_agent`
call (a `CompiledSubAgent`), since declarative subagents are assembled inside the parent's single
call and all resolve against whatever is registered at that moment.

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
> specified, not built — see `docs/specs/energy.md`). See `docs/milestones/complete/milestone1.md`.

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
> **Status:** ⬜ Planned — no `usage.jsonl` / `.agent-metrics.json` / trace file is written and
> nothing is appended to PR descriptions. Note what *does* exist, so this is not rebuilt from
> scratch: M1's tracker already computes per-turn tokens/cost/energy, and M2 persists a per-session
> ledger on the `past.sqlite` `sessions` row — this section is the missing **sink** and **PR
> surface** over data that is already collected. The HITL audit trails (`interrupts.jsonl`,
> `denials.jsonl`) are built but are an approval/boundary record, not telemetry. See the status
> matrix above.

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
> `/exit`, lifecycle stage output — see `docs/milestones/complete/mvp.md` §1a), since grown to
> carry `/config`, `/recall`, `/topic`, `/refresh` and the HITL prompts. `.harness-config.yaml`
> **shipped in Milestone 3** (HITL), and Milestone 5 added the `harness config` wizard plus
> `.harness-profile.yaml`. Still ⬜ Planned: the **host-side Typer/Rich `harness` CLI and the TUI** —
> today's host surface is plain argparse subcommands. See the status matrix above.

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

### Human-in-the-Loop (HITL)
> **Status:** ✅ Built (Milestone 3) — the interrupt spine and all three trigger sources ship. See
> `docs/milestones/complete/milestone3.md` (esp. §0 build status) and
> `deepagent-image/CLAUDE.md` → "Human-in-the-loop (Milestone 3)" for detail.
>
> **Not implemented (this design's optional/open parts):**
> - **Deterministic pause = an `AgentMiddleware`, not a `workflows.py` `pause` step** — workflow
>   steps are subprocess side-effects and cannot suspend the graph in-process, so the pause tier is
>   `hitl.PauseMiddleware`. The `session.end` PR gate **is** a blocking approval (`cli._pr_approval`
>   ahead of the git-pr workflow) for the strict/guided presets — an interactive veto; headless and
>   the autonomous preset let the PR proceed (git-pr never auto-merges).
> - **System-event source 3, one of three still open:** `missing_price` is a recognized
>   `.harness-config.yaml` key but **not enforced** — blocked by the cost-module no-sibling-import
>   guard, so it belongs in a separate reader middleware. `permission_denied` **shipped in Milestone
>   4 slice D, audit-only**: a path-guard denial prints to stderr always and appends to
>   `<state-dir>/denials.jsonl` under HITL, but never offers an interactive approve — every denial
>   it can currently raise is a true workspace escape, which must not be waveable-through by a
>   mis-click. `provider_error` is built (retry/abort; `switch provider` not offered — needs an
>   agent rebuild).
> - **`interruption_policy: shadow`** — not built; only `blocking` ships (the shadow-mode ordering/
>   resume UX is the one open fork, milestone3 §6).
> - **Budget/clock pause-on-interrupt** ("pause the clock") — not wired; M1 caps still tick while a
>   human decides.
> - **Host TUI channel** (Rich prompt / batched review panel) — deferred; the in-container REPL is
>   the only channel. (S6 PR-b, the `choose` arrow-key menu, **is** built — inline ↑/↓ + Enter menu
>   with a typed-index fallback off-TTY.)

HITL is not a feature bolted onto the CLI — it is **one interrupt spine** with three trigger
sources feeding a single **human channel**. The spine is LangGraph's `interrupt()` over the
SqliteSaver checkpoint already in place (status row: "Conversation checkpoint"): any point in the
graph can suspend, persist the exact state to `checkpoints.sqlite`, surface a structured prompt,
and resume on the human's typed reply — durably, across process restarts. The human channel is
wherever the human is: the in-container REPL prompt today (the MVP loop above), the Rich prompt /
batched review panel in the host TUI later.

An **interrupt request** is a small structured object —
`{kind, prompt, options, context, default, timeout_policy}`, `kind ∈ {approve, choose, input,
resolve}`. `options` carries the approve/deny pair or a fixed choice set; `context` carries the
salient state (the diff, the command, the price gap). The human's response resumes the graph and,
for a workflow step, is the value the step routes on.

**Three trigger sources, one spine:**

1.  **Deterministic — workflow-bound (the pause action tier, §3).** A workflow whose gate is a
    predicate (`always` or a condition) and whose action is *pause*. The gate decides *whether* to
    interrupt with no model in the loop: `always` before a PR push, or a predicate — a
    `review_triggers` path match (`*.env`, `auth_logic.py`), a dirty `git status`, a turn/cost
    counter over threshold. This is where `autonomy_level` lives: the presets below just install
    different sets of these pause workflows. Fires at any of the hook points — most usefully
    `tool.start` (gate a tool call) and `session.end` (gate the PR).
2.  **Agent-initiated — the `ask_human` tool.** A Deep Agents tool the agent calls when *it* decides
    it is blocked: ambiguous requirements, a missing credential, a design fork it should not guess.
    Same interrupt object, same channel; the difference is the model chooses to raise it rather than
    a predicate. Non-deterministic by design — the agent's escape hatch, complementary to the
    deterministic gates.
3.  **System-event — raised by the harness itself.** Existing failure/uncertainty points promote to
    an interrupt instead of a silent log or a hard crash:
    *   **Missing price warning** (Milestone 1). When a model has no `[pricing]` entry the cost
        ledger records NULL and today proceeds keyless. With HITL on, an interrupt surfaces *"model
        X has no price — continue untracked / abort / enter a rate?"* before spend accrues.
    *   **Provider errors** (§12 resilience). A 429/5xx/context-overflow that exhausts retry/backoff
        raises to the human — *"retry / switch provider / abort"* — instead of failing the turn.
    *   **Permission / security gates** (§2, §10). A path-guard rejection, a NetJail-blocked egress,
        or a bwrap denial becomes a resolvable prompt — *"this command wants to write outside
        /workspace — allow once / deny"* — rather than an opaque failure.

**Autonomy presets & policy.** Users configure defaults via `.harness-config.yaml`:
*   **`autonomy_level`** — preset pause-workflow set:
    *   `strict`: human approval for *every* tool call (high safety, low speed).
    *   `guided`: approval only for `CRITICAL` tasks and filesystem deletions (balanced).
    *   `autonomous`: approval only for the final PR submission (high speed, lower safety).
*   **`review_triggers`** — keyword/path patterns that force a pause regardless of `autonomy_level`.
*   **`interruption_policy`** — how a pause presents: **blocking** (stop and wait at the interrupt
    point) or **shadow** (agent continues in a change-batched mode; all pending interrupts collect
    into one review pass at `agent.end`/`session.end`).
*   **`system_interrupts`** — which harness events (source 3) raise vs. log/crash.

Each interrupt request may carry a `timeout_policy` (fall through to `default`, or hold) so an
unattended headless run (§12) has defined behavior when no human is present.

```yaml
autonomy_level: guided            # strict | guided | autonomous — preset pause-workflow set
review_triggers:                  # force a pause regardless of level
  - "*.env"
  - "auth_logic.py"
interruption_policy: blocking     # blocking | shadow
system_interrupts:                # which harness events raise (vs. log/crash)
  missing_price: true
  provider_error: true
  permission_denied: true
```

---

## 10. Security, Verification, & Testing Plan
> **Status:** 🟡 Partial — the **security verification suite is built** (Milestone 4 slice G:
> `test_mask`, `test_pathguard`, `test_jail`, `test_nsguard`, `test_seccomp`, `test_apparmor`,
> `test_doctor`, driven by `docs/milestones/complete/milestone4.md` §19 and run in CI). The risk analysis below and
> the automation-validation tests remain a plan. See the status matrix above.

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
*   **Raw prompt/response debug mode** — **BUILT as Milestone 7**
    (`docs/milestones/complete/milestone7.md`; authoritative where the two disagree).
    `DEEPAGENTS_RAW_TRACE` / `--raw-trace` / `/config set raw_trace` is a four-valued knob
    (`off`/`file`/`console`/`both`) that records, **per model call**, the literal payload the
    harness hands the model and the whole object the model hands back — final system prompt, full
    message history, tool schemas, tool-call/tool-result blocks, every reply content block in order
    (reasoning included), `tool_calls` with raw args, invalid tool calls, and the metadata bags.
    Only separators, indices and counts are added. Distinct from `DEEPAGENTS_DEBUG` (checkpointer
    state, failure-only): this fires every model call, success or failure — for diagnosing
    weak/local-model tool-calling failures (hallucinated tool JSON, ignored instructions, a tool
    the model never saw).

    **Correction to this entry's original wording:** "raw tags included, e.g. Ollama's
    chat-template markers" is **not deliverable client-side** and was never promised by the
    implementation. Ollama renders the chat template *server-side* inside `/api/chat`; the body the
    harness sends is JSON. M7 §3 splits fidelity into three levels and ships **L1, the message
    level** — the final `system_message`/`messages`/`tools` at the innermost middleware seam, held
    to the standard *nothing dropped* rather than *nothing added*. L2 (the literal HTTP body, via an
    `httpx` event hook) is deferred, not rejected. L3 (the template-rendered token string) needs the
    model server's own debug logging — `OLLAMA_DEBUG=1` with a foreground `ollama serve`, or the
    template from `/api/show` — and the record header names its own level so an operator cannot
    mistake one for the other.
*   **Automated Benchmarking Suite**: Quantitatively measure the harness (routing, compression, memory) against known-correct coding tasks. Budget is tight — full SWE-bench (2294 instances) is out of reach — so the plan is a cost-tiered ladder, cheapest signal first, built on a shared batch driver.

    **Benchmark tiers (cheapest first):**
    1.  *Gold set (free, CI regression).* A small pinned set (5–20) of bug-fix tasks — each `{id, repo/dir, base_commit, task_prompt, fail_to_pass[]}` — run against **local/free models** (ollama/lmstudio, already in the provider registry) for **$0**. This is the primary regression signal for "did a harness change break the loop." Built first.
    2.  *Aider polyglot (cheap agentic).* The Exercism-based exercise set: self-contained stub+test dirs, no clone-at-commit, exercises the real edit→run-tests loop cheaply.
    3.  *SWE-bench Lite / Verified subset (reportable, spend deliberately).* Run a fixed **25–50 instance sample** (not the full set) with a cheap model as the reportable configuration; prefer **Verified** (human-validated, fewer broken instances) for signal-per-dollar.

    **Harness gaps to close (not yet built):**
    -   *Batch eval driver* (`harness/bench/`, host-runnable/stdlib): iterate a dataset, prep each instance's workspace, run the harness **headless** (the existing non-TTY single-turn path), capture the result.
    -   *Prediction/patch output mode* (`--emit-patch`): emit a clean `git diff` prediction (jsonl `{instance_id, model_name_or_path, model_patch}`) **excluding** harness artifacts (`.deepagents/`, `.agent_telemetry/`, and the workspace `.conda/` env). The current git lifecycle produces a commit→PR, not a scorable patch.
    -   *Per-instance hard stop* (`--max-turns` + session wall-clock): the M1 cost/token caps don't bound a stuck instance on a **free/local** model (cost never accrues), so a turn/time bound is required to cap runaway instances.

    **Decided approaches (design forks, resolved):**
    -   *Scoring:* **reuse the official evaluation harnesses** (SWE-bench eval harness, Aider runner). The only contract the harness must satisfy is the predictions jsonl — no bespoke scorer, keeping numbers standard-comparable.
    -   *Anti-cheat network posture:* run the solve under **NetJail** with a **minimal allowlist** (pypi/registry only) so the agent cannot fetch the upstream fix from GitHub. Any egress the instance setup needs must be granted in `netjail/*.txt` or it fails closed.
    -   *Per-instance environment setup:* **adopt SWE-bench's per-instance Docker images** (repo + deps at `base_commit`) and inject the agent into them, rather than remapping every repo into the workspace conda model. This deliberately bypasses the two-stack conda convention **for the benchmark path only**. Open detail to pin before this tier: confirm the harness venv (`/opt/venv`, runs `main.py`) can ride inside those images without colliding with the instance interpreter (mount, or install the harness to a fixed path).

    Tiers 1–2 need only the batch driver + patch output + hard stop; the network and image decisions apply at the SWE-bench tier.
*   **Self-Tuning Classifier**: Implement a feedback loop where the orchestrator reports the "correctness" of the initial routing decision, used to fine-tune the local classifier model via DPO or RLHF.
*   **Classifier Knowledge Distillation**: Use a high-reasoning "Teacher" model (e.g., Claude 3.5 Opus or GPT-4o) to label a large dataset of prompt-category pairs. Fine-tune the lightweight local classifier (the "Student") using this high-quality synthetic data to align its routing decisions with the teacher's reasoning.
*   **IDE Integration**: Develop a VS Code extension to allow the harness to be controlled directly from the editor, providing inline "agent-suggested" diffs.

### Advanced Optimization
*   **Speculative Execution**: Run the local orchestrator and cloud orchestrator in parallel for `MODERATE` tasks; use the cloud result to verify the local one, optimizing for both speed and accuracy.
*   **Dynamic Prompt Compression**: Adjust Caveman/Headroom intensity levels in real-time based on the current token window usage and the importance of the current task.


---

## 12. Operational Hardening & Automation Roadmap
> **Status:** 🟡 Partial — these were the gaps surfaced by an audit of the built MVP + Milestone 1
> against the full vision. **Most have since shipped:** CI (12.1, M4 slice F), `harness doctor`
> (12.2, M4 slice E), headless one-shot (12.3, M3 P2), provider resilience (12.4, M3 P1), and
> thread/checkpoint management (12.5, M2). Still open: deepagents-native skills/memories wiring
> (12.6) and telemetry persistence beyond the M2 ledger row (12.7). Each item below carries its own
> status. Several are bridges to already-planned sections — cross-refs noted.

### 12.1 CI pipeline for the harness repo
> **Status:** ✅ Built (Milestone 4 slice F) — `.github/workflows/ci.yml` runs `host-tests`,
> `image-tests`, `smoke` and `parity` on push/PR. `smoke` now loads the M4.1 AppArmor profile into
> the runner kernel and pins `JAIL_CHECK=1`, so the bwrap jail is a red/green gate rather than an
> environmental skip (`milestone4.1.md` §10.1); the `apparmor-load-probe` measurement job that
> established this is possible has been deleted, its answer recorded there. The
> rationale below is kept as the record of why; the job list there is the plan, the workflow file
> is the truth.

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
> **Status:** ✅ Built (Milestone 4 slice E) — `harness/doctor.py`, reachable as
> `python3 -m harness doctor`. It also grew beyond this section's original scope: the resolved-config
> summary (M5), mask-policy and state-dir placement checks, and the seccomp/AppArmor artifact +
> live bwrap unshare probes (M4/M4.1).

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
> **Status:** ✅ Built (Milestone 3, prereq P2). `cli.run_batch` / `--headless`. **Not yet:** PR URL in
> the JSON (git-pr logs it to stderr), and multi-task file input (single task only so far).

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
> **Status:** ✅ Built (Milestone 3, prereq P1). `harness/resilience.py` + `cli._invoke_resilient`.

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
> Specced in `docs/milestones/complete/milestone2.md` §2.6 (present/past split + `harness threads`/`harness past`
> lifecycle CLI). The design below is the source the milestone pulls from.

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
> `docs/milestones/complete/milestone2.md` §2.7 disambiguates this native `memories/` surface from that milestone's
> bespoke `past.sqlite` archive and defers the wire-or-document decision below to here.

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
> First slice specced in `docs/milestones/complete/milestone2.md` §2.3: a per-session token/cost ledger on the
> `past.sqlite` `sessions` row. The `usage.jsonl` sink below remains the fuller, still-planned form.

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


