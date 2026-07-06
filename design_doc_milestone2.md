# Milestone 2 — Present / Past Memory

> **Status:** ⬜ Planned. Successor to `design_doc_milestone1.md` (cost/token visibility + resource
> caps). Wins over `design_doc.md` for "what we build next" once Milestone 1 has landed. Maps to the
> `design_doc.md` status-matrix §12 rows **"Thread / checkpoint management"** and **"Deepagents-native
> skills & memories wiring"**, and feeds §8 (telemetry) and §12 "Cost / telemetry persistence".

---

## 1. Goal & Definition of Done

A session starts **fresh** and never silently resumes yesterday's conversation, yet nothing is lost:
every session is archived to a **separate, on-demand store** that accumulates across runs and is read
back only when asked.

The harness today fuses two things the LangGraph checkpointer treats as one: **persistence** (state
saved to `checkpoints.sqlite`) and **context injection** (resuming a `thread_id` replays that thread's
whole history into the model context). Because `--thread-id` defaults to the literal `"default"`
(`harness/cli.py`), every start reopens the same thread and the agent behaves "as if it never left."
Milestone 2 splits those two concerns.

Two lanes:
- **Present** — one live checkpointer thread. Auto-loaded into context (normal LangGraph). Fresh id
  per run by default; nameable/resumable on request.
- **Past** — one accumulating archive, in its **own database the checkpointer cannot see**, so it is
  *structurally* impossible to auto-load. Written every session; read only via explicit recall.

Done when:
1. A `run-docker` start with no thread argument opens a **new** present thread (fresh context), not
   the previous conversation. The original "continue default" behavior is opt-in, not default.
2. `--thread-id <name>` (or `DEEPAGENTS_THREAD_ID`) selects a **custom-named** present thread: resumes
   it if it exists, creates it if not.
3. Every session's turns are appended to the **past archive** (`past.sqlite`) as they happen — full
   transcript — plus a per-session summary row written at `session.end`. The archive grows across
   sessions and survives `--rm` runs (rides the workspace mount, like the checkpointer DB).
4. The past is **never auto-injected** into context. It enters context only through recall.
5. Recall works two ways, over one shared query function:
   - `/recall [query]` — a deterministic REPL command; you control exactly when past enters context.
   - `recall_past(query)` — an agent tool the model can call mid-turn when a task needs prior context.
6. The whole feature is **removable** the way the Milestone 1 cost tracker is (§2.5 of that doc):
   delete the archive module + its wiring and default the present thread back to `"default"`, and the
   harness behaves byte-for-byte like Milestone 1. Smoke test passes unchanged either way.

Explicit non-goals (stay deferred): cross-thread *forking* (seeding a new thread from a parent's
checkpoint — the "shared past, then diverge" branch model discussed and rejected for this milestone);
a live *shared present* across two thread names (one shared present = one thread, by definition);
semantic/vector search over the archive (recall is keyword/summary scan first — embeddings are a
later refinement); automatic summarization *compression* of the present thread (that is §7 Headroom,
not this); telemetry-to-PR export of the archive (§8); pruning/GC policy for `past.sqlite` (tracked as
a risk, not built here — see §6).

---

## 2. Design

### 2.1 Present — fresh-by-default threads

Change the `--thread-id` default from the literal `"default"` to a **fresh unique id per run**:

```python
parser.add_argument(
    "--thread-id",
    default=os.getenv("DEEPAGENTS_THREAD_ID") or f"session-{datetime.now():%Y%m%d-%H%M%S}",
    help="Present thread id. New per run unless set; pass a prior id to resume it.",
)
```

- No arg → new thread each start → fresh context. (Def-of-Done 1.)
- `--thread-id my-refactor` → custom named; resumes if the id already has checkpoint state, else
  starts it. (Def-of-Done 2.)
- `DEEPAGENTS_THREAD_ID` still honored, for pinning a thread from an `--env-file`.

`.env.example` currently sets `DEEPAGENTS_THREAD_ID=default`, which would pin the default thread and
defeat fresh-start. Comment it out (leave documented as the resume knob), so the default path is
genuinely fresh.

The checkpointer, `config = {"configurable": {"thread_id": args.thread_id}}`, and
`checkpoints.sqlite` are otherwise unchanged — present is just normal LangGraph on a non-constant id.

### 2.2 Past — the accumulating archive (separate DB)

A second SQLite DB beside the checkpointer, **never opened by LangGraph**:

```
<workspace>/.deepagents/
  checkpoints.sqlite   # PRESENT — SqliteSaver, one live thread (unchanged)
  past.sqlite          # PAST — this milestone; checkpointer never touches it
```

Schema (two tables):

