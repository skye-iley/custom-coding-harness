"""Tests for harness/cli.py — arg parsing, env coercion, exit + budget wiring.

cli.py pulls the whole runtime stack (dotenv, langgraph, deepagents via
harness.agent), so the module is gated behind importorskip and runs only in the
runtime/test image. The focus is the deterministic glue: argument defaults, env
float/int coercion, the Python-side exit-command match, and the
"build a cost tracker only when there's something to track" contract that keeps
the harness byte-for-byte MVP when nothing needs tracking (§2.5).
"""

from __future__ import annotations

import dataclasses
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


def test_dispatch_routes_mask_scan(monkeypatch):
    # M4: `-m harness mask-scan` routes to mask_scan_main, not the agent loop.
    import harness.mask_scan as ms

    seen = {}
    monkeypatch.setattr(ms, "mask_scan_main", lambda argv: seen.update(argv=argv) or 0)
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("agent loop must not run for mask-scan"))
    assert cli.dispatch(["mask-scan", "/ws", "/state"]) == 0
    assert seen["argv"] == ["/ws", "/state"]


def test_dispatch_routes_doctor(monkeypatch):
    # M4: `-m harness doctor` routes to doctor_main, not the agent loop.
    import harness.doctor as doc

    seen = {}
    monkeypatch.setattr(doc, "doctor_main", lambda argv: seen.update(argv=argv) or 0)
    monkeypatch.setattr(cli, "main", lambda: pytest.fail("agent loop must not run for doctor"))
    assert cli.dispatch(["doctor", "/ws"]) == 0
    assert seen["argv"] == ["/ws"]


# --- _should_audit_path_denials (M4 slice D) --------------------------------


def test_should_audit_path_denials_off_when_hitl_off():
    assert cli._should_audit_path_denials(None) is False


def test_should_audit_path_denials_off_when_interrupt_disabled():
    import harness.config as hitl_config

    conf = hitl_config.HitlSection(system_interrupts={"permission_denied": False})
    assert cli._should_audit_path_denials(conf) is False


def test_should_audit_path_denials_on_when_enabled():
    import harness.config as hitl_config

    conf = hitl_config.HitlSection(system_interrupts={"permission_denied": True})
    assert cli._should_audit_path_denials(conf) is True


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


# --- run banner respects the headless stdout contract ----------------------

def test_run_banner_goes_to_stderr_when_headless(capsys):
    # Regression: headless run_batch reserves stdout for its single JSON line.
    # The Model/Workspace/Task/Topic preamble must NOT print to stdout in that
    # mode (it would prepend non-JSON lines a caller then fails to parse).
    cli._print_run_banner("prov:model", "/ws", "do a thing", "mytopic", headless=True)
    cap = capsys.readouterr()
    assert cap.out == ""  # nothing on the machine-readable channel
    assert "Model: prov:model" in cap.err
    assert "Task: do a thing" in cap.err
    assert "Topic: mytopic" in cap.err


def test_run_banner_goes_to_stdout_when_interactive(capsys):
    # Interactive keeps the human-facing preamble on stdout (unchanged behavior).
    cli._print_run_banner("prov:model", "/ws", "do a thing", None, headless=False)
    cap = capsys.readouterr()
    assert "Model: prov:model" in cap.out
    assert "Workspace: /ws" in cap.out
    assert "Topic" not in cap.out  # no topic passed => no Topic line
    assert cap.err == ""


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
    #
    # The "500 INTERNAL" is a retryable transient (P1), so the resilience layer
    # first retries it: with the budget pinned to 2 that is 3 invokes (1 + 2),
    # which also proves run_turn is wrapped by _invoke_resilient. The exhausted
    # error is then caught and the session closes cleanly.
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "2")
    monkeypatch.setenv("DEEPAGENTS_RETRY_BASE", "0.01")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    agent = _BoomAgent()
    rc = cli.run_repl(agent, {}, "do the thing")
    assert rc == 0
    assert agent.calls == 3


