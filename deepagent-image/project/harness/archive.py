"""Past archive — an accumulating SQLite store, separate from the checkpointer.

`past.sqlite` lives beside `checkpoints.sqlite` under `<workspace>/.deepagents/`
but is **never opened by LangGraph**, so it is *structurally* impossible to
auto-inject into model context. Written every session (per-turn `turns` rows +
one `sessions` row); read back only through explicit recall (`/recall`,
`recall_past`).

Two-stack purity (design_doc_milestone2.md §2.2 / §6):
- Runs in the harness venv on **stdlib `sqlite3` only** — no new dependency.
- Must **not** import `providers.py` or `cost.py` (acyclic graph, mirroring the
  `cost.py`-↛-siblings rule). The resolved model, the token/cost totals, and the
  provider/model strings are all passed in from `cli.py` as plain values.

Removable contract (Def-of-Done 9): delete this module + `memadmin.py` + their
wiring and default the present thread back to `"default"`, and the harness
behaves byte-for-byte like Milestone 1.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Only ArchiveMiddleware needs the real AgentMiddleware base; fall back to
# `object` when langchain is absent so `import harness.archive` still works for
# the stdlib-only host tests (same pattern as cost.py).
try:  # pragma: no cover - exercised differently in-container vs. test host
    from langchain.agents.middleware.types import AgentMiddleware
except ModuleNotFoundError:  # pragma: no cover
    AgentMiddleware = object  # type: ignore[assignment,misc]

# Env knob: DEEPAGENTS_ARCHIVE=0 disables the past archive entirely (§3).
ARCHIVE_ENV = "DEEPAGENTS_ARCHIVE"

# Sentinel that marks a message as injected recall context (not a genuine turn).
# Placed in a message's additional_kwargs/metadata by the recall path so the tap
# can skip it and never re-archive recalled slices (§2.3 no-re-archiving gap fix).
RECALL_MARK = "deepagents_recall"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  run_id     TEXT PRIMARY KEY,
  thread_id  TEXT,
  topic      TEXT,
  provider   TEXT,
  model      TEXT,
  started    TEXT,
  ended      TEXT,
  summary    TEXT,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  cost_usd        REAL,
  cost_provenance TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_id);
CREATE INDEX IF NOT EXISTS idx_sessions_topic  ON sessions(topic);
CREATE TABLE IF NOT EXISTS turns (
  run_id     TEXT REFERENCES sessions(run_id),
  idx        INTEGER,
  role       TEXT,
  content    TEXT,
  ts         TEXT,
  PRIMARY KEY (run_id, idx)
);
"""


# --- helpers -----------------------------------------------------------------

def archive_enabled() -> bool:
    """False only when DEEPAGENTS_ARCHIVE is explicitly '0' (default on)."""
    return os.getenv(ARCHIVE_ENV, "1").strip() != "0"


def default_db_path(workspace: Path | str) -> Path:
    return Path(workspace) / ".deepagents" / "past.sqlite"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating parent dir + schema) the archive DB with row access."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the single connection is created on the main
    # thread (cli.main) but the recall_past agent tool reads it from a langgraph
    # worker thread. Access stays effectively serialized — the agent runs one
    # turn at a time, so no two threads touch the connection concurrently — but
    # sqlite3's default same-thread guard would still reject the cross-thread
    # read. This mirrors how langgraph's own SqliteSaver opens its DB.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _get(msg: Any, key: str) -> Any:
    """Read an attribute or dict key off a message-like object.

    Works for both langchain message objects (attributes) and the plain dicts
    the host tests use, so the tap logic is testable without langchain.
    """
    if isinstance(msg, dict):
        return msg.get(key)
    return getattr(msg, key, None)


def _is_recall_marked(msg: Any) -> bool:
    for key in ("additional_kwargs", "metadata", "response_metadata"):
        bag = _get(msg, key)
        if isinstance(bag, dict) and bag.get(RECALL_MARK):
            return True
    return False


_ROLE_MAP = {"human": "human", "user": "human", "ai": "ai", "assistant": "ai"}


def _msg_role(msg: Any) -> str | None:
    raw = _get(msg, "type") or _get(msg, "role")
    return _ROLE_MAP.get(raw) if isinstance(raw, str) else None


