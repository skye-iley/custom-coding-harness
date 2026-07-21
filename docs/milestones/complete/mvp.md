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
3. `run-docker` against any host directory to open a **persistent interactive session**: the
   container starts once, the agent answers, and a prompt stays open for the next message —
   multi-turn, with the agent reading/editing the workspace, without restarting the container
   between turns.
4. End the session deterministically with an explicit exit command (e.g. `/exit`); within a
   session the agent remembers earlier turns, and re-opening with the same thread id resumes prior
   state across sessions.
5. Trust that their `.env` secrets are never written into the image or the workspace.

That is the entire bar. No web UI, no PR automation, no cost dashboard, no classifier, and no
host-side `harness` CLI/TUI (the interactive loop runs *inside* the container — see §1a).

---

## 1a. True-MVP Increment: Interactive Multi-Turn Session

The first MVP ran **one task per container start** (`run-docker "<task>"` → answer → exit). The
"true MVP" keeps everything above and makes the session **interactive and persistent**: the
container comes up once and the user converses with the agent over many turns until they explicitly
close it. This is the in-container conversation loop only — **not** the host-side Typer/Rich
`harness` CLI/TUI of `design_doc.md` §9, which stays out of scope.

### New requirements

| # | Requirement | Design decision (MVP) |
|---|-------------|-----------------------|
| 1 | Container stays open until the user explicitly closes it | The harness runs a REPL loop; container lifetime = loop lifetime. `--rm` still applies, so the container is removed *after* the user exits, not after one answer. |
| 2 | Multi-turn conversation without closing/reopening between turns | One agent built once; each turn is another `invoke` on the **same `thread_id`** in the same process — no per-turn container restart, no re-resolving the model. In-session history is carried by the live agent/process; the SqliteSaver checkpointer is what *persists* it for cross-session resume (next row), not what makes in-session memory work. |
| 3 | Read and edit files in `project/workspace` | Unchanged from the MVP — `LocalShellBackend` rooted at the bind-mounted workspace already gives read/write. Listed here because it must keep working across every turn of a session. |
| 4 | Keep a CLI input line open for the next prompt after each response, until closed | After printing the answer the loop blocks on a prompt (`input()`), reads the next message, and repeats. Requires an interactive TTY (`docker run -it`). |
| 5 | A specific command that ends the session **deterministically, with no LLM interpreting it** | Exit lines (`/exit`, `/quit`) are matched in Python *before* the text is sent to the agent, so quitting never depends on the model choosing to call a tool. EOF (Ctrl-D) ends the session. Ctrl-C is two-stage: during a turn it cancels that turn and returns to the prompt; at an idle prompt (or a second consecutive Ctrl-C) it ends the session. |
| 6 | Print stage output across the run | The harness prints lifecycle markers distinct from agent output — e.g. `container loading`, `building agent`, `thinking`, `reading prompt`, `session closed` — so the user can see where the loop is. |

### Decisions & defaults (open to change)

- **Exit tokens:** `/exit` and `/quit` (slash-prefixed so they cannot collide with a genuine
  instruction the user wants to send to the agent). Matched in Python, never by the model —
  satisfies requirement 5.
- **Interrupt (Ctrl-C):** two-stage. A `KeyboardInterrupt` raised *during* a turn's `invoke` is
  caught, the turn is abandoned, and the loop returns to the prompt with the session intact. A
  Ctrl-C at an idle prompt — or a second consecutive Ctrl-C — ends the session like EOF. Stops one
  fat-fingered Ctrl-C from killing a long session.
- **Initial task still allowed:** `run-docker "<task>"` runs that task as the first turn, then drops
  to the interactive prompt. With no task it goes straight to the prompt.
- **Non-interactive fallback (CI / smoke / piped stdin):** when stdin is not a TTY, the loop runs the
  initial task (or the default inspection task) for exactly one turn, then exits on EOF — so
  automation and the existing `smoke`/`verify` flows keep working unchanged.
- **Stage markers** are written with a distinct prefix (e.g. `[harness] …`) so they are easy to tell
  apart from the agent's reply and easy to grep/suppress later.
