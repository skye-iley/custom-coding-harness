"""Tests for harness/cli.py — arg parsing, env coercion, exit + budget wiring.

cli.py pulls the whole runtime stack (dotenv, langgraph, deepagents via
harness.agent), so the module is gated behind importorskip and runs only in the
runtime/test image. The focus is the deterministic glue: argument defaults, env
float/int coercion, the Python-side exit-command match, and the
"build a cost tracker only when there's something to track" contract that keeps
the harness byte-for-byte MVP when nothing needs tracking (§2.5).
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")  # image-only
pytest.importorskip("dotenv")
pytest.importorskip("deepagents")

from _bootstrap import _load  # noqa: E402

cli = _load("harness.cli")
cost = _load("harness.cost")
providers = _load("harness.providers")


# --- parse_args ------------------------------------------------------------

def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["main.py", *args])


def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_WORKSPACE", raising=False)
    monkeypatch.delenv("DEEPAGENTS_THREAD_ID", raising=False)
    monkeypatch.delenv("DEEPAGENTS_MAX_COST", raising=False)
    monkeypatch.delenv("DEEPAGENTS_MAX_TOKENS", raising=False)
    _argv(monkeypatch)
    ns = cli.parse_args()
    assert ns.task == []
    assert ns.model is None
    # M2: no thread arg => a FRESH per-run present thread, not the literal "default".
    assert ns.thread_id.startswith("session-")
    assert ns.topic is None
    assert ns.workspace.endswith("workspace")
    assert ns.max_cost is None and ns.max_tokens is None
    assert ns.stream is False


def test_parse_args_collects_task_words(monkeypatch):
    _argv(monkeypatch, "fix", "the", "bug")
    assert cli.parse_args().task == ["fix", "the", "bug"]


def test_parse_args_model_and_stream_flags(monkeypatch):
    _argv(monkeypatch, "--model", "openai:gpt", "--stream")
    ns = cli.parse_args()
    assert ns.model == "openai:gpt" and ns.stream is True


def test_parse_args_budget_from_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_MAX_COST", "2.5")
    monkeypatch.setenv("DEEPAGENTS_MAX_TOKENS", "1000")
    _argv(monkeypatch)
    ns = cli.parse_args()
    assert ns.max_cost == 2.5 and ns.max_tokens == 1000


def test_parse_args_cli_budget_overrides_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_MAX_COST", "2.5")
    _argv(monkeypatch, "--max-cost", "9.0")
    assert cli.parse_args().max_cost == 9.0


# --- _env_float / _env_int -------------------------------------------------

def test_env_float_present(monkeypatch):
    monkeypatch.setenv("X", "1.5")
    assert cli._env_float("X") == 1.5


def test_env_float_absent_or_empty(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert cli._env_float("X") is None
    monkeypatch.setenv("X", "")
    assert cli._env_float("X") is None


def test_env_int_present_and_absent(monkeypatch):
    monkeypatch.setenv("N", "42")
    assert cli._env_int("N") == 42
    monkeypatch.delenv("N", raising=False)
    assert cli._env_int("N") is None


def test_env_float_malformed_raises_systemexit(monkeypatch):
    monkeypatch.setenv("X", "abc")
    with pytest.raises(SystemExit):
        cli._env_float("X")


def test_env_int_malformed_raises_systemexit(monkeypatch):
    monkeypatch.setenv("N", "1.5")
    with pytest.raises(SystemExit):
        cli._env_int("N")


# --- dispatch (shared entry for main.py and -m harness) --------------------

def test_dispatch_default_runs_agent(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: 7)
    assert cli.dispatch([]) == 7
    assert cli.dispatch(["do", "a", "task"]) == 7


def test_dispatch_routes_sync_models(monkeypatch):
    import harness.sync_models as sm

    seen = {}
    # update() returns None so the lambda yields 0 (the success exit code);
    # `setdefault(...) or 0` returned the truthy argv list instead of 0.
    monkeypatch.setattr(sm, "sync_models_main", lambda argv: seen.update(argv=argv) or 0)
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("agent loop must not run for sync-models"))
    assert cli.dispatch(["sync-models", "--dry-run"]) == 0
    assert seen["argv"] == ["--dry-run"]


# --- _is_exit_command ------------------------------------------------------

@pytest.mark.parametrize("line", ["/exit", "/quit", " /EXIT ", "/Quit\n"])
def test_is_exit_command_true(line):
    assert cli._is_exit_command(line)


@pytest.mark.parametrize("line", ["exit", "/exitnow", "hello", "", "  "])
def test_is_exit_command_false(line):
    assert not cli._is_exit_command(line)


# --- build_cost_tracker — the null=MVP contract ----------------------------

def test_tracker_none_when_nothing_to_track(monkeypatch):
    # Unknown model -> no provider -> Free pricing, no energy, no budget -> None,
    # so main() appends no middleware (byte-for-byte MVP, §2.5).
    monkeypatch.setattr(providers, "PROVIDERS", [])
    assert cli.build_cost_tracker("unknown:x", None, None) is None


def test_tracker_built_when_budget_set(monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", [])
    tracker = cli.build_cost_tracker("unknown:x", 5.0, None)
    assert isinstance(tracker, cost.CostTrackerMiddleware)


def test_tracker_built_for_priced_provider(tmp_path, monkeypatch):
    pdir = tmp_path / "acme"
    (pdir / "models").mkdir(parents=True)
    (pdir / "provider.toml").write_text(
        'api_key_env = "ACME_API_KEY"\nrequires_key = false\npriority = 1\n'
        'pricing = "rate_table"\n',
        encoding="utf-8",
    )
    (pdir / "models" / "m1.toml").write_text(
        'name = "m1"\n[pricing]\ninput = 1.0\noutput = 2.0\n', encoding="utf-8"
    )
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    # Non-Free pricing alone is enough to build the tracker, with no budget set.
    assert cli.build_cost_tracker("acme:m1", None, None) is not None


def test_tracker_built_for_energy_only_model(tmp_path, monkeypatch):
    pdir = tmp_path / "local"
    (pdir / "models").mkdir(parents=True)
    (pdir / "provider.toml").write_text(
        'api_key_env = "LOCAL_API_KEY"\nrequires_key = false\npriority = 1\n',
        encoding="utf-8",  # default pricing = free
    )
    (pdir / "models" / "m1.toml").write_text(
        'name = "m1"\n[energy]\nper_input_token = 0.0002\n', encoding="utf-8"
    )
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    # Free pricing but an energy estimate -> still tracked.
    assert cli.build_cost_tracker("local:m1", None, None) is not None


# --- LangSmith tracing guard -----------------------------------------------

def _clear_langsmith(monkeypatch):
    for var in (*cli._LANGSMITH_TRACING_VARS, *cli._LANGSMITH_KEY_VARS):
        monkeypatch.delenv(var, raising=False)


def test_langsmith_enabled_without_key_is_disabled(monkeypatch):
    _clear_langsmith(monkeypatch)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    cli._guard_langsmith()
    # Enabled but keyless -> flag disables tracing rather than connect.
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_langsmith_legacy_flag_without_key_is_disabled(monkeypatch):
    _clear_langsmith(monkeypatch)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "1")
    cli._guard_langsmith()
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_langsmith_enabled_with_key_is_left_on(monkeypatch):
    _clear_langsmith(monkeypatch)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-secret")
    cli._guard_langsmith()
    # Key present -> guard leaves tracing untouched.
    assert os.environ["LANGSMITH_TRACING"] == "true"


def test_langsmith_disabled_stays_disabled(monkeypatch):
    _clear_langsmith(monkeypatch)
    # No flag set at all: guard is a no-op, does not create the var.
    cli._guard_langsmith()
    assert "LANGSMITH_TRACING" not in os.environ


# --- Milestone 2: present/past wiring --------------------------------------

archive = _load("harness.archive")


def test_fresh_thread_id_differs_per_run(monkeypatch):
    # Two parses with no thread arg / env should not collide on the literal
    # "default" — each run gets its own present thread (Def-of-Done 1).
    monkeypatch.delenv("DEEPAGENTS_THREAD_ID", raising=False)
    _argv(monkeypatch)
    ns = cli.parse_args()
    assert ns.thread_id.startswith("session-")
    assert ns.thread_id != "default"


def test_thread_id_env_resumes_named(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_THREAD_ID", "my-refactor")
    _argv(monkeypatch)
    assert cli.parse_args().thread_id == "my-refactor"


def test_topic_from_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_TOPIC", "auth")
    _argv(monkeypatch)
    assert cli.parse_args().topic == "auth"


def test_topic_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_TOPIC", "auth")
    _argv(monkeypatch, "--topic", "ui")
    assert cli.parse_args().topic == "ui"


def test_dispatch_routes_threads_and_past(monkeypatch):
    import harness.memadmin as ma

    seen = {}
    # update() returns None => the lambda yields 0 (success), not the argv list.
    monkeypatch.setattr(ma, "memadmin_main", lambda argv: seen.update(argv=argv) or 0)
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("agent loop must not run for admin"))
    assert cli.dispatch(["past", "list"]) == 0
    assert seen["argv"] == ["past", "list"]
    seen.clear()
    assert cli.dispatch(["threads", "rm", "x", "--yes"]) == 0
    assert seen["argv"] == ["threads", "rm", "x", "--yes"]


def test_handle_topic_sets_and_shows(tmp_path):
    conn = archive.connect(tmp_path / "past.sqlite")
    archive.start_session(conn, "run-1", "t", "p", "m")
    new = cli._handle_topic(conn, "run-1", "/topic auth", None)
    assert new == "auth"
    assert archive.get_topic(conn, "run-1") == "auth"
    # No-arg form shows current, does not change it.
    assert cli._handle_topic(conn, "run-1", "/topic", "auth") == "auth"


def test_handle_recall_stages_marked_slice(tmp_path):
    conn = archive.connect(tmp_path / "past.sqlite")
    archive.start_session(conn, "run-1", "t", "p", "m", topic=None)
    archive.append_turn(conn, "run-1", [{"role": "user", "content": "the auth work"},
                                        {"role": "ai", "content": "done"}])
    archive.end_session(conn, "run-1", archive.summarize(conn, "run-1"))

    pending = cli._handle_recall(conn, "/recall auth", None, [])
    assert len(pending) == 1
    msg = pending[0]
    assert msg["role"] == "system"
    assert msg["additional_kwargs"][archive.RECALL_MARK] is True
    assert "the auth work" in msg["content"]


def test_handle_recall_no_query_lists_without_staging(tmp_path):
    conn = archive.connect(tmp_path / "past.sqlite")
    archive.start_session(conn, "run-1", "t", "p", "m")
    archive.end_session(conn, "run-1", "some summary")
    # No query => list mode, nothing staged for injection.
    assert cli._handle_recall(conn, "/recall", None, []) == []


def test_cost_totals_null_without_tracker():
    assert cli._cost_totals_for_row(None) == (None, None, None, None)


def test_cost_totals_free_is_official_zero():
    tracker = cli.build_cost_tracker("unknown:x", 1.0, None)  # budget => tracker built, Free pricing
    tracker.session.input, tracker.session.output = 12, 3
    inp, out, usd, prov = cli._cost_totals_for_row(tracker)
    assert (inp, out, usd, prov) == (12, 3, 0.0, "official")


def test_cost_totals_unpriced_floor_is_null(monkeypatch):
    tracker = cli.build_cost_tracker("unknown:x", 1.0, None)
    tracker.session.input = 10
    tracker.session.unpriced_calls = 1  # a call we couldn't price
    inp, out, usd, prov = cli._cost_totals_for_row(tracker)
    # Free pricing short-circuits to official 0 only when pricing is Free; here we
    # force the unpriced branch by swapping pricing to a non-Free stub.
    monkeypatch.setattr(tracker, "_pricing", object())
    inp, out, usd, prov = cli._cost_totals_for_row(tracker)
    assert usd is None and prov is None


# --- run_repl resilience: a turn error must not crash the session -----------

class _BoomAgent:
    """Agent whose every invoke raises, standing in for a transient provider
    error (e.g. Gemini `ServerError: 500 INTERNAL`) surfaced out of invoke."""

    def __init__(self):
        self.calls = 0

    def invoke(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("500 INTERNAL")


def test_run_repl_turn_error_non_interactive_closes_cleanly(monkeypatch):
    # A one-shot (non-TTY) run whose only turn hits a provider error must not
    # propagate the exception out of run_repl — otherwise main() skips archive
    # finalization and the container dies with a traceback. It should close with
    # rc 0 so main() still finalizes the session row.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    agent = _BoomAgent()
    rc = cli.run_repl(agent, {}, "do the thing")
    assert rc == 0
    assert agent.calls == 1


def test_run_repl_turn_error_interactive_survives_to_next_prompt(monkeypatch):
    # In an interactive session a failed turn is reported and the loop keeps
    # going: the user gets the prompt back to retry, the session is not killed.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    # Force the plain-input() fallback so the test drives the loop without a real
    # terminal (prompt_toolkit's session.prompt would need one).
    monkeypatch.setattr(cli, "_make_prompt_session", lambda *a, **k: None)
    prompts = iter(["hello"])  # one prompt, then EOF ends the loop

    def fake_input(_prompt=""):
        try:
            return next(prompts)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    agent = _BoomAgent()
    rc = cli.run_repl(agent, {}, "")  # no initial task; the loop drives the turn
    assert rc == 0
    assert agent.calls == 1  # the failing turn ran and was caught, not re-raised


# --- prompt_toolkit REPL input ergonomics (M3 slice 6, PR-a) ---

def test_slash_commands_gated_on_archive():
    # /exit and /quit always available; /recall and /topic only when the past
    # archive is on (they are inert otherwise, so must not appear in the menu).
    base = cli.slash_commands(archive_on=False)
    assert set(base) == {"/exit", "/quit"}

    witharchive = cli.slash_commands(archive_on=True)
    assert {"/recall", "/topic"} <= set(witharchive)
    # Every command carries a non-empty description (the completion preview meta).
    assert all(witharchive.values())


def test_read_line_falls_back_to_input_without_session(monkeypatch):
    # session=None => plain input(), so a non-TTY / prompt_toolkit-less run is
    # byte-for-byte the old behavior.
    monkeypatch.setattr("builtins.input", lambda prompt="": "typed line")
    assert cli._read_line(None, "you> ") == "typed line"


def test_read_line_uses_session_when_present():
    # With a session, the read goes through session.prompt (not input()).
    class _FakeSession:
        def __init__(self):
            self.seen = None

        def prompt(self, prompt_str):
            self.seen = prompt_str
            return "from session"

    sess = _FakeSession()
    assert cli._read_line(sess, "you> ") == "from session"
    assert sess.seen == "you> "


def test_completion_candidates_match_typed_slash_token():
    # The completion menu is derived from a pure function (no terminal needed):
    # matching slash commands with their preview description, and nothing for a
    # non-slash prompt or once an argument is typed.
    cmds = cli.slash_commands(archive_on=True)

    assert cli._completion_candidates("/re", cmds) == [("/recall", cmds["/recall"])]
    assert sorted(c for c, _ in cli._completion_candidates("/", cmds)) == [
        "/exit",
        "/quit",
        "/recall",
        "/topic",
    ]
    # Every candidate carries a non-empty preview description.
    assert all(meta for _, meta in cli._completion_candidates("/", cmds))
    # A plain prompt, or a command with an argument, gets no menu (minimal feel).
    assert cli._completion_candidates("fix the bug", cmds) == []
    assert cli._completion_candidates("/recall foo", cmds) == []


def test_make_prompt_session_returns_none_when_prompt_toolkit_missing(monkeypatch):
    # If prompt_toolkit can't be imported, the builder degrades to None so the
    # REPL falls back to input() rather than crashing.
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("prompt_toolkit"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert cli._make_prompt_session(archive_on=False, history_path=None) is None


# --- _dump_partial: surface accumulated turn state on error (DEEPAGENTS_DEBUG) ---

class _Msg:
    def __init__(self, type_, content, tool_calls=None):
        self.type = type_
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


class _Snap:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _StatefulAgent:
    def __init__(self, snap=None, raises=None):
        self._snap = snap
        self._raises = raises

    def get_state(self, config):
        if self._raises is not None:
            raise self._raises
        return self._snap


def test_dump_partial_silent_without_debug(monkeypatch, capsys):
    monkeypatch.delenv("DEEPAGENTS_DEBUG", raising=False)
    agent = _StatefulAgent(_Snap([_Msg("ai", "hi")]))
    cli._dump_partial(agent, {})
    assert capsys.readouterr().err == ""  # gated off, no state pull at all


def test_dump_partial_prints_messages_and_tool_calls(monkeypatch, capsys):
    monkeypatch.setenv("DEEPAGENTS_DEBUG", "1")
    snap = _Snap([
        _Msg("human", "test"),
        _Msg("ai", "reasoning about the task", tool_calls=[{"name": "write_file"}]),
    ])
    cli._dump_partial(_StatefulAgent(snap), {})
    err = capsys.readouterr().err
    assert "partial turn state (2 msg" in err
    assert "reasoning about the task" in err
    assert "tool_calls=['write_file']" in err


def test_dump_partial_no_messages_reports_none(monkeypatch, capsys):
    monkeypatch.setenv("DEEPAGENTS_DEBUG", "1")
    cli._dump_partial(_StatefulAgent(_Snap([])), {})
    assert "failed before any step was checkpointed" in capsys.readouterr().err


def test_dump_partial_never_raises_when_get_state_fails(monkeypatch, capsys):
    monkeypatch.setenv("DEEPAGENTS_DEBUG", "1")
    agent = _StatefulAgent(raises=RuntimeError("checkpointer gone"))
    cli._dump_partial(agent, {})  # must not raise out of an error handler
    assert "partial state unavailable" in capsys.readouterr().err