def _msg_text(msg: Any) -> str:
    content = _get(msg, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def current_turn_messages(messages: list) -> list:
    """The human + assistant messages of the most recent turn.

    A turn's real input is the last human message in the thread; everything
    after it belongs to this turn (the AI reply, any tool calls/results). Slicing
    from the last human message captures exactly this turn regardless of how many
    prior turns the thread holds, and naturally excludes any recall context
    injected *before* the human message (§2.3).
    """
    last_human = -1
    for i, msg in enumerate(messages):
        if _msg_role(msg) == "human":
            last_human = i
    if last_human < 0:
        return []
    return list(messages[last_human:])


# --- write path --------------------------------------------------------------

def start_session(
    conn: sqlite3.Connection,
    run_id: str,
    thread_id: str,
    provider: str | None,
    model: str | None,
    *,
    topic: str | None = None,
    started: str | None = None,
) -> None:
    """Insert the run's `sessions` row at session.start (one row per run).

    INSERT OR IGNORE so a re-entrant start (same run_id) is a harmless no-op;
    `ended`/`summary`/token/cost columns stay NULL until end_session.
    """
    conn.execute(
        "INSERT OR IGNORE INTO sessions "
        "(run_id, thread_id, topic, provider, model, started) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, thread_id, topic, provider, model, started or _now_iso()),
    )
    conn.commit()


def set_topic(conn: sqlite3.Connection, run_id: str, topic: str | None) -> None:
    """Set/switch this run's continual-topic label (`/topic`, §2.4)."""
    conn.execute("UPDATE sessions SET topic = ? WHERE run_id = ?", (topic, run_id))
    conn.commit()


def get_topic(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute("SELECT topic FROM sessions WHERE run_id = ?", (run_id,)).fetchone()
    return row["topic"] if row else None


def append_turn(conn: sqlite3.Connection, run_id: str, messages: Iterable) -> int:
    """Append this turn's genuine human/ai messages to `turns`. Returns the count.

    Skips any recall-marked message (injected past context) so recall never
    re-archives — the no-re-archiving invariant (§2.3). Empty-content messages
    (e.g. an AI message that is only a tool call) are skipped. `idx` continues
    from the run's current max, so resumed runs and multi-message turns stay
    ordered without collision.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(idx), -1) AS m FROM turns WHERE run_id = ?", (run_id,)
    ).fetchone()
    idx = row["m"] + 1
    appended = 0
    for msg in messages:
        if _is_recall_marked(msg):
            continue
        role = _msg_role(msg)
        if role not in ("human", "ai"):
            continue
        content = _msg_text(msg)
        if not content.strip():
            continue
        conn.execute(
            "INSERT INTO turns (run_id, idx, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (run_id, idx, role, content, _now_iso()),
        )
        idx += 1
        appended += 1
    if appended:
        conn.commit()
    return appended


def get_turns(conn: sqlite3.Connection, run_id: str, *, limit: int | None = None) -> list[dict]:
    sql = "SELECT idx, role, content, ts FROM turns WHERE run_id = ? ORDER BY idx"
    params: list = [run_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def end_session(
    conn: sqlite3.Connection,
    run_id: str,
    summary: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    cost_provenance: str | None = None,
    ended: str | None = None,
) -> None:
    """Close the run's `sessions` row: summary + token/cost totals (§2.3).

    Called after the Milestone 1 session-total line prints, so the ledger row
    matches stderr. A keyless run leaves `cost_usd` NULL (never a wrong number).
    """
    conn.execute(
        "UPDATE sessions SET ended = ?, summary = ?, input_tokens = ?, "
        "output_tokens = ?, cost_usd = ?, cost_provenance = ? WHERE run_id = ?",
        (
            ended or _now_iso(),
            summary,
            input_tokens,
            output_tokens,
            cost_usd,
            cost_provenance,
            run_id,
        ),
    )
    conn.commit()


# --- summary -----------------------------------------------------------------

def _deterministic_summary(turns: list[dict]) -> str:
    """Non-LLM fallback: first human prompt + turn count. Never blank, never fails."""
    if not turns:
        return "(empty session — no turns)"
    first_human = next((t["content"] for t in turns if t["role"] == "human"), "")
    head = first_human.strip().splitlines()[0].strip() if first_human.strip() else "(no prompt)"
    if len(head) > 200:
        head = head[:197] + "..."
    # Count exchanges (human prompts), not raw rows, so "turn" reads naturally.
    n = sum(1 for t in turns if t["role"] == "human") or len(turns)
    return f"{head} — {n} turn{'s' if n != 1 else ''}"


def _llm_summary(model: Any, turns: list[dict]) -> str:
    """Cheap 1-2 sentence condense using the already-resolved chat model.

    `model` is a langchain chat model passed in from cli.py (archive.py never
    resolves one itself, §2.2). Raises on any failure so the caller can fall back.
    """
    transcript = "\n".join(f"{t['role']}: {t['content']}" for t in turns)[:8000]
    prompt = (
        "Summarize this coding-agent session in 1-2 sentences for a searchable "
        "archive. Focus on what was asked and what was done/decided. Be terse.\n\n"
        + transcript
    )
    resp = model.invoke(prompt)
    text = getattr(resp, "content", None)
    if isinstance(text, list):  # some providers return content parts
        text = "\n".join(
            str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in text
        )
    text = (text or "").strip()
    if not text:
        raise ValueError("empty summary")
    return text


def summarize(conn: sqlite3.Connection, run_id: str, *, model: Any = None) -> str:
    """Condense a run's turns into one summary string.

    LLM condense when a model is given and the call succeeds; otherwise the
    deterministic fallback (keyless/offline/failed). Same "safe no-op" discipline
    as the git-pr workflow — session.end must never fail the run.
    """
    turns = get_turns(conn, run_id)
    fallback = _deterministic_summary(turns)
    if model is None or not turns:
        return fallback
    try:
        return _llm_summary(model, turns)
    except Exception as exc:  # pragma: no cover - network/provider dependent
        print(
            f"[harness] archive: LLM summary failed ({exc}); using deterministic summary.",
            file=sys.stderr,
        )
        return fallback


# --- read path ---------------------------------------------------------------

@dataclass
class Hit:
    run_id: str
    thread_id: str | None
    topic: str | None
    provider: str | None
    model: str | None
    started: str | None
    ended: str | None
    summary: str | None
    turns: list[dict] = field(default_factory=list)


def recall(
    conn: sqlite3.Connection,
    query: str = "",
    *,
    topic: str | None = None,
    limit: int = 5,
    include_turns: bool = True,
    turn_limit: int = 20,
) -> list[Hit]:
    """Search the archive; returns matching sessions, most recent first.

    Scans `sessions.summary` (compact, fast) with a keyword LIKE for v1. `topic`
    None scans the whole archive (today's behavior); a topic value filters to that
    lane first (uses `idx_sessions_topic`). Scoping only *narrows* — it adds no
    auto-load path, so the "never auto-injected" invariant holds. The
    (`topic`, `query`) signature leaves room for a later embedding backend without
    changing callers.
    """
    sql = "SELECT * FROM sessions"
    conds: list[str] = []
    params: list = []
    if topic is not None:
        conds.append("topic = ?")
        params.append(topic)
    q = query.strip()
    if q:
        conds.append("(summary LIKE ? OR run_id LIKE ? OR thread_id LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY started DESC LIMIT ?"
    params.append(limit)

    hits: list[Hit] = []
    for r in conn.execute(sql, params).fetchall():
        turns = get_turns(conn, r["run_id"], limit=turn_limit) if include_turns else []
        hits.append(
            Hit(
                run_id=r["run_id"],
                thread_id=r["thread_id"],
                topic=r["topic"],
                provider=r["provider"],
                model=r["model"],
                started=r["started"],
                ended=r["ended"],
                summary=r["summary"],
                turns=turns,
            )
        )
    return hits


def list_topics(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT topic, COUNT(*) AS n FROM sessions WHERE topic IS NOT NULL "
        "GROUP BY topic ORDER BY topic"
    ).fetchall()
    return [(r["topic"], r["n"]) for r in rows]


def format_hits(hits: list[Hit], *, with_turns: bool = False, turn_chars: int = 400) -> str:
    """Render recall hits as a compact text block for injection / listing."""
    if not hits:
        return ""
    blocks: list[str] = []
    for h in hits:
        header = f"[{h.run_id}]"
        if h.topic:
            header += f" topic={h.topic}"
        if h.started:
            header += f" started={h.started}"
        lines = [header, f"  summary: {h.summary or '(none)'}"]
        if with_turns and h.turns:
            for t in h.turns:
                snippet = t["content"].strip().replace("\n", " ")
                if len(snippet) > turn_chars:
                    snippet = snippet[: turn_chars - 3] + "..."
                lines.append(f"  {t['role']}: {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --- middleware --------------------------------------------------------------

class ArchiveMiddleware(AgentMiddleware):
    """Tap each completed turn and append its human/ai messages to `past.sqlite`.

    Appended in `cli.main` next to the cost tracker (only when the archive is
    enabled). Writes per-turn — not only at session.end — so a crash or Ctrl-C
    mid-session still leaves the archive consistent up to the last finished turn
    (§2.3 crash-safety). Skips recall-marked messages so recall never re-archives.
    """

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        super().__init__()
        self._conn = conn
        self._run_id = run_id

    def after_agent(self, state, runtime):
        messages = (
            state.get("messages")
            if isinstance(state, dict)
            else getattr(state, "messages", None)
        )
        if not messages:
            return
        append_turn(self._conn, self._run_id, current_turn_messages(messages))
