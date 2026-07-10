"""Unit tests for harness/archive.py — the past archive (Milestone 2).

Host-runnable: archive.py is stdlib `sqlite3` only and its AgentMiddleware import
falls back to `object` when langchain is absent, so the whole module (incl.
ArchiveMiddleware) imports on a bare interpreter. No keys, no network. All writes
go to `tmp_path`. The shared lazy loader lives in `_bootstrap.py`.
"""

from __future__ import annotations

from _bootstrap import _load

archive = _load("harness.archive")


# --- helpers -----------------------------------------------------------------

def _db(tmp_path):
    return archive.connect(tmp_path / "past.sqlite")


def _turn(human: str, ai: str) -> list[dict]:
    return [
        {"role": "user", "content": human},
        {"role": "ai", "content": ai},
    ]


# --- schema / connect --------------------------------------------------------

def test_connect_creates_tables(tmp_path):
    conn = _db(tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "turns"} <= names
    assert (tmp_path / "past.sqlite").exists()


def test_connect_is_idempotent(tmp_path):
    _db(tmp_path).close()
    # Re-connecting an existing DB must not error (IF NOT EXISTS schema).
    conn = _db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


# --- write / read round-trip -------------------------------------------------

def test_append_and_recall_round_trip(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "thread-a", "openai", "gpt", topic=None)
    archive.append_turn(conn, "run-1", _turn("add a login form", "done"))
    archive.end_session(conn, "run-1", archive.summarize(conn, "run-1"))

    hits = archive.recall(conn, "login")
    assert [h.run_id for h in hits] == ["run-1"]
    assert hits[0].turns[0]["content"] == "add a login form"
    assert "login" in hits[0].summary


def test_recall_from_other_thread(tmp_path):
    # The recall_past agent tool runs on a langgraph worker thread while the
    # connection was opened on the main thread. connect() must open with
    # check_same_thread=False or sqlite3 raises ProgrammingError cross-thread.
    import threading

    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "thread-a", "openai", "gpt", topic=None)
    archive.append_turn(conn, "run-1", _turn("the secret word is GIRAFFE", "ok"))
    archive.end_session(conn, "run-1", archive.summarize(conn, "run-1"))

    result: dict = {}

    def _worker():
        try:
            result["hits"] = archive.recall(conn, "GIRAFFE")
        except Exception as exc:  # pragma: no cover - failure path asserts below
            result["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    assert "error" not in result, f"cross-thread recall raised: {result.get('error')!r}"
    assert [h.run_id for h in result["hits"]] == ["run-1"]


def test_append_from_other_thread(tmp_path):
    # The write tap (ArchiveMiddleware -> append_turn) can also fire off a
    # langgraph worker thread while the connection was opened on the main
    # thread. The same check_same_thread=False guard must keep writes working
    # cross-thread, not just reads.
    import threading

    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "thread-a", "openai", "gpt", topic=None)

    result: dict = {}

    def _worker():
        try:
            archive.append_turn(conn, "run-1", _turn("logged off-thread", "ok"))
        except Exception as exc:  # pragma: no cover - failure path asserts below
            result["error"] = exc

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    assert "error" not in result, f"cross-thread append raised: {result.get('error')!r}"
    # The worker-thread write is durable and readable from the main thread.
    assert archive.get_turns(conn, "run-1")[0]["content"] == "logged off-thread"


def test_recall_empty_archive_returns_nothing(tmp_path):
    # The past never auto-loads: an unqueried / empty archive yields nothing.
    assert archive.recall(_db(tmp_path), "anything") == []


