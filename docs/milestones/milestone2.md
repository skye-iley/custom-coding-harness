# Milestone 2 — Present / Past Memory

> **Status:** ✅ Built (`feat/milestone-2-memory`). Successor to `docs/milestones/milestone1.md` (cost/token visibility + resource
> caps). Wins over `design_doc.md` for "what we build next" once Milestone 1 has landed. Lands the
> `design_doc.md` status-matrix §12 rows **"Thread / checkpoint management"** (§12.5, via the §2.6
> lifecycle commands) and a first slice of **"Cost / telemetry persistence"** (§12.7, via the per-session
> ledger on the archive), disambiguates **"Deepagents-native skills & memories wiring"** (§12.6, §2.7),
> and feeds §8 (telemetry-to-PR).

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
   Recall can be **scoped to a continual topic** (`--topic`/`DEEPAGENTS_TOPIC`/`/topic` label): a
   tagged session's recall defaults to its own topic lane, `--all` widens to the whole archive, and an
   untagged session recalls globally. The label is explicit (not inferred); auto-clustering is deferred.
6. Each session's `sessions` row records the run's token/cost totals (off the Milestone 1 accumulator),
   making `past.sqlite` an on-disk spend ledger; a resumed named thread yields one row **per run**
   (unique `run_id`), and recalled context is never re-archived.
7. Both stores are inspectable and prunable from a keyless CLI: `harness threads …` (present /
   `checkpoints.sqlite`) and `harness past …` (archive / `past.sqlite`), with confirm-guarded delete.
8. The past archive is documented as distinct from deepagents' native `memories/`; no dead scaffolding
   is left implying M2 wired the latter.
9. The whole feature is **removable** the way the Milestone 1 cost tracker is (§2.5 of that doc):
   delete the archive module (+ `memadmin.py`) + its wiring and default the present thread back to
   `"default"`, and the harness behaves byte-for-byte like Milestone 1. Smoke test passes unchanged either way.

