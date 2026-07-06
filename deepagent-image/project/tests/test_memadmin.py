"""Unit tests for harness/memadmin.py — keyless threads/past admin (M2 §2.6).

Host-runnable: memadmin.py is stdlib `sqlite3` + `harness.archive` only (no run
path, no keys, no network). Tests point `--db` straight at a tmp sqlite so no
workspace resolution is needed. The shared lazy loader lives in `_bootstrap.py`.
"""

from __future__ import annotations

import sqlite3

from _bootstrap import _load

archive = _load("harness.archive")
memadmin = _load("harness.memadmin")


# --- fixtures ----------------------------------------------------------------

def _past_db(tmp_path):
    path = tmp_path / "past.sqlite"
    conn = archive.connect(path)
    archive.start_session(conn, "run-auth", "t1", "openai", "gpt", topic="auth")
    archive.append_turn(conn, "run-auth", [{"role": "user", "content": "auth q"}, {"role": "ai", "content": "a"}])
    archive.end_session(conn, "run-auth", "auth summary", input_tokens=10, output_tokens=5,
                        cost_usd=0.02, cost_provenance="official")
    archive.start_session(conn, "run-ui", "t2", "openai", "gpt", topic="ui")
    archive.end_session(conn, "run-ui", "ui summary")
    conn.close()
    return path


def _checkpoints_db(tmp_path):
    """Minimal stand-in for LangGraph's SqliteSaver schema (thread_id + checkpoint_id)."""
    path = tmp_path / "checkpoints.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
    conn.execute("CREATE TABLE writes (thread_id TEXT)")
    conn.executemany(
        "INSERT INTO checkpoints VALUES (?, ?)",
        [("thread-a", "c1"), ("thread-a", "c2"), ("thread-b", "c3")],
    )
    conn.commit()
    conn.close()
    return path


# --- past --------------------------------------------------------------------

def test_past_list(tmp_path, capsys):
    db = _past_db(tmp_path)
    assert memadmin.memadmin_main(["past", "list", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "run-auth" in out and "run-ui" in out


def test_past_list_topic_filter(tmp_path, capsys):
    db = _past_db(tmp_path)
    memadmin.memadmin_main(["past", "list", "--db", str(db), "--topic", "auth"])
    out = capsys.readouterr().out
    assert "run-auth" in out
    assert "run-ui" not in out


def test_past_topics(tmp_path, capsys):
    db = _past_db(tmp_path)
    memadmin.memadmin_main(["past", "topics", "--db", str(db)])
    out = capsys.readouterr().out
    assert "auth" in out and "ui" in out


def test_past_show(tmp_path, capsys):
    db = _past_db(tmp_path)
    memadmin.memadmin_main(["past", "show", "run-auth", "--db", str(db)])
    out = capsys.readouterr().out
    assert "auth q" in out  # drills into turns


def test_past_rm_guarded_without_yes(tmp_path, capsys):
    db = _past_db(tmp_path)
    rc = memadmin.memadmin_main(["past", "rm", "run-auth", "--db", str(db)])
    assert rc == 1
    assert "--yes" in capsys.readouterr().out
    # Row survives the refused delete.
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE run_id='run-auth'").fetchone()[0] == 1


def test_past_rm_with_yes_deletes_only_target(tmp_path):
    db = _past_db(tmp_path)
    assert memadmin.memadmin_main(["past", "rm", "run-auth", "--db", str(db), "--yes"]) == 0
    conn = sqlite3.connect(str(db))
    remaining = {r[0] for r in conn.execute("SELECT run_id FROM sessions")}
    assert remaining == {"run-ui"}  # only the target went
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE run_id='run-auth'").fetchone()[0] == 0


def test_past_prune_topic_targets_only_matched(tmp_path):
    db = _past_db(tmp_path)
    assert memadmin.memadmin_main(["past", "prune", "--topic", "auth", "--db", str(db), "--yes"]) == 0
    conn = sqlite3.connect(str(db))
    assert {r[0] for r in conn.execute("SELECT run_id FROM sessions")} == {"run-ui"}


def test_past_prune_guarded_without_yes(tmp_path):
    db = _past_db(tmp_path)
    assert memadmin.memadmin_main(["past", "prune", "--topic", "auth", "--db", str(db)]) == 1
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2  # nothing deleted


# --- threads -----------------------------------------------------------------

def test_threads_list(tmp_path, capsys):
    db = _checkpoints_db(tmp_path)
    memadmin.memadmin_main(["threads", "list", "--db", str(db)])
    out = capsys.readouterr().out
    assert "thread-a" in out and "thread-b" in out


def test_threads_rm_guarded(tmp_path):
    db = _checkpoints_db(tmp_path)
    assert memadmin.memadmin_main(["threads", "rm", "thread-a", "--db", str(db)]) == 1
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id='thread-a'").fetchone()[0] == 2


def test_threads_rm_with_yes(tmp_path):
    db = _checkpoints_db(tmp_path)
    assert memadmin.memadmin_main(["threads", "rm", "thread-a", "--db", str(db), "--yes"]) == 0
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id='thread-a'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id='thread-b'").fetchone()[0] == 1


def test_threads_prune_keeps_n(tmp_path):
    db = _checkpoints_db(tmp_path)
    # keep 1 most-recent thread (by MAX checkpoint_id = 'c3' => thread-b), prune the rest.
    assert memadmin.memadmin_main(["threads", "prune", "--keep", "1", "--db", str(db), "--yes"]) == 0
    conn = sqlite3.connect(str(db))
    threads = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    assert threads == {"thread-b"}


def test_missing_db_is_graceful(tmp_path, capsys):
    missing = tmp_path / "nope.sqlite"
    assert memadmin.memadmin_main(["past", "list", "--db", str(missing)]) == 0
    assert "not found" in capsys.readouterr().out