- **Session vs. cross-session memory:** in-session history lives in the running process + the
  checkpointer; reusing `DEEPAGENTS_THREAD_ID` across separate sessions still resumes prior state
  from the on-disk SqliteSaver, exactly as before.

### Touch points (for planning — code lands in a later change)

- `project/harness/cli.py` — replace the single `invoke` in `main()` with the REPL loop
  (`run_repl` / `run_turn` helpers), the deterministic exit check, two-stage Ctrl-C handling
  (cancel turn vs. end session), and stage prints.
- `scripts/run-docker.ps1` / `scripts/run-docker.sh` — add `-it` so stdin/TTY is available for the
  prompt loop (keep `--rm`, the `.env` guard, and the optional task arg; keep the `.ps1`/`.sh` pair
  in sync).
- Docs: ✅ done — the run sections of `deepagent-image/CLAUDE.md` and root `CLAUDE.md` now describe
  the persistent multi-turn REPL (`docker run -it`, `/exit`/`/quit`, single-turn non-TTY fallback).

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
- Host-side CLI frontend (Typer/Rich), TUI, HITL `.harness-config.yaml`. (The MVP's interactivity is
  the in-container REPL of §1a — a prompt loop inside the running container, not a host `harness`
  command wrapping `docker exec`.)
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
                                        REPL loop (run_repl), container stays up:
                                          read prompt ─▶ invoke agent (same thread_id)
                                          ─▶ print answer ─▶ repeat until /exit | EOF
                                        each turn runs in /project/workspace
                                        (workspace-local conda env for the user's code)
```

The agent is built **once** and reused for every turn; `/exit` (matched in Python, no model
involvement) ends the loop, after which the `--rm` container is removed. A non-TTY stdin collapses
the loop to a single turn (see §1a).

**Two Python stacks, never mixed:**

| Stack | Location | Purpose |
|-------|----------|---------|
| Harness | `/opt/venv` (uv, first on PATH) | runs `main.py` only |
| Workspace | `<workspace>/.conda/env` (Miniforge) | the agent's project code, tests, installs |

The image is built once and is immutable. The workspace is bind-mounted at run time and is the only
thing that persists.

---

## 4. MVP Feature Set (What Is Built)

These are implemented in the `deepagent-image/project/harness/` package (entry shim `main.py`) and
the `scripts/` wrappers:

- **Interactive multi-turn session (§1a).** The harness runs a REPL loop: the agent is built once,
  then a prompt → `invoke` (same `thread_id`) → answer cycle repeats until the user exits. The
  container stays up for the whole session (`docker run -it`, still `--rm` on exit). A non-TTY stdin
  degrades to a single turn for CI/smoke.
- **Deterministic exit + stage output (§1a).** `/exit` and `/quit` end the session in Python without
  the model interpreting them; lifecycle stage markers (`container loading`, `building agent`,
  `thinking`, `reading prompt`, `session closed`) are printed distinctly from agent replies.
- **Provider-agnostic model selection.** `PROVIDERS` is the single source of truth for
  `choose_model`, credential validation, and chat-model resolution. It is **loaded at import time
  from the on-disk `project/providers/` TOML registry** (`<provider>/provider.toml` +
  `models/<model>.toml`), not hard-coded — add/change a provider or model by editing TOML, no Python
  edit (`DEEPAGENTS_PROVIDERS_DIR` overrides the path for tests). Native providers
  (openai / anthropic / google_genai / deepseek / ollama) pass through to `init_chat_model`;
  OpenAI-compatible providers (cursor / openrouter / lmstudio) route via `ChatOpenAI` + a
  `*_BASE_URL`. Selection precedence: `--model` → `DEEPAGENTS_MODEL` → first provider by ascending
  `priority` whose API key is set and whose `default_model` is non-`None`. The dev-time
  `sync-models` command (`python3 -m harness sync-models`; `scripts/sync-models.{sh,ps1}`)
  regenerates `models/*.toml` from each provider's live list-models endpoint — it needs keys +
  network (the sealed runtime has neither) and never rewrites `provider.toml`.
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

# per session (interactive: container stays open, multi-turn, until you exit)
.\scripts\run-docker.ps1                                  # opens straight to the prompt
.\scripts\run-docker.ps1 "summarize this repo"           # runs that first, then prompts
.\scripts\run-docker.ps1 -WorkspacePath C:\path\to\repo "add a test for foo()"
# then type follow-ups at the  you>  prompt; type /exit (or /quit) to close the container
```

`run-docker.ps1` refuses to start without `project\.env`, runs the container interactively
(`docker run -it --rm`) so the prompt loop has a TTY, bind-mounts the workspace to
`/project/workspace`, seeds a missing `environment.yml` / `.gitignore` / `run-in-env.sh`, and mounts
`~/.gitconfig` read-only if present.

---

## 7. Configuration Surface (MVP)

| Input | Where | Required? |
|-------|-------|-----------|
| Provider API key(s) | `project/.env` | At least one |
| `DEEPAGENTS_MODEL` | `.env` or `--model` | Optional (else auto by key) |
| `*_BASE_URL` | `.env` | Only for cursor/openrouter/lmstudio |
| Task (first turn) | CLI arg or `DEEPAGENTS_TASK` | Optional; runs as turn 1 then prompts. Empty + TTY → straight to prompt; empty + no TTY → default inspection task |
| Exit command | typed at the `you>` prompt | `/exit` or `/quit` — matched in Python, no LLM; EOF/Ctrl-C also end the session |
| `DEEPAGENTS_THREAD_ID` | `.env` | Optional (`default`); reuse to resume memory across sessions |
| `AGENT_WORKSPACE` | `.env` | Fixed to `/project/workspace` for the standard mount |
| Provider/model registry | `project/providers/` (TOML) | Built-in; edit to add providers/models. `DEEPAGENTS_PROVIDERS_DIR` overrides path (tests) |
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
7. **Interactive multi-turn**: in one `run-docker` (`-it`) session, send two related prompts without
   restarting; the second answer reflects the first turn, and the container stays up between them.
8. **Deterministic exit**: typing `/exit` ends the session and the container exits 0 with no
   model/tool call interpreting the command; EOF (Ctrl-D) does the same. Ctrl-C *during* a turn
   cancels that turn and returns to the prompt (session survives); Ctrl-C at the idle prompt (or a
   second consecutive Ctrl-C) ends the session.
9. **Stage output**: the run prints the lifecycle markers (container loading → building agent →
   thinking → reading prompt → session closed) distinctly from the agent's replies.
10. **Non-interactive fallback**: piping a task with no TTY runs exactly one turn and exits 0, so
    `smoke`/CI behavior is unchanged.

---

## 9. Known Limitations (MVP)

- No in-container command sandbox beyond the Docker boundary (see §5).
- No cost/token visibility; the user pays provider rates blind until §6/§7 of the full design lands.
- No git automation; the user reviews and commits agent changes manually.
- Single agent; many turns per session but no parallelism or peer review.
- Interactivity is a single-user, single-agent in-container REPL — no host-side `harness` CLI/TUI,
  no concurrent sessions, no live cost/status panel (all `design_doc.md` §9).
- The prompt loop needs a TTY (`-it`); without one it degrades to a single non-interactive turn.
  TTY detection is host-dependent on Windows (native PowerShell vs. Git-Bash/MSYS vs. piped stdin),
  so the interactive path must be verified on the PowerShell host, not only via a Bash shell.
- No token streaming: each turn blocks on a single `invoke` and the whole answer prints at once
  after the `thinking` marker. Incremental/streamed output is a full-design goal (`design_doc.md`
  §9), not in the MVP.
- No per-session turn or token ceiling: a persistent REPL can run — and spend provider credits —
  indefinitely until the user exits (compounds the no-resource-limits note below).
- No resource limits — a runaway agent can consume host CPU/memory up to Docker defaults.

These are acceptable for an MVP whose purpose is to validate the core loop (provider routing →
sandboxed-by-container agent run → persisted workspace + memory). Each maps to a tracked section in
`design_doc.md`.