def test_run_repl_turn_error_interactive_survives_to_next_prompt(monkeypatch):
    # In an interactive session a failed turn is reported and the loop keeps
    # going: the user gets the prompt back to retry, the session is not killed.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    # Retries off so this isolates the survival property from P1's backoff: one
    # invoke, caught, loop continues.
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "0")
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
    # /exit, /quit, /config always available; /recall and /topic only when the
    # past archive is on (they are inert otherwise, so must not appear in the menu).
    base = cli.slash_commands(archive_on=False)
    assert set(base) == {"/exit", "/quit", "/config"}

    witharchive = cli.slash_commands(archive_on=True)
    assert {"/recall", "/topic"} <= set(witharchive)
    # Every command carries a non-empty description (the completion preview meta).
    assert all(witharchive.values())

    # /refresh is gated on the ephemeral source mount, independent of the archive:
    # absent by default, present (with a description) only when refresh_on.
    assert "/refresh" not in cli.slash_commands(archive_on=True)
    withrefresh = cli.slash_commands(archive_on=False, refresh_on=True)
    assert "/refresh" in withrefresh
    assert all(withrefresh.values())


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
        "/config",
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


def test_repl_key_bindings_enter_submits_ctrl_j_and_alt_enter_newline():
    # Enter submits; Ctrl-J and Alt+Enter insert a newline (typed multi-line).
    # Shift+Enter is deliberately unbound (not portably distinguishable from Enter).
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.keys import Keys

    kb = cli._repl_key_bindings()
    keysets = {tuple(b.keys) for b in kb.bindings}
    assert (Keys.Enter,) in keysets
    assert (Keys.ControlJ,) in keysets
    assert (Keys.Escape, Keys.Enter) in keysets

    def handler_for(keys):
        for b in kb.bindings:
            if tuple(b.keys) == keys:
                return b.handler
        raise AssertionError(f"no binding for {keys}")

    class _CS:
        def __init__(self, completion):
            self.current_completion = completion

    class _Buf:
        def __init__(self, completion=None):
            self.complete_state = _CS(completion) if completion else None
            self.inserted = []
            self.submitted = False
            self.applied = None

        def insert_text(self, text):
            self.inserted.append(text)

        def validate_and_handle(self):
            self.submitted = True

        def apply_completion(self, completion):
            self.applied = completion

    class _Ev:
        def __init__(self, buf):
            self.current_buffer = buf

    # Ctrl-J inserts a newline, does not submit.
    buf = _Buf()
    handler_for((Keys.ControlJ,))(_Ev(buf))
    assert buf.inserted == ["\n"] and not buf.submitted

    # Enter with no open completion submits.
    buf = _Buf()
    handler_for((Keys.Enter,))(_Ev(buf))
    assert buf.submitted and buf.inserted == []

    # Enter with a navigated completion accepts it instead of submitting.
    sentinel = object()
    buf = _Buf(completion=sentinel)
    handler_for((Keys.Enter,))(_Ev(buf))
    assert buf.applied is sentinel and not buf.submitted


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


# --- Milestone 5, C5: /config REPL command ------------------------------------

cfg = _load("harness.config")


def test_parse_config_command_bare_and_with_subcommand():
    assert cli._parse_config_command("/config") == ("", [])
    assert cli._parse_config_command("/config save") == ("save", [])
    assert cli._parse_config_command("/config set model openai:gpt-5.5") == (
        "set", ["model", "openai:gpt-5.5"],
    )


def test_parse_config_set_args_valid():
    assert cli._parse_config_set_args(["model", "openai:gpt-5.5"]) == ("model", "openai:gpt-5.5")
    # Values may contain spaces (e.g. a topic label); only the first token is the field.
    assert cli._parse_config_set_args(["topic", "auth", "refactor"]) == ("topic", "auth refactor")


def test_parse_config_set_args_too_few_raises():
    with pytest.raises(ValueError, match="usage"):
        cli._parse_config_set_args(["model"])
    with pytest.raises(ValueError, match="usage"):
        cli._parse_config_set_args([])


def test_parse_config_set_args_prespinup_field_rejected():
    with pytest.raises(ValueError, match="fixed for this container"):
        cli._parse_config_set_args(["jail", "true"])
    with pytest.raises(ValueError, match="fixed for this container"):
        cli._parse_config_set_args(["mask_mode", "allow"])


def test_parse_config_set_args_unknown_field_rejected():
    with pytest.raises(ValueError, match="unknown field"):
        cli._parse_config_set_args(["bogus", "x"])


