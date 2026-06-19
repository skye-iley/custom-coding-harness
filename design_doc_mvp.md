# MVP Design Document: Deep Agents Coding Harness

> **Relationship to `design_doc.md`.** `design_doc.md` is the full target vision (multi-agent,
> FSM-routed, bubblewrap-jailed, Headroom/Caveman-compressed, CLI+TUI, telemetry-to-PR). **This
> document is the MVP**: the smallest coherent, shippable slice. Everything not listed here is
> explicitly out of scope for the MVP and lives in `design_doc.md` as roadmap. When the two
> disagree, this document wins for "what we are building now."

---

## 1. MVP Goal & Definition of "Viable"

**Goal.** Give a developer a one-command way to run a Deep Agents coding agent, with a model
provider of their choice, against a real local codebase, inside a disposable Docker container,
without leaking secrets into the image and without the agent's dependency installs polluting either
the host or the harness runtime.

**The MVP is viable when** a user can:
1. Copy `.env.example` → `.env`, set one provider API key.
2. `build` the image once.
3. `run-docker "<task>"` against any host directory and get the agent's result on stdout.
4. Re-run with the same thread id and have the agent remember the prior conversation.
5. Trust that their `.env` secrets are never written into the image or the workspace.

That is the entire bar. No web UI, no PR automation, no cost dashboard, no classifier.

---

## 2. Non-Goals (Deferred to `design_doc.md`)

Explicitly **not** in the MVP:
- FSM/Qwen complexity classifier and local↔cloud routing (MVP: caller picks the model, or it is
  auto-selected by which API key is set).