def test_append_turn_idx_is_sequential(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    archive.append_turn(conn, "run-1", _turn("q1", "a1"))
    archive.append_turn(conn, "run-1", _turn("q2", "a2"))
    idxs = [t["idx"] for t in archive.get_turns(conn, "run-1")]
    assert idxs == [0, 1, 2, 3]


# --- resumed named thread => distinct run rows (PK gap fix) -------------------

def test_resumed_thread_yields_two_rows_no_collision(tmp_path):
    conn = _db(tmp_path)
    # Same thread_id run twice (Mon/Tue) => two distinct run_ids, no PK collision.
    archive.start_session(conn, "run-mon", "my-refactor", "openai", "gpt")
    archive.append_turn(conn, "run-mon", _turn("monday", "ok"))
    archive.start_session(conn, "run-tue", "my-refactor", "openai", "gpt")
    archive.append_turn(conn, "run-tue", _turn("tuesday", "ok"))

    rows = conn.execute("SELECT run_id FROM sessions WHERE thread_id='my-refactor'").fetchall()
    assert {r[0] for r in rows} == {"run-mon", "run-tue"}
    # turns idx restarts per run without colliding (PK is (run_id, idx)).
    assert [t["idx"] for t in archive.get_turns(conn, "run-tue")] == [0, 1]


# --- no re-archiving of recalled context -------------------------------------

def test_recall_marked_message_not_archived(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    messages = [
        {"role": "system", "content": "PAST CTX", "additional_kwargs": {archive.RECALL_MARK: True}},
        {"role": "user", "content": "real question"},
        {"role": "ai", "content": "real answer"},
    ]
    n = archive.append_turn(conn, "run-1", archive.current_turn_messages(messages))
    assert n == 2  # human + ai only; the marked system slice is skipped
    roles = [t["role"] for t in archive.get_turns(conn, "run-1")]
    assert roles == ["human", "ai"]


def test_marked_only_slice_writes_zero_turns(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    marked = [{"role": "ai", "content": "x", "additional_kwargs": {archive.RECALL_MARK: True}}]
    assert archive.append_turn(conn, "run-1", marked) == 0


def test_current_turn_messages_slices_from_last_human(tmp_path):
    messages = [
        {"role": "user", "content": "old turn"},
        {"role": "ai", "content": "old reply"},
        {"role": "system", "content": "recall", "additional_kwargs": {archive.RECALL_MARK: True}},
        {"role": "user", "content": "new turn"},
        {"role": "ai", "content": "new reply"},
    ]
    slice_ = archive.current_turn_messages(messages)
    assert [archive._msg_text(m) for m in slice_] == ["new turn", "new reply"]


# --- cost / token totals on the row ------------------------------------------

def test_end_session_writes_cost_totals(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "openai", "gpt")
    archive.end_session(
        conn, "run-1", "sum",
        input_tokens=100, output_tokens=40, cost_usd=0.0123, cost_provenance="official",
    )
    row = conn.execute("SELECT * FROM sessions WHERE run_id='run-1'").fetchone()
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 40
    assert abs(row["cost_usd"] - 0.0123) < 1e-9
    assert row["cost_provenance"] == "official"
    assert row["ended"] is not None


def test_keyless_run_leaves_cost_null(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "google_genai", "gemini")
    archive.end_session(conn, "run-1", "sum")  # no cost passed
    row = conn.execute("SELECT cost_usd, cost_provenance FROM sessions WHERE run_id='run-1'").fetchone()
    assert row["cost_usd"] is None
    assert row["cost_provenance"] is None


# --- topic scoping -----------------------------------------------------------

def test_topic_scoped_recall_returns_only_that_lane(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-auth", "t1", "openai", "gpt", topic="auth")
    archive.end_session(conn, "run-auth", "auth work summary")
    archive.start_session(conn, "run-ui", "t2", "openai", "gpt", topic="ui")
    archive.end_session(conn, "run-ui", "ui work summary")
    archive.start_session(conn, "run-free", "t3", "openai", "gpt", topic=None)
    archive.end_session(conn, "run-free", "misc summary")

    scoped = archive.recall(conn, "", topic="auth")
    assert [h.run_id for h in scoped] == ["run-auth"]

    # Untagged / global recall sees the whole pool.
    all_runs = {h.run_id for h in archive.recall(conn, "", topic=None, limit=10)}
    assert all_runs == {"run-auth", "run-ui", "run-free"}


def test_set_topic_updates_row(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m", topic=None)
    archive.set_topic(conn, "run-1", "auth")
    assert archive.get_topic(conn, "run-1") == "auth"
    assert [t[0] for t in archive.list_topics(conn)] == ["auth"]


# --- summary -----------------------------------------------------------------

def test_deterministic_summary_used_without_model(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    archive.append_turn(conn, "run-1", _turn("fix the parser bug", "fixed"))
    summary = archive.summarize(conn, "run-1", model=None)
    assert "fix the parser bug" in summary
    assert "1 turn" in summary  # 1 human turn counted


def test_summary_falls_back_when_model_raises(tmp_path):
    class Boom:
        def invoke(self, *_a, **_k):
            raise RuntimeError("no network")

    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    archive.append_turn(conn, "run-1", _turn("do the thing", "ok"))
    summary = archive.summarize(conn, "run-1", model=Boom())
    assert "do the thing" in summary  # deterministic fallback, no crash


def test_llm_summary_used_when_model_succeeds(tmp_path):
    class Model:
        def invoke(self, *_a, **_k):
            class R:
                content = "Condensed: fixed the parser."
            return R()

    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    archive.append_turn(conn, "run-1", _turn("fix parser", "done"))
    assert archive.summarize(conn, "run-1", model=Model()) == "Condensed: fixed the parser."


# --- removable contract ------------------------------------------------------

def test_archive_enabled_honors_env(monkeypatch):
    monkeypatch.delenv(archive.ARCHIVE_ENV, raising=False)
    assert archive.archive_enabled() is True  # default on
    monkeypatch.setenv(archive.ARCHIVE_ENV, "0")
    assert archive.archive_enabled() is False
    monkeypatch.setenv(archive.ARCHIVE_ENV, "1")
    assert archive.archive_enabled() is True


# --- middleware tap ----------------------------------------------------------

def test_archive_middleware_taps_completed_turn(tmp_path):
    conn = _db(tmp_path)
    archive.start_session(conn, "run-1", "t", "p", "m")
    mw = archive.ArchiveMiddleware(conn, "run-1")
    state = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "ai", "content": "hi"},
    ]}
    mw.after_agent(state, runtime=None)
    assert [t["role"] for t in archive.get_turns(conn, "run-1")] == ["human", "ai"]