Explicit non-goals (stay deferred): cross-thread *forking* (seeding a new thread from a parent's
checkpoint — the "shared past, then diverge" branch model discussed and rejected for this milestone);
a live *shared present* across two thread names (one shared present = one thread, by definition);
semantic/vector search over the archive **and** automatic *derivation* of continual topics (recall is
keyword/summary scan first, and topics are explicit operator/env labels — §2.2/§2.4; embeddings and
auto-clustering are a later refinement that slots behind the same `recall(topic=…)` seam); automatic
summarization *compression* of the present thread (that is §7 Headroom,
not this); telemetry-to-PR export of the archive (§8, though this milestone now persists the per-session
cost ledger that feeds it — §2.3); **automatic/policy-based** GC for `past.sqlite` (a *manual* prune
command **is** in scope — §2.6 — but retention enforced without an operator command is deferred).

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
  run_id     TEXT PRIMARY KEY,   -- unique per run: `run-<ts>` — NOT the thread_id (see below)
  thread_id  TEXT,               -- the present thread_id for that run (repeats across resumes)
  topic      TEXT,               -- optional "continual topic" label; NULL = untagged / global pool
  provider   TEXT,
  model      TEXT,
  started    TEXT,               -- ISO 8601
  ended      TEXT,               -- NULL until session.end
  summary    TEXT,               -- NULL until session.end (per-session condense)
  input_tokens    INTEGER,       -- session totals, filled at session.end from the M1 cost accumulator
  output_tokens   INTEGER,
  cost_usd        REAL,          -- NULL when keyless / estimate unavailable
  cost_provenance TEXT           -- 'official' | 'estimate' | 'reported' | NULL
);
CREATE INDEX idx_sessions_thread ON sessions(thread_id);
CREATE INDEX idx_sessions_topic  ON sessions(topic);
CREATE TABLE turns (
  run_id     TEXT REFERENCES sessions(run_id),
  idx        INTEGER,            -- turn order within the run
  role       TEXT,               -- 'human' | 'ai'
  content    TEXT,
  ts         TEXT,
  PRIMARY KEY (run_id, idx)
);
```

**Why `run_id`, not `thread_id`, is the PK (gap fix).** A named present thread is *resumable*
(Def-of-Done 2): `--thread-id my-refactor` can run Monday and Tuesday. Keying `sessions` on the
`thread_id` collides on the second `start_session`, and `turns(session_id, idx)` collides when the
resumed thread restarts `idx`. So each run mints a fresh `run_id` (`run-<ts>`); `thread_id` is kept as
an indexed column so recall can still group a named thread's runs. One run = one `sessions` row, always.

**Two grouping keys — `thread_id` vs. `topic`.** `thread_id` groups *runs of the same present thread*
(a resumed `--thread-id auth-refactor`). `topic` is a coarser, **decoupled** label — a "continual
topic" that spans *different* threads/sessions: a run can carry a throwaway `session-<ts>` thread id yet
be tagged `topic="auth"`, so its archive lands in the same lane as every other `auth` run. `topic` is
optional (NULL = untagged, part of the flat global pool, exactly today's behavior); it is an explicit
operator/env label, **not** an inferred cluster — semantic auto-grouping of sessions into topics stays
the deferred embedding work (see non-goals). `topic` is the manual seam a later embedding backend slots
behind without changing callers.

New module `harness/archive.py` owns this DB — schema init, append, summarize, query. It must **not**
import `providers.py` or `cost.py` (keep the dependency graph acyclic, mirroring the
`cost.py`-↛-siblings rule). It takes the resolved model string as a plain argument for the `sessions`
row; it does not reach into the registry. The session token/cost totals are likewise passed in from
`cli.py` at `session.end` (plain numbers off the M1 accumulator) — `archive.py` never imports `cost.py`.

### 2.3 Write path — accumulate as it happens

- **Per-turn append (crash-safe).** A thin `ArchiveMiddleware` (an `AgentMiddleware`, appended in
  `cli.main` next to the cost tracker) taps each completed turn and writes the human + ai messages to
  `turns` immediately — not only at `session.end` — so a crash or `KeyboardInterrupt` mid-session
  still leaves the archive consistent up to the last finished turn. This mirrors the known partial-
  write caveat already documented for the checkpointer on SIGINT, but per-turn granularity bounds the
  loss to at most the in-flight turn.
- **Session row.** Written at `session.start` (`run_id`/`thread_id`/provider/model/started); `ended`,
  `summary`, and the token/cost totals filled at `session.end`.
- **No re-archiving of recalled context (gap fix).** `/recall` and `recall_past` inject a past slice
  into the present thread (§2.4). That injected message is **marked** (a sentinel role/metadata tag),
  and the tap skips any message carrying the mark — otherwise every recall rewrites old turns back into
  `past.sqlite`, compounding on each recall. The tap archives only genuine human/ai turns of the run.
- **Session token/cost totals.** At `session.end`, read the per-session totals off the Milestone 1
  cost accumulator (input/output tokens, cost, provenance) and write them into the `sessions` row —
  the same end hook that writes `summary`, run **after** the M1 session-total line prints so the row
  matches stderr. Keyless/estimate-unavailable runs leave `cost_usd` NULL. This makes `past.sqlite` the
  on-disk spend ledger §8 / design_doc.md §12.7 want, for near-zero extra code (row + hook already exist).
- **Summary.** At `session.end`, condense the session's `turns` into one `summary` string. Default: a
  cheap LLM condense using the already-resolved model. If summarization is unavailable (no turns, or a
  keyless/failed call), fall back to a deterministic non-LLM summary (first human prompt + turn count)
  so the row is never blank and `session.end` never fails the run — same "safe no-op" discipline the
  git-pr workflow uses.

### 2.4 Read path — recall, never automatic

One query function in `archive.py` (`recall(query, *, topic=None, limit) -> list[Hit]`) backs both
surfaces:

- Scans `sessions.summary` first (compact, fast), returns matching sessions ranked by recency.
- **Topic scope.** `topic=None` scans the whole archive (today's behavior); `topic="auth"` filters to
  that lane before matching (uses `idx_sessions_topic`). Scoping only *narrows* — it adds no auto-load
  path, so the core "never auto-injected" invariant (Def-of-Done 4) is untouched.
- Drills into `turns` for a matched session on demand (full transcript slice).
- Keyword/LIKE match for v1; the signature (`topic` + `query`) leaves room for a later embedding backend
  — including auto-derived topics — without changing callers.

**Setting the topic.** `--topic <name>` / `DEEPAGENTS_TOPIC` stamps the run's `sessions` row at
`session.start`; `/topic <name>` in the REPL sets/switches it mid-session (writes forward — earlier
rows keep their label). Unset → NULL → global pool.

**Default recall scope.** When the current session **has** a topic, both surfaces default to scoping
recall to that topic (the point of tagging one); `--all` (or `topic=None` explicitly) widens to the
whole archive. When the session is untagged, recall is global, exactly as before.

Two callers:
- **`/recall [query] [--all]`** — handled in `run_repl`'s command dispatch alongside `/exit`.
  Deterministic: the operator decides when past enters context. With no query, lists recent sessions
  (run id + topic + summary) so the operator can pick. Injects the selected slice as a system/context
  message into the present thread for the next turn (marked so the tap never re-archives it, §2.3).
- **`recall_past(query, topic=None)`** — a tool added to the agent's tool list so the model can pull
  prior context mid-turn ("what did we decide about X"). Same `recall()` underneath (defaults to the
  session's topic; the model may pass a topic or `None` to widen); returns the hits as its result.

Because the past lives in `past.sqlite` and the checkpointer only ever reads `checkpoints.sqlite`,
there is no code path that loads past into context except these two explicit surfaces. That is the
core invariant (Def-of-Done 4).

### 2.5 Removable contract

Like the Milestone 1 cost tracker, this is additive middleware + optional wiring:
- `ArchiveMiddleware` is appended only when archiving is enabled (default on; `DEEPAGENTS_ARCHIVE=0`
  to disable). When absent, no `past.sqlite` is created and nothing taps the turns.
- Delete `harness/archive.py`, drop its `cli.py` wiring, and restore the `--thread-id` default to
  `"default"` → the harness is byte-for-byte Milestone 1. The smoke test must pass in both states.

The §2.6 lifecycle subcommands are keyless read/delete tools over the DBs; deleting `memadmin.py`
with `archive.py` restores the byte-for-byte-M1 contract too (the `threads` half then simply has no
`past.sqlite` to inspect — `checkpoints.sqlite` is M1's own).

### 2.6 Memory lifecycle — inspect & prune both stores

Fresh-per-run threads (2.1) make `checkpoints.sqlite` grow **faster**: every no-arg start mints a new
`session-<ts>` present thread. And `past.sqlite` grows unbounded by design (§6). Shipping both stores
with zero lifecycle tools is an awkward state — 2.1 accelerates the exact growth design_doc.md §12.5
was written to manage. Pull the management surface into this milestone, next to the stores that need it.

One keyless subcommand group over the local sqlite DBs (mirrors `sync-models`; **no run-path change**),
confirm-guarded on every delete per the irreversible-action rule (`--yes` to skip the prompt):

- `harness threads list | show <id> | rm <id> | prune --older-than <N>d` — over `checkpoints.sqlite`
  (present). Ids + turn count + last-modified; delete / bulk-prune orphan `session-<ts>` threads.
  (= design_doc.md §12.5, pulled forward.)
- `harness past list | show <run> | rm <run> | prune --older-than <N>d` — over `past.sqlite` (archive),
  all accepting an optional `--topic <name>` filter (list/prune a single continual-topic lane).
  Symmetric surface; `show` prints a session's `topic` + `summary` + turn slice. `harness past topics`
  lists distinct topics with run counts. Answers the §6 retention risk with an operator tool instead of
  only an unbounded-growth caveat.

Both are inspection + explicit delete only — neither can inject into a live session (that stays the sole
job of `/recall` / `recall_past`). New `harness/memadmin.py` (queries both sqlite schemas directly, no
run-path import), `cli.dispatch` wiring, `test_memadmin.py` against tmp DBs. Automatic/policy-based GC
(retention windows enforced without a command) stays deferred — this is the manual surface only.

### 2.7 Past archive vs. deepagents-native `memories/` — disambiguate

This milestone's header maps to the status-matrix row "Deepagents-native skills & memories wiring," but
the two are **not** the same store and must not be conflated:
- **This milestone's past** = a bespoke `past.sqlite` transcript archive, read only via explicit recall.
- **Deepagents `memories/`** = the framework's own memory surface, baked into the image at
  `project/memories/` but currently **empty + unread** (design_doc.md §12.6).

Shipping M2 silent on this leaves two "memory" concepts plus dead scaffolding. Minimum for this
milestone: state the distinction in `deepagent-image/CLAUDE.md`, and **either** (a) wire the per-session
`summary` into a deepagents memory entry so recall and native memory share one source, **or** (b) leave
`memories/` explicitly out of scope and defer to §12.6 for the wiring decision. Prefer (b) unless (a) is
cheap; either way, no silent dead dir is left implying M2 filled it.

---

## 3. Config / CLI surface

| Knob | Default | Effect |
|------|---------|--------|
| `--thread-id` / `DEEPAGENTS_THREAD_ID` | fresh `session-<ts>` | Present thread; custom name resumes/creates. |
| `--topic` / `DEEPAGENTS_TOPIC` | none (NULL) | Continual-topic label for the run; scopes recall by default. |
| `DEEPAGENTS_ARCHIVE` | `1` | `0` disables the past archive entirely (removable contract). |
| `/recall [query] [--all]` | — | REPL command: list/inject past on demand; scoped to session topic unless `--all`. |
| `/topic [name]` | — | REPL command: set/switch the session's continual topic (no arg = show current). |
| `recall_past(query, topic=None)` | — | Agent tool: model-initiated recall mid-turn; defaults to session topic. |
| `harness threads …` | — | Keyless: list/show/rm/prune present threads (`checkpoints.sqlite`). §2.6. |
| `harness past …` | — | Keyless: list/show/rm/prune archive runs (`past.sqlite`). §2.6. |

No new run-script flags required; the knobs are env + REPL command, so no image rebuild is needed for
the CLI surface (only the harness package changes → rebuild for code, per the two-stack rule).

---

## 4. Files touched

| File | Change |
|------|--------|
| `deepagent-image/project/harness/archive.py` | **New.** `past.sqlite` schema init (per-run `run_id` PK + `thread_id`/`topic` indexes + token/cost columns), `append_turn` (skips recall-marked msgs), `start_session`/`end_session` (end writes summary + cost totals), `summarize`, `recall(query, topic=…)`. No `providers`/`cost` import. |
| `deepagent-image/project/harness/memadmin.py` | **New.** Keyless `threads`/`past` subcommands (list/show/rm/prune, `past` with `--topic` filter + `past topics`) over both sqlite DBs; confirm-guarded delete. No run-path import. §2.6. |
| `deepagent-image/project/harness/cli.py` | Fresh `--thread-id` default; mint per-run `run_id`; `--topic`/`DEEPAGENTS_TOPIC` stamp; build + append `ArchiveMiddleware`; `start_session`/`end_session` around the REPL (end passes M1 cost totals); `/recall` + `/topic` in `run_repl` dispatch; `threads`/`past` in `dispatch`. |
| `deepagent-image/project/harness/agent.py` | Register the `recall_past` tool (guarded by `DEEPAGENTS_ARCHIVE`). |
| `deepagent-image/project/.env.example` | Comment out `DEEPAGENTS_THREAD_ID=default`; document it as the resume knob; add `DEEPAGENTS_ARCHIVE` and `DEEPAGENTS_TOPIC` (documented, unset by default). |
| `deepagent-image/project/tests/test_archive.py` | **New.** Host-runnable (stdlib + `harness.archive` via `_bootstrap._load`): schema, append/query round-trip, fresh-id default, recall never auto-loads, resumed named thread → second row (no PK collision), recall-marked msg not re-archived, cost totals land on the row, topic-scoped recall returns only that lane while untagged recall stays global, removable=MVP. |
| `deepagent-image/project/tests/test_memadmin.py` | **New.** Host-runnable: `threads`/`past` list/show/rm/prune over tmp sqlite; delete guarded without `--yes`; prune targets only matched rows. |
| `deepagent-image/project/tests/test_cli.py` | Fresh-id-by-default; `/recall` + `/topic` dispatch; `--topic`/`DEEPAGENTS_TOPIC` stamps the row; archive middleware appended only when enabled; `threads`/`past` dispatch. |
| `deepagent-image/CLAUDE.md` | Document present/past split, the `.deepagents/past.sqlite` store, `/recall`, `/topic` + `DEEPAGENTS_TOPIC` continual-topic scoping, `DEEPAGENTS_ARCHIVE`, the `threads`/`past` lifecycle commands, and the past-vs-deepagents-`memories/` distinction (§2.7); update the "Conversation state persists…" gotcha. |
| `design_doc.md` §12 / status matrix | Flip "Thread / checkpoint management" and "memories wiring" ⬜ → 🟡/✅ as shipped; point at this doc. |

---

## 5. Build Order

1. `archive.py`: schema init (per-run `run_id` PK, `thread_id` index, cost columns) + `append_turn` +
   `start_session`/`end_session` + `recall`, with host-runnable unit coverage (round-trip, recall
   ranking, empty archive, resumed thread → distinct `run_id`). No LLM yet — `summary` filled by the
   deterministic fallback; cost columns NULL for now.
2. Fresh `--thread-id` default in `cli.py` + `.env.example` fix. Verify a no-arg start opens a new
   thread and a named `--thread-id` resumes. (Def-of-Done 1–2; smallest shippable slice.)
3. `ArchiveMiddleware` + conditional append in `cli.main`; `start_session`/`end_session` bracketing
   the REPL. Confirm turns land in `past.sqlite` per-turn and the session row closes at end.
4. `/recall` command in `run_repl` (list + inject, marking injected context). Then the `recall_past`
   agent tool in `agent.py` over the same `recall()`. Verify the tap skips the recall-marked message so
   recalled context is not re-archived.
5. Continual-topic scoping: `--topic`/`DEEPAGENTS_TOPIC` + `/topic` stamp the `sessions.topic`;
   `recall(query, topic=…)` filters; default-scope to the session's topic with `--all` to widen.
   Verify a topic-scoped recall returns only that lane and an untagged run stays global.
6. LLM per-session summary at `session.end`, with the deterministic fallback on keyless/failed calls.
7. Session token/cost totals into the `sessions` row at `session.end` (off the M1 accumulator, after
   the session-total line prints). Verify the row matches stderr; NULL cost on a keyless run.
8. `harness/memadmin.py`: `threads` + `past` subcommands (list/show/rm/prune, `past` with `--topic` +
   `past topics`), keyless, confirm-guarded, wired through `cli.dispatch`, with `test_memadmin.py`. (§2.6.)
9. Disambiguation: `deepagent-image/CLAUDE.md` states past-vs-deepagents-`memories/` and the (a)/(b)
   decision (§2.7).
10. Docs + `design_doc.md` matrix; run `verify` / `smoke`; confirm the removable path (archive off,
   `--thread-id default`) reproduces Milestone 1 behavior and the smoke test passes unchanged.

---

## 6. Risks / Open Questions

- **Present partial-write on SIGINT.** Same caveat the checkpointer already documents: a Ctrl-C
  mid-turn can leave the in-flight turn half-persisted. Per-turn archive append bounds archive loss to
  the current turn; the present thread keeps its existing behavior. Acceptable; revisit if flaky.
- **Archive growth / retention.** `past.sqlite` grows unbounded, and fresh-per-run threads (2.1) make
  `checkpoints.sqlite` grow *faster*. This milestone now ships a **manual** lifecycle surface (§2.6
  `threads`/`past` list/rm/prune) instead of only flagging the risk; automatic/policy GC stays deferred.
  Summary rows keep the *scannable* surface small even as `turns` grows.
- **Cost-total consistency.** The `sessions` cost columns must match the M1 stderr session-total — write
  them from the same accumulator, *after* the total line prints, so the ledger can't diverge. A keyless
  run leaves `cost_usd` NULL (never a wrong number). Confirm the summarizer's own model call (if it runs)
  is accounted the same way it is for the M1 total, so it isn't silently omitted from the row.
- **Recall-marker reliability.** The no-re-archive guarantee (§2.3) depends on the tap correctly
  identifying injected recall context. Use an explicit sentinel (role/metadata), not content heuristics,
  and unit-test that a recalled slice makes exactly zero new `turns` rows.
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
- A keyless / offline / `-NetJail` run still archives (deterministic summary) and exits 0; its
  `sessions` row has `cost_usd` NULL, not a fabricated number.
- A **resumed** named thread (`--thread-id x` run twice) produces a **second** `sessions` row with a
  distinct `run_id` — no PK collision, no `turns` idx collision.
- A `/recall` slice injected into a turn produces **zero** new `turns` rows (no re-archiving,
  no compounding on repeat recall).
- A `--topic auth` run's recall returns only `auth`-lane sessions; `/recall --all` returns the whole
  archive; an untagged session recalls globally. `harness past list --topic auth` shows only that lane.
- After a real session, its `sessions` row carries token/cost totals matching the M1 stderr
  session-total line.
- `harness threads list` and `harness past list` show seeded rows; `rm`/`prune` delete only the
  targeted rows and refuse a mass delete without `--yes`.
- `deepagent-image/CLAUDE.md` states the past-archive-vs-deepagents-`memories/` distinction (§2.7).
- **Removing the feature** (delete `archive.py` + `memadmin.py` + wiring, default `--thread-id` back to
  `"default"`) leaves the harness behaving exactly like Milestone 1 — verified by the smoke test passing
  unchanged.
- `smoke` and `verify` pass; `.ps1` / `.sh` script pairs stay behavior-identical (no run-script
  change expected this milestone).