- Multi-agent funnel (classifier → orchestrator → worker). MVP runs **one** agent.
- Git session lifecycle: branch / commit / push / PR automation. MVP leaves git to the user.
- Bubblewrap executor jail as a *load-bearing* boundary (see §5 for the MVP's actual boundary).
- `HarnessProfile` dynamic per-agent bind mounts and the path-guard middleware.
- Token/cost tracking, `prices.json`, Headroom/Caveman compression, prompt caching.
- Observability: trace files, metrics files, telemetry-to-PR.
- CLI frontend (Typer/Rich), TUI, HITL `.harness-config.yaml`.
- Resource limits (`--cpus`, `--pids-limit`, memory caps).
- Dual-container split.

---

## 3. Architecture (MVP)

Single Docker image, single agent process, single mounted workspace.

```
host                                  container (deepagent-harness)
─────                                 ─────────────────────────────
project/.env  ──(--env-file)────────▶ env vars (secrets, model choice)
<workspace>/  ──(-v :/project/workspace)─▶ /project/workspace  (read-write, persisted)
~/.gitconfig  ──(-v :ro)────────────▶ /home/agent/.gitconfig   (agent git identity)

                                      main.py
                                        ├─ choose_model() ── PROVIDERS registry
                                        ├─ load_mcp_tools(.mcp.json)
                                        ├─ load_hooks(hooks.json)
                                        ├─ SqliteSaver(<workspace>/.deepagents/checkpoints.sqlite)
                                        └─ create_deep_agent(model, LocalShellBackend, ...)
                                                 │
                                                 ▼
                                        agent runs task in /project/workspace
                                        (workspace-local conda env for the user's code)
```

**Two Python stacks, never mixed:**

| Stack | Location | Purpose |
|-------|----------|---------|
| Harness | `/opt/venv` (uv, first on PATH) | runs `main.py` only |
| Workspace | `<workspace>/.conda/env` (Miniforge) | the agent's project code, tests, installs |

The image is built once and is immutable. The workspace is bind-mounted at run time and is the only
thing that persists.

---

## 4. MVP Feature Set (What Is Built)

These are implemented in `deepagent-image/project/main.py` and the `scripts/` wrappers:

- **Provider-agnostic model selection.** `PROVIDERS` is the single source of truth for
  `choose_model`, credential validation, and chat-model resolution. Native providers
  (openai / anthropic / google_genai / deepseek / ollama) pass through to `init_chat_model`;
  OpenAI-compatible providers (cursor / openrouter / lmstudio) route via `ChatOpenAI` + a
  `*_BASE_URL`. Selection precedence: `--model` → `DEEPAGENTS_MODEL` → first provider in the list
  whose API key is set and whose `default_model` is non-`None`.
- **MCP tools.** `.mcp.json` (Claude/Cursor shape) is loaded via `langchain_mcp_adapters`; transport
  inferred from `command`/`url`. Empty/missing = no extra tools.
- **Lifecycle hooks.** `hooks.json` shell commands fire on session / agent / model / tool events
  (`ShellHooksMiddleware`); session-scoped hooks bracket the whole run.
- **Conversation memory.** `SqliteSaver` checkpoint at `<workspace>/.deepagents/checkpoints.sqlite`,
  keyed by `DEEPAGENTS_THREAD_ID`. Survives `--rm` because it rides the workspace mount. Reuse the
  thread id to resume.
- **Workspace dependency isolation.** Agent code uses a workspace-local conda env
  (`environment.yml` + `run-in-env.sh` seeded into the workspace), separate from the harness venv.
- **Secret hygiene.** Keys live only in `project/.env`, passed via `--env-file` at run time. `.env`
  is gitignored and never `COPY`ed into the image.
- **Workspace boundary check.** `resolve_workspace` rejects an `AGENT_WORKSPACE` outside `/project`
  when running in-container (detected via the `DEEPAGENTS_IN_CONTAINER=1` marker baked by the
  Dockerfile).

---

## 5. Security Posture (MVP) — read carefully

**The trust boundary in the MVP is the Docker container, not bubblewrap.**

`scripts/sandbox-exec.sh` and `bwrap` are installed in the image, but the agent's shell tool runs
through `LocalShellBackend` directly — **commands are not routed through `sandbox-exec` and the
network is not isolated per-command.** Treat the agent as having full read/write to the mounted
workspace and outbound network from inside the container.

MVP mitigations that *are* in force:
- Runs as `USER agent` (uid 10001), not root.
- `.env` secrets never baked into the image.
- `~/.ssh` is **never** mounted (only `~/.gitconfig`, read-only). No host Docker socket mount.
- Container is `--rm`; only the explicitly mounted workspace persists.

MVP user contract (must be documented to the user):
- **Only mount a workspace you are willing to let an autonomous agent fully modify.**
- The container can reach the network; do not run untrusted tasks with sensitive credentials in the
  environment beyond the single model key you need.

Hardening the executor (wire `sandbox-exec` into the shell backend, two-phase network isolation,
resource limits, path guard) is **post-MVP** and tracked in `design_doc.md` §2/§10.

---

## 6. User Workflow (MVP)

PowerShell primary on this machine; `.sh` equivalents exist.

```powershell
# one-time
copy project\.env.example project\.env      # then set ONE provider key
.\scripts\build.ps1                          # docker build -t deepagent-harness
.\scripts\verify.ps1                         # confirms harness venv + conda import OK

# per task
.\scripts\run-docker.ps1 "summarize this repo and list its entry points"
.\scripts\run-docker.ps1 -WorkspacePath C:\path\to\repo "add a test for foo()"
```

`run-docker.ps1` refuses to start without `project\.env`, bind-mounts the workspace to
`/project/workspace`, seeds a missing `environment.yml` / `.gitignore` / `run-in-env.sh`, and mounts
`~/.gitconfig` read-only if present.

---

## 7. Configuration Surface (MVP)

| Input | Where | Required? |
|-------|-------|-----------|
| Provider API key(s) | `project/.env` | At least one |
| `DEEPAGENTS_MODEL` | `.env` or `--model` | Optional (else auto by key) |
| `*_BASE_URL` | `.env` | Only for cursor/openrouter/lmstudio |
| Task | CLI arg or `DEEPAGENTS_TASK` | Optional (falls back to default task) |
| `DEEPAGENTS_THREAD_ID` | `.env` | Optional (`default`); reuse to resume memory |
| `AGENT_WORKSPACE` | `.env` | Fixed to `/project/workspace` for the standard mount |
| MCP servers | `project/.mcp.json` | Optional |
| Lifecycle hooks | `project/hooks.json` | Optional |
| Project instructions | `AGENTS.md` (container CWD) | Optional; appended to system prompt |

---

## 8. Acceptance / Test Plan (MVP)

The MVP ships when these pass (the first two already exist as `verify` / `smoke`):

1. **Harness sanity** (`verify`): `deepagents`, `langgraph`, `langchain_openai` import in the image;
   conda CLI present.
2. **Smoke run** (`smoke`): container starts and `main.py --help` / a trivial task returns 0.
3. **End-to-end task**: with one real key set, `run-docker "<task>"` produces the agent's final
   message on stdout and exit 0.
4. **Memory resume**: two runs sharing a `--thread-id` show the second run aware of the first.
5. **Secret hygiene**: `docker history` / image inspection shows no `.env` contents; `.env` is
   gitignored.
6. **Workspace isolation**: agent-installed packages land in `<workspace>/.conda`, not `/opt/venv`.

---

## 9. Known Limitations (MVP)

- No in-container command sandbox beyond the Docker boundary (see §5).
- No cost/token visibility; the user pays provider rates blind until §6/§7 of the full design lands.
- No git automation; the user reviews and commits agent changes manually.
- Single agent, single task per invocation; no parallelism or peer review.
- No resource limits — a runaway agent can consume host CPU/memory up to Docker defaults.

These are acceptable for an MVP whose purpose is to validate the core loop (provider routing →
sandboxed-by-container agent run → persisted workspace + memory). Each maps to a tracked section in
`design_doc.md`.