def test_config_prespinup_fields_match_live_fields_complement():
    # milestone5_invariants.md #4: derived from LIVE_FIELDS, not hand-duplicated.
    settings_fields = {f.name for f in dataclasses.fields(cfg.Settings)}
    assert set(cli._CONFIG_PRESPINUP_FIELDS) == settings_fields - cfg.LIVE_FIELDS


def test_handle_config_bare_prints_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    model, new_agent, topic = cli._handle_config(
        "/config",
        config={"configurable": {"thread_id": "session-x"}},
        current_model="openai:gpt-5.5",
        topic="auth",
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert model == "openai:gpt-5.5" and new_agent is None and topic == "auth"
    err = capsys.readouterr().err
    assert "model" in err and "openai:gpt-5.5" in err
    assert "thread_id" in err and "session-x" in err
    assert "pre-spinup" in err
    assert "hitl" in err and "off" in err  # no hitl_conf => shown as off


def test_handle_config_set_model_rebuilds_and_switches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_rebuild(spec):
        calls.append(spec)
        return f"agent-for-{spec}"

    edited = set()
    model, new_agent, topic = cli._handle_config(
        "/config set model openai:gpt-6",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=fake_rebuild,
        edited=edited,
    )
    assert calls == ["openai:gpt-6"]
    assert model == "openai:gpt-6"
    assert new_agent == "agent-for-openai:gpt-6"
    assert "model" in edited


def test_handle_config_set_model_failure_keeps_old_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fake_rebuild(spec):
        raise SystemExit("no credentials for that provider")

    model, new_agent, topic = cli._handle_config(
        "/config set model bogus:model",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=fake_rebuild,
        edited=set(),
    )
    assert model == "openai:gpt-5.5"
    assert new_agent is None
    assert "failed" in capsys.readouterr().err


def test_handle_config_set_model_without_rebuild_agent_is_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    model, new_agent, topic = cli._handle_config(
        "/config set model openai:gpt-6",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert model == "openai:gpt-5.5" and new_agent is None
    assert "unavailable" in capsys.readouterr().err


def test_handle_config_set_thread_id_mutates_config_dict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"configurable": {"thread_id": "old-thread"}}
    edited = set()
    cli._handle_config(
        "/config set thread_id new-thread",
        config=config,
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=edited,
    )
    assert config["configurable"]["thread_id"] == "new-thread"
    assert "thread_id" in edited


def test_handle_config_set_topic_returns_new_topic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model, new_agent, topic = cli._handle_config(
        "/config set topic new-topic-label",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic="old-topic",
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert topic == "new-topic-label"


def test_handle_config_set_budget_without_tracker_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli._handle_config(
        "/config set max_cost 5.0",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert "no cost tracker active" in capsys.readouterr().err


class _FakeTracker:
    def __init__(self):
        self._max_cost = None
        self._max_tokens = None


def test_handle_config_set_budget_mutates_tracker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tracker = _FakeTracker()
    edited = set()
    cli._handle_config(
        "/config set max_cost 12.5",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=tracker,
        hitl_conf=None,
        rebuild_agent=None,
        edited=edited,
    )
    assert tracker._max_cost == 12.5
    assert "max_cost" in edited

    cli._handle_config(
        "/config set max_tokens 1000",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=tracker,
        hitl_conf=None,
        rebuild_agent=None,
        edited=edited,
    )
    assert tracker._max_tokens == 1000


def test_handle_config_set_budget_bad_number(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    tracker = _FakeTracker()
    cli._handle_config(
        "/config set max_cost not-a-number",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=tracker,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert tracker._max_cost is None
    assert "must be a number" in capsys.readouterr().err


def test_handle_config_set_hitl_without_hitl_conf_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli._handle_config(
        "/config set hitl.autonomy_level strict",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert "HITL is off" in capsys.readouterr().err


def test_handle_config_set_hitl_mutates_live_object(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hitl_conf = cfg.HitlSection(autonomy_level="guided")
    edited = set()
    cli._handle_config(
        "/config set hitl.autonomy_level strict",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=hitl_conf,
        rebuild_agent=None,
        edited=edited,
    )
    # Frozen dataclass, mutated via object.__setattr__ -- same object, new value,
    # so PauseMiddleware.wrap_tool_call (which reads self._config live) sees it.
    assert hitl_conf.autonomy_level == "strict"
    assert "hitl.autonomy_level" in edited


def test_handle_config_set_hitl_invalid_value_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    hitl_conf = cfg.HitlSection(autonomy_level="guided")
    cli._handle_config(
        "/config set hitl.autonomy_level reckless",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=hitl_conf,
        rebuild_agent=None,
        edited=set(),
    )
    assert hitl_conf.autonomy_level == "guided"  # unchanged
    assert "must be one of" in capsys.readouterr().err


def test_handle_config_save_writes_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    edited = {"model", "topic"}
    cli._handle_config(
        "/config save",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-6",
        topic="my-topic",
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=edited,
    )
    values = cfg.load_profile(tmp_path / cfg.PROFILE_NAME)
    assert values["model"] == "openai:gpt-6"
    assert values["topic"] == "my-topic"


def test_handle_config_save_nothing_edited_is_a_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli._handle_config(
        "/config save",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert not (tmp_path / cfg.PROFILE_NAME).exists()
    assert "nothing session-edited" in capsys.readouterr().err


def test_handle_config_unknown_subcommand(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli._handle_config(
        "/config frobnicate",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert "unknown subcommand" in capsys.readouterr().err


# --- Milestone 5 review fixes: provenance threading + tracker repricing --------


def test_handle_config_uses_threaded_sources_not_a_fresh_resolve(tmp_path, monkeypatch, capsys):
    """Regression: `/config` re-resolved settings with no `cli=` tier, so any
    field passed as a CLI flag reported its provenance as default/env/profile.
    The source tags are the whole point of the display, so the pair parse_args()
    already resolved (WITH the CLI tier) has to be threaded through."""
    monkeypatch.chdir(tmp_path)
    settings = cfg.Settings(model="openai:gpt-6", max_cost=5.0)
    sources = cfg.SettingsSources(model="cli", max_cost="cli")

    cli._handle_config(
        "/config",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-6",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
        settings=settings,
        sources=sources,
    )
    line = next(l for l in capsys.readouterr().err.splitlines() if "model" in l and "gpt-6" in l)
    assert "(cli)" in line


def test_handle_config_bare_falls_back_to_resolve_when_not_threaded(tmp_path, monkeypatch, capsys):
    """The threading is optional so host tests can call this bare -- that path
    must still print rather than crash on the None pair."""
    monkeypatch.chdir(tmp_path)
    cli._handle_config(
        "/config",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert "pre-spinup" in capsys.readouterr().err


def test_reprice_tracker_repoints_rates_and_name(monkeypatch):
    """Regression: CostTrackerMiddleware caches pricing/rates/model name at
    construction, so before this every turn after `/config set model` was billed
    at the LAUNCH model's rates and reported under the launch model's name."""
    tracker = cli.build_cost_tracker("openai:gpt-5.5", max_cost=1.0, max_tokens=None)
    assert tracker is not None
    before = tracker.bare_model

    cli._reprice_tracker(tracker, "anthropic:claude-haiku-4-5")
    assert tracker.bare_model != before
    assert tracker.bare_model == "claude-haiku-4-5"
    # Budgets and accumulated spend survive: a budget is a session ceiling, not
    # a per-model one.
    assert tracker._max_cost == 1.0


def test_reprice_tracker_none_says_tracking_stays_off(capsys):
    """A session launched on an unpriced model has no tracker at all (M1's
    null=MVP contract) and one can't be added mid-session without under-counting
    the run -- so say so instead of switching silently."""
    cli._reprice_tracker(None, "openai:gpt-5.5")
    err = capsys.readouterr().err
    assert "cost tracking is off" in err and "openai:gpt-5.5" in err


def test_config_set_model_reprices_the_tracker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tracker = cli.build_cost_tracker("openai:gpt-5.5", max_cost=1.0, max_tokens=None)
    assert tracker is not None

    cli._handle_config(
        "/config set model anthropic:claude-haiku-4-5",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic=None,
        tracker=tracker,
        hitl_conf=None,
        rebuild_agent=lambda spec: f"agent-{spec}",
        edited=set(),
    )
    assert tracker.bare_model == "claude-haiku-4-5"