```sql
CREATE TABLE sessions (
  id         TEXT PRIMARY KEY,   -- the present thread_id for that run
  provider   TEXT,
  model      TEXT,
  started    TEXT,               -- ISO 8601
  ended      TEXT,               -- NULL until session.end
  summary    TEXT                -- NULL until session.end (per-session condense)
);
CREATE TABLE turns (
  session_id TEXT REFERENCES sessions(id),
  idx        INTEGER,            -- turn order within the session
  role       TEXT,               -- 'human' | 'ai'
  content    TEXT,
  ts         TEXT,
  PRIMARY KEY (session_id, idx)
);
```

New module `harness/archive.py` owns this DB — schema init, append, summarize, query. It must **not**
import `providers.py` or `cost.py` (keep the dependency graph acyclic, mirroring the
`cost.py`-↛-siblings rule). It takes the resolved model string as a plain argument for the `sessions`
row; it does not reach into the registry.

### 2.3 Write path — accumulate as it happens

- **Per-turn append (crash-safe).** A thin `ArchiveMiddleware` (an `AgentMiddleware`, appended in
  `cli.main` next to the cost tracker) taps each completed turn and writes the human + ai messages to
  `turns` immediately — not only at `session.end` — so a crash or `KeyboardInterrupt` mid-session
  still leaves the archive consistent up to the last finished turn. This mirrors the known partial-
  write caveat already documented for the checkpointer on SIGINT, but per-turn granularity bounds the
  loss to at most the in-flight turn.
- **Session row.** Written at `session.start` (id/provider/model/started); `ended` + `summary` filled
  at `session.end`.
- **Summary.** At `session.end`, condense the session's `turns` into one `summary` string. Default: a
  cheap LLM condense using the already-resolved model. If summarization is unavailable (no turns, or a
  keyless/failed call), fall back to a deterministic non-LLM summary (first human prompt + turn count)
  so the row is never blank and `session.end` never fails the run — same "safe no-op" discipline the
  git-pr workflow uses.

### 2.4 Read path — recall, never automatic

One query function in `archive.py` (`recall(query, *, limit) -> list[Hit]`) backs both surfaces:

- Scans `sessions.summary` first (compact, fast), returns matching sessions ranked by recency.
- Drills into `turns` for a matched session on demand (full transcript slice).
- Keyword/LIKE match for v1; the function signature leaves room for a later embedding backend without
  changing callers.

Two callers:
- **`/recall [query]`** — handled in `run_repl`'s command dispatch alongside `/exit`. Deterministic:
  the operator decides when past enters context. With no query, lists recent sessions (id + summary)
  so the operator can pick. Injects the selected slice as a system/context message into the present
  thread for the next turn.
- **`recall_past(query)`** — a tool added to the agent's tool list so the model can pull prior context
  mid-turn ("what did we decide about X"). Same `recall()` underneath; the tool returns the hits as
  its result, which the agent folds into its reasoning.

Because the past lives in `past.sqlite` and the checkpointer only ever reads `checkpoints.sqlite`,
there is no code path that loads past into context except these two explicit surfaces. That is the
core invariant (Def-of-Done 4).

### 2.5 Removable contract

Like the Milestone 1 cost tracker, this is additive middleware + optional wiring:
- `ArchiveMiddleware` is appended only when archiving is enabled (default on; `DEEPAGENTS_ARCHIVE=0`
  to disable). When absent, no `past.sqlite` is created and nothing taps the turns.
- Delete `harness/archive.py`, drop its `cli.py` wiring, and restore the `--thread-id` default to
  `"default"` → the harness is byte-for-byte Milestone 1. The smoke test must pass in both states.

---

## 3. Config / CLI surface

| Knob | Default | Effect |
|------|---------|--------|
| `--thread-id` / `DEEPAGENTS_THREAD_ID` | fresh `session-<ts>` | Present thread; custom name resumes/creates. |
| `DEEPAGENTS_ARCHIVE` | `1` | `0` disables the past archive entirely (removable contract). |
| `/recall [query]` | — | REPL command: list/inject past on demand. |
| `recall_past(query)` | — | Agent tool: model-initiated recall mid-turn. |

No new run-script flags required; the knobs are env + REPL command, so no image rebuild is needed for
the CLI surface (only the harness package changes → rebuild for code, per the two-stack rule).

---

## 4. Files touched

