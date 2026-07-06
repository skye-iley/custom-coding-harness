"""Keyless lifecycle admin for the two SQLite stores (Milestone 2 §2.6).

    harness threads list|show|rm|prune       # PRESENT: checkpoints.sqlite (LangGraph)
    harness past    list|show|rm|prune|topics # PAST:    past.sqlite (the archive)

Inspect and prune both DBs from a keyless CLI — no model, no network, no
provider keys. Wired through `cli.dispatch` (argv[0] in {threads, past}). Imports
`archive` for the past-DB schema/queries (a pure data module) but nothing on the
run path (no providers/cost/agent), so it stays a self-contained admin surface.

Deletes are confirm-guarded: `rm`/`prune` refuse to touch rows without `--yes`,
printing what *would* be removed first.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from harness import archive


def _default_workspace() -> str:
    return os.getenv("AGENT_WORKSPACE") or str(Path.cwd() / "workspace")


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _guard(action: str, yes: bool, what: str) -> bool:
    """Return True if the destructive `action` may proceed. Prints + blocks otherwise."""
    if yes:
        return True
    print(f"[harness] refusing to {action} {what} without --yes. Re-run with --yes to confirm.")
    return False


# --- present: checkpoints.sqlite --------------------------------------------

def _checkpoints_path(args) -> Path:
    if args.db:
        return Path(args.db)
    return Path(args.workspace) / ".deepagents" / "checkpoints.sqlite"


def _thread_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # LangGraph's SqliteSaver keys everything on thread_id; checkpoint_id sorts
    # chronologically (monotonic), so MAX(checkpoint_id) is the latest state.
    return conn.execute(
        "SELECT thread_id, COUNT(*) AS n, MAX(checkpoint_id) AS latest "
        "FROM checkpoints GROUP BY thread_id ORDER BY latest DESC"
    ).fetchall()


def _thread_delete(conn: sqlite3.Connection, thread_id: str) -> None:
    for table in ("checkpoints", "writes"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
        except sqlite3.OperationalError:
            pass  # 'writes' may not exist on older schemas
    conn.commit()


def _cmd_threads(args) -> int:
    conn = _connect(_checkpoints_path(args))
    if conn is None:
        print("[harness] no present threads (checkpoints.sqlite not found).")
        return 0
    try:
        if args.action == "list":
            rows = _thread_rows(conn)
            if not rows:
                print("[harness] no present threads.")
                return 0
            for r in rows:
                print(f"{r['thread_id']}\tcheckpoints={r['n']}")
            return 0

        if args.action == "show":
            if not args.target:
                print("[harness] usage: harness threads show <thread_id>")
                return 1
            n = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (args.target,)
            ).fetchone()[0]
            print(f"thread_id={args.target}\tcheckpoints={n}")
            return 0

        if args.action == "rm":
            if not args.target:
                print("[harness] usage: harness threads rm <thread_id> --yes")
                return 1
            if not _guard("delete thread", args.yes, args.target):
                return 1
            _thread_delete(conn, args.target)
            print(f"[harness] removed present thread {args.target}.")
            return 0

        if args.action == "prune":
            rows = _thread_rows(conn)
            keep = args.keep if args.keep is not None else 0
            doomed = [r["thread_id"] for r in rows[keep:]]
            if not doomed:
                print("[harness] nothing to prune.")
                return 0
            if not _guard("prune", args.yes, f"{len(doomed)} thread(s)"):
                for t in doomed:
                    print(f"  would remove: {t}")
                return 1
            for t in doomed:
                _thread_delete(conn, t)
            print(f"[harness] pruned {len(doomed)} present thread(s), kept {keep}.")
            return 0
    finally:
        conn.close()
    print(f"[harness] unknown threads action: {args.action}")
    return 1


# --- past: past.sqlite -------------------------------------------------------

def _past_path(args) -> Path:
    if args.db:
        return Path(args.db)
    return archive.default_db_path(args.workspace)


def _fmt_session(r: sqlite3.Row) -> str:
    cost = "-" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}"
    prov = r["cost_provenance"] or ""
    topic = r["topic"] or "-"
    summary = (r["summary"] or "").splitlines()[0] if r["summary"] else ""
    return (
        f"{r['run_id']}\ttopic={topic}\tthread={r['thread_id']}\t"
        f"started={r['started']}\tcost={cost}{('/' + prov) if prov else ''}\t{summary}"
    )


def _session_rows(conn, topic: str | None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM sessions"
    params: list = []
    if topic is not None:
        sql += " WHERE topic = ?"
        params.append(topic)
    sql += " ORDER BY started DESC"
    return conn.execute(sql, params).fetchall()


def _past_delete(conn, run_ids: list[str]) -> None:
    for run_id in run_ids:
        conn.execute("DELETE FROM turns WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM sessions WHERE run_id = ?", (run_id,))
    conn.commit()


def _cmd_past(args) -> int:
    conn = _connect(_past_path(args))
    if conn is None:
        print("[harness] no past archive (past.sqlite not found).")
        return 0
    try:
        if args.action == "list":
            rows = _session_rows(conn, args.topic)
            if not rows:
                print("[harness] no archived sessions" + (f" in topic '{args.topic}'." if args.topic else "."))
                return 0
            for r in rows:
                print(_fmt_session(r))
            return 0

        if args.action == "topics":
            topics = archive.list_topics(conn)
            if not topics:
                print("[harness] no topics.")
                return 0
            for name, n in topics:
                print(f"{name}\tsessions={n}")
            return 0

        if args.action == "show":
            if not args.target:
                print("[harness] usage: harness past show <run_id>")
                return 1
            row = conn.execute("SELECT * FROM sessions WHERE run_id = ?", (args.target,)).fetchone()
            if row is None:
                print(f"[harness] no such run: {args.target}")
                return 1
            print(_fmt_session(row))
            for t in archive.get_turns(conn, args.target):
                snippet = t["content"].strip().replace("\n", " ")
                if len(snippet) > 300:
                    snippet = snippet[:297] + "..."
                print(f"  [{t['idx']}] {t['role']}: {snippet}")
            return 0

        if args.action == "rm":
            if not args.target:
                print("[harness] usage: harness past rm <run_id> --yes")
                return 1
            row = conn.execute("SELECT run_id FROM sessions WHERE run_id = ?", (args.target,)).fetchone()
            if row is None:
                print(f"[harness] no such run: {args.target}")
                return 1
            if not _guard("delete run", args.yes, args.target):
                return 1
            _past_delete(conn, [args.target])
            print(f"[harness] removed archived run {args.target}.")
            return 0

        if args.action == "prune":
            rows = _session_rows(conn, args.topic)
            if args.before:
                rows = [r for r in rows if (r["started"] or "") < args.before]
            doomed = [r["run_id"] for r in rows]
            if not doomed:
                print("[harness] nothing to prune.")
                return 0
            what = f"{len(doomed)} run(s)" + (f" in topic '{args.topic}'" if args.topic else "")
            if not _guard("prune", args.yes, what):
                for rid in doomed:
                    print(f"  would remove: {rid}")
                return 1
            _past_delete(conn, doomed)
            print(f"[harness] pruned {len(doomed)} archived run(s).")
            return 0
    finally:
        conn.close()
    print(f"[harness] unknown past action: {args.action}")
    return 1


# --- entry -------------------------------------------------------------------

def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="harness", add_help=True)
    parser.add_argument("store", choices=["threads", "past"])
    parser.add_argument(
        "action",
        choices=["list", "show", "rm", "prune", "topics"],
        help="threads: list/show/rm/prune; past: also topics.",
    )
    parser.add_argument("target", nargs="?", help="thread_id / run_id for show/rm.")
    parser.add_argument("--workspace", default=_default_workspace())
    parser.add_argument("--db", help="Override the sqlite path directly (bypasses --workspace).")
    parser.add_argument("--topic", help="past: filter list/prune to a topic lane.")
    parser.add_argument("--before", help="past prune: only runs started before this ISO timestamp.")
    parser.add_argument("--keep", type=int, help="threads prune: keep the N most-recent threads.")
    parser.add_argument("--yes", action="store_true", help="Confirm a destructive rm/prune.")
    return parser.parse_args(argv)


def memadmin_main(argv: list[str]) -> int:
    args = _parse(argv)
    if args.store == "threads":
        if args.action == "topics":
            print("[harness] 'topics' applies only to `past`.")
            return 1
        return _cmd_threads(args)
    return _cmd_past(args)