| File | Change |
|------|--------|
| `deepagent-image/project/harness/archive.py` | **New.** `past.sqlite` schema init, `append_turn`, `start_session`/`end_session`, `summarize`, `recall`. No `providers`/`cost` import. |
| `deepagent-image/project/harness/cli.py` | Fresh `--thread-id` default; build + append `ArchiveMiddleware`; `start_session`/`end_session` around the REPL (beside the existing session.start/end hooks); `/recall` in `run_repl` dispatch. |
| `deepagent-image/project/harness/agent.py` | Register the `recall_past` tool (guarded by `DEEPAGENTS_ARCHIVE`). |
| `deepagent-image/project/.env.example` | Comment out `DEEPAGENTS_THREAD_ID=default`; document it as the resume knob; add `DEEPAGENTS_ARCHIVE`. |
| `deepagent-image/project/tests/test_archive.py` | **New.** Host-runnable (stdlib + `harness.archive` via `_bootstrap._load`): schema, append/query round-trip, fresh-id default, recall never auto-loads, removable=MVP. |
| `deepagent-image/project/tests/test_cli.py` | Fresh-id-by-default; `/recall` dispatch; archive middleware appended only when enabled. |
| `deepagent-image/CLAUDE.md` | Document present/past split, the `.deepagents/past.sqlite` store, `/recall`, `DEEPAGENTS_ARCHIVE`; update the "Conversation state persists…" gotcha. |
| `design_doc.md` §12 / status matrix | Flip "Thread / checkpoint management" and "memories wiring" ⬜ → 🟡/✅ as shipped; point at this doc. |

---

## 5. Build Order

1. `archive.py`: schema init + `append_turn` + `start_session`/`end_session` + `recall`, with
   host-runnable unit coverage (round-trip, recall ranking, empty archive). No LLM yet — `summary`
   filled by the deterministic fallback.
2. Fresh `--thread-id` default in `cli.py` + `.env.example` fix. Verify a no-arg start opens a new
   thread and a named `--thread-id` resumes. (Def-of-Done 1–2; smallest shippable slice.)
3. `ArchiveMiddleware` + conditional append in `cli.main`; `start_session`/`end_session` bracketing
   the REPL. Confirm turns land in `past.sqlite` per-turn and the session row closes at end.
4. `/recall` command in `run_repl` (list + inject). Then the `recall_past` agent tool in `agent.py`
   over the same `recall()`.
5. LLM per-session summary at `session.end`, with the deterministic fallback on keyless/failed calls.
6. Docs + `design_doc.md` matrix; run `verify` / `smoke`; confirm the removable path (archive off,
   `--thread-id default`) reproduces Milestone 1 behavior and the smoke test passes unchanged.

---

## 6. Risks / Open Questions

- **Present partial-write on SIGINT.** Same caveat the checkpointer already documents: a Ctrl-C
  mid-turn can leave the in-flight turn half-persisted. Per-turn archive append bounds archive loss to
  the current turn; the present thread keeps its existing behavior. Acceptable; revisit if flaky.
- **Archive growth / retention.** `past.sqlite` grows unbounded, like `checkpoints.sqlite` today. No
  prune/GC in this milestone — tracked under §12 "Thread / checkpoint management". Summary rows keep
  the *scannable* surface small even as `turns` grows.
- **Summary quality vs. cost.** The `session.end` summarization spends a model call. Keep it cheap
  (short condense) and always fall back to the deterministic summary so a keyless/offline run still
  archives and exits 0. Confirm it doesn't perturb the Milestone 1 session-total cost line
  (summarize *after* the total is printed, or account for it explicitly).
- **`recall_past` context bloat.** A broad query could inject a large transcript slice and blow the
  context window. Cap the injected slice (top-N sessions, truncated turns) in `recall(limit=…)`; the
  agent tool documents the cap in its description.
- **NetJail.** Recall + archive are local SQLite — no egress. The `session.end` summarization reuses
  the already-allowed model endpoint, so no new `netjail/allowed-domains.txt` entry is needed. Verify
  under `-NetJail` that a keyless jailed run still archives via the deterministic-summary fallback.
- **Two-stack purity.** `archive.py` runs in the harness venv (stdlib `sqlite3` only — no new dep) and
  must not import `providers`/`cost`; the summarizer receives the already-resolved chat model from
  `cli.py`, it does not resolve one itself.

---

## 7. Acceptance

- A no-arg `run-docker` start opens a fresh present thread; the prior conversation is **not** in
  context. `--thread-id <name>` resumes/creates a named thread.
- After several sessions, `past.sqlite` has one `sessions` row per run and the full `turns` transcript
  for each; nothing from it appears in a new session's context until recalled.
- `/recall` with no query lists recent sessions; `/recall <query>` injects a matching slice into the
  present turn. `recall_past(<query>)` returns hits when the agent calls it.
- A keyless / offline / `-NetJail` run still archives (deterministic summary) and exits 0.
- **Removing the feature** (delete `archive.py` + wiring, default `--thread-id` back to `"default"`)
  leaves the harness behaving exactly like Milestone 1 — verified by the smoke test passing unchanged.
- `smoke` and `verify` pass; `.ps1` / `.sh` script pairs stay behavior-identical (no run-script
  change expected this milestone).
