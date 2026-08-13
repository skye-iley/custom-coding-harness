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
    # A test asserting *default* resolution must not inherit the ambient env: a
    # DEEPAGENTS_MODEL exported in the shell (or `-e`'d into a dev container) is a
    # legitimate tier the resolver honours, so it would make ns.model non-None and
    # fail this for reasons that have nothing to do with the code.
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
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


# --- _env_float ------------------------------------------------------------
# (`_env_int` went with `_env_defaults` in M5 C2 -- Settings casts int fields now.)

def test_env_float_present(monkeypatch):
    monkeypatch.setenv("X", "1.5")
    assert cli._env_float("X") == 1.5


def test_env_float_absent_or_empty(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert cli._env_float("X") is None
    monkeypatch.setenv("X", "")
    assert cli._env_float("X") is None


def test_env_float_malformed_raises_systemexit(monkeypatch):
    monkeypatch.setenv("X", "abc")
    with pytest.raises(SystemExit):
        cli._env_float("X")


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
    # Host-side save: with DEEPAGENTS_IN_CONTAINER unset there is no mount to
    # reason about, so a missing profile is just a first write. (In the test
    # image the var IS set, hence the explicit delenv -- see
    # test_handle_config_save_without_mount_refuses for that path.)
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)
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


def test_handle_config_save_without_mount_refuses(tmp_path, monkeypatch, capsys):
    """In-container with no profile file => run-docker never mounted one (it
    mounts only `if exists` on the host), so the write would land in the --rm
    layer and vanish. Refuse and say so, rather than print success."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    cli._handle_config(
        "/config save",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-6",
        topic="my-topic",
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited={"model", "topic"},
    )
    assert not (tmp_path / cfg.PROFILE_NAME).exists()
    err = capsys.readouterr().err
    assert "no .harness-profile.yaml is mounted" in err
    assert "wrote" not in err


def test_handle_config_save_with_mount_still_writes_in_container(tmp_path, monkeypatch):
    """The refusal keys on *no mount*, not on being in a container -- a mounted
    profile must still be writable from the REPL (that's what the read-write
    mount is for)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPAGENTS_IN_CONTAINER", "1")
    (tmp_path / cfg.PROFILE_NAME).write_text("model: openai:gpt-5.5\n", encoding="utf-8")
    cli._handle_config(
        "/config save",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-6",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited={"model"},
    )
    assert cfg.load_profile(tmp_path / cfg.PROFILE_NAME)["model"] == "openai:gpt-6"


def test_handle_config_save_readonly_target_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    """Under DEEPAGENTS_JAIL=1 /project is read-only, so save_profile's in-place
    fallback raises OSError too. A REPL command must never end the session."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPAGENTS_IN_CONTAINER", raising=False)

    def boom(path, values):
        raise OSError("Read-only file system")

    monkeypatch.setattr(cli, "save_profile", boom)
    model, new_agent, topic = cli._handle_config(
        "/config save",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-6",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited={"model"},
    )
    assert (model, new_agent, topic) == ("openai:gpt-6", None, None)
    err = capsys.readouterr().err
    assert "could not write" in err and "read-only" in err


# --- /config set reaches the past.sqlite row (F7) ------------------------------


def test_handle_config_set_topic_updates_archive_row(tmp_path, monkeypatch):
    """`/topic` and `/config set topic` change the same knob, so they must
    persist the same way -- otherwise `harness past list --topic` files the run
    under the launch topic."""
    monkeypatch.chdir(tmp_path)
    conn = archive.connect(tmp_path / "past.sqlite")
    archive.start_session(conn, "run-1", "t", "openai", "gpt-5.5", topic="launch-topic")

    cli._handle_config(
        "/config set topic new-lane",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic="launch-topic",
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
        archive_conn=conn,
        run_id="run-1",
    )
    assert archive.get_topic(conn, "run-1") == "new-lane"


def test_handle_config_set_model_updates_archive_row(tmp_path, monkeypatch):
    """Otherwise the ledger attributes every post-switch turn to the launch model."""
    monkeypatch.chdir(tmp_path)
    conn = archive.connect(tmp_path / "past.sqlite")
    archive.start_session(conn, "run-1", "t", "openai", "gpt-5.5")

    cli._handle_config(
        "/config set model openai:gpt-6",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=lambda spec: f"agent-for-{spec}",
        edited=set(),
        archive_conn=conn,
        run_id="run-1",
    )
    row = conn.execute("SELECT provider, model FROM sessions WHERE run_id='run-1'").fetchone()
    assert (row["provider"], row["model"]) == ("openai", "gpt-6")


def test_handle_config_set_topic_bare_clears_it(tmp_path, monkeypatch):
    """A topic could be set but never cleared: `_parse_config_set_args` rejected
    a bare field outright."""
    monkeypatch.chdir(tmp_path)
    conn = archive.connect(tmp_path / "past.sqlite")
    archive.start_session(conn, "run-1", "t", "openai", "gpt-5.5", topic="launch-topic")

    _model, _agent, topic = cli._handle_config(
        "/config set topic",
        config={"configurable": {"thread_id": "t"}},
        current_model="openai:gpt-5.5",
        topic="launch-topic",
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
        archive_conn=conn,
        run_id="run-1",
    )
    assert topic is None
    assert archive.get_topic(conn, "run-1") is None


def test_handle_config_without_archive_conn_still_works(tmp_path, monkeypatch):
    """Archive off (or a bare host-side call) => the archive writes are skipped,
    not attempted."""
    monkeypatch.chdir(tmp_path)
    _model, _agent, topic = cli._handle_config(
        "/config set topic solo",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert topic == "solo"


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


# =============================================================================
# Milestone 5.1: /config's lists and dispatch derive from the registry
# =============================================================================


def test_config_settable_fields_derived_from_registry():
    assert cli._CONFIG_SETTABLE_FIELDS == tuple(
        s.name for s in cfg.FIELD_SPECS if s.settable
    )


def test_config_hitl_validators_derived_from_choices():
    """`_CONFIG_HITL_VALIDATORS` was the only place any field's valid values
    were written down (milestone5.1.md §3.1). It is now a view of `choices`."""
    assert cli._CONFIG_HITL_VALIDATORS == {
        s.name: s.choices
        for s in cfg.FIELD_SPECS
        if s.name.startswith("hitl.") and s.choices
    }


def test_config_unsettable_fields_derived_from_nullable():
    assert cli._CONFIG_UNSETTABLE_FIELDS == tuple(
        s.name for s in cfg.FIELD_SPECS if s.nullable
    )


def test_every_settable_field_has_an_applier_and_vice_versa():
    """The one hand-maintained pairing left after R4 (an applier mutates the
    tracker/archive/agent, which config.py must not import), so it is guarded in
    both directions: a settable field with no applier would KeyError at dispatch
    time, and an applier naming no field is dead code."""
    assert set(cli._LIVE_APPLIERS) == {s.name for s in cfg.FIELD_SPECS if s.settable}


def test_non_settable_live_field_is_rejected_by_config_set():
    """`hitl` itself is live but not individually settable -- it is a whole
    file. Its dotted sub-fields are the settable surface."""
    assert "hitl" not in cli._CONFIG_SETTABLE_FIELDS
    with pytest.raises(ValueError, match="unknown field"):
        cli._parse_config_set_args(["hitl", "guided"])


# --- R6: the arrow-key picker for enum fields ----------------------------------


def test_arrow_select_takes_a_plain_options_list(monkeypatch):
    """R6 widened `_arrow_select` from an InterruptRequest to a bare list. The
    HITL `choose` path still works because the caller now passes req.options."""
    import inspect

    params = list(inspect.signature(cli._arrow_select).parameters)
    assert params[0] == "options"
    assert "header" in params


def test_config_set_bare_enum_field_opens_the_picker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hitl_conf = cfg.HitlSection(autonomy_level="guided")
    seen = {}

    def fake_select(options, *, header=None):
        seen["options"] = list(options)
        seen["header"] = header
        return "strict"

    monkeypatch.setattr(cli, "_arrow_select", fake_select)
    edited = set()
    cli._handle_config(
        "/config set hitl.autonomy_level",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=hitl_conf,
        rebuild_agent=None,
        edited=edited,
    )
    assert seen["options"] == list(cfg.AUTONOMY_LEVELS)
    assert seen["header"] == "Autonomy level:"
    assert hitl_conf.autonomy_level == "strict"
    assert "hitl.autonomy_level" in edited


def test_config_set_bare_enum_field_falls_back_when_picker_declines(tmp_path, monkeypatch, capsys):
    """Esc / no prompt_toolkit / non-TTY => the picker returns None and the
    typed path's usage error is unchanged."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_arrow_select", lambda options, header=None: None)
    hitl_conf = cfg.HitlSection(autonomy_level="guided")
    cli._handle_config(
        "/config set hitl.autonomy_level",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=hitl_conf,
        rebuild_agent=None,
        edited=set(),
    )
    assert hitl_conf.autonomy_level == "guided"
    assert "usage: /config set <field> <value>" in capsys.readouterr().err


def test_config_set_bare_free_text_field_does_not_open_the_picker(tmp_path, monkeypatch, capsys):
    """`model` has no `choices` (an open set), so a bare set stays a usage
    error rather than a picker over nothing (fork 4: no history picker)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "_arrow_select", lambda *a, **k: pytest.fail("picker must not open for free text")
    )
    cli._handle_config(
        "/config set model",
        config={"configurable": {"thread_id": "t"}},
        current_model="m",
        topic=None,
        tracker=None,
        hitl_conf=None,
        rebuild_agent=None,
        edited=set(),
    )
    assert "usage: /config set <field> <value>" in capsys.readouterr().err


# =============================================================================
# Milestone 6 — telemetry capture (T2/T3/T5)
# =============================================================================

import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

tm = _load("harness.telemetry")


class _StubResult(dict):
    pass


class _OkAgent:
    """Agent whose invoke succeeds, returning a minimal final-message result."""

    def __init__(self):
        self.calls = 0

    def invoke(self, *args, **kwargs):
        self.calls += 1
        return {"messages": [type("M", (), {"content": "done", "type": "ai"})()]}


def _telemetry(tmp_path, **over):
    kwargs = dict(run_id="run-test-1", thread_id="thread-1", topic="t",
                  provider="ollama", model="gemma4")
    kwargs.update(over)
    return cli.TelemetryMiddleware(tmp_path / "usage.jsonl", **kwargs)


# --- the removable contract (invariant 20) ------------------------------------


def test_telemetry_off_builds_no_middleware(tmp_path):
    settings = cfg.Settings(telemetry=False)
    assert cli.build_telemetry(
        settings, tmp_path, run_id="r", thread_id="t", topic=None,
        provider=None, model=None,
    ) is None


def test_telemetry_on_by_default(tmp_path):
    assert cli.build_telemetry(
        cfg.Settings(), tmp_path, run_id="r", thread_id="t", topic=None,
        provider=None, model=None,
    ) is not None


def test_telemetry_off_writes_no_sink_and_no_summary(tmp_path, monkeypatch):
    """Invariant 20: nothing on disk, and run_turn behaves exactly as before."""
    monkeypatch.setenv("DEEPAGENTS_STATE_DIR", str(tmp_path / "state"))
    cli.run_turn(_OkAgent(), "hi", {}, telemetry=None)
    cli._write_session_summary(None, tmp_path, started="x", elapsed_ms=1.0)
    assert not (tmp_path / "state").exists()


def test_every_added_parameter_defaults_to_the_inert_value():
    """§15.1: the removable contract is structural, not policed by tests. Every
    signature this milestone widened must default telemetry to None."""
    import inspect

    for fn in (cli.run_turn, cli.run_repl, cli.run_batch, cli._invoke_resilient,
               cli._run_turn_hitl):
        param = inspect.signature(fn).parameters["telemetry"]
        assert param.default is None, f"{fn.__name__} does not default telemetry to None"


# --- capture (invariants 1, 2, 2a, 4, 4a) --------------------------------------


def test_one_record_per_completed_turn(tmp_path):
    t = _telemetry(tmp_path)
    agent = _OkAgent()
    for _ in range(3):
        cli.run_turn(agent, "hi", {}, telemetry=t)
    records = tm.read_records(t.sink)
    assert [r["turn"] for r in records] == [1, 2, 3]
    assert all(r["failed"] is False for r in records)
    assert all(r["run_id"] == "run-test-1" for r in records)


def test_a_failed_turn_is_still_recorded(tmp_path, monkeypatch):
    """Invariant 2 — the record an operator most wants and the one an exception
    path most easily drops. run_turn has no general `except`, so this pins that
    the write happens in its `finally`."""
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "0")
    t = _telemetry(tmp_path)
    with pytest.raises(RuntimeError):
        cli.run_turn(_BoomAgent(), "hi", {}, telemetry=t)
    records = tm.read_records(t.sink)
    assert len(records) == 1
    assert records[0]["failed"] is True


def test_failed_turn_recorded_on_the_headless_path_too(tmp_path, monkeypatch):
    """Invariant 2 pins BOTH turn paths: run_repl and run_batch each carry their
    own general `except`, so a test that only drives the REPL leaves headless —
    the benchmark path — unproven."""
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "0")
    t = _telemetry(tmp_path)
    rc = cli.run_batch(_BoomAgent(), {}, ["do it"], telemetry=t)
    assert rc == 1
    records = tm.read_records(t.sink)
    assert len(records) == 1 and records[0]["failed"] is True


def test_repl_path_records_a_failed_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "0")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    t = _telemetry(tmp_path)
    assert cli.run_repl(_BoomAgent(), {}, "do it", telemetry=t) == 0
    assert [r["failed"] for r in tm.read_records(t.sink)] == [True]


def test_operator_deny_is_not_a_failure(tmp_path):
    """Invariant 2a: conflating "the human said no" with "the turn broke" would
    put a governance signal in the reliability column, and a sweep reading
    `turns_failed` would be measuring the operator."""
    hitl_mod = _load("harness.hitl")

    class _HaltAgent:
        def invoke(self, *a, **k):
            raise hitl_mod.HaltTurn(
                type("TM", (), {"content": "blocked"})(), "execute"
            )

        def update_state(self, *a, **k):
            return None

    t = _telemetry(tmp_path)
    assert cli.run_turn(_HaltAgent(), "hi", {}, telemetry=t) is None
    records = tm.read_records(t.sink)
    assert len(records) == 1 and records[0]["failed"] is False
    assert records[0]["outcome"] == tm.OUTCOME_DENIED


def test_governance_stops_are_outcomes_not_failures(tmp_path, monkeypatch):
    """Invariant 2a applied to the other three events that arrive as exceptions.

    A budget cap, an operator Ctrl-C and a fail-closed headless abort are each the
    harness doing what it was configured to do. The deny case above was already
    excluded from `turns_failed` on exactly this reasoning while these three were
    counted — an inconsistency a sweep run under `--max-cost` would have read as
    harness unreliability on every capped instance.
    """
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "0")
    hitl_mod = _load("harness.hitl")
    cost_mod = _load("harness.cost")

    cases = [
        (cost_mod.BudgetExceeded("max cost"), tm.OUTCOME_BUDGET),
        (KeyboardInterrupt(), tm.OUTCOME_CANCELLED),
        (hitl_mod.InterruptAborted(None, "no default"), tm.OUTCOME_ABORTED),
        (RuntimeError("provider 500"), tm.OUTCOME_ERROR),
    ]
    for exc, expected in cases:
        class _Agent:
            def invoke(self, *a, **k):
                raise exc

        t = _telemetry(tmp_path / expected)
        with pytest.raises(type(exc)):
            cli.run_turn(_Agent(), "hi", {}, telemetry=t)
        record = tm.read_records(t.sink)[0]
        assert record["outcome"] == expected, exc
        assert record["failed"] is (expected == tm.OUTCOME_ERROR), exc


def test_sink_failure_never_breaks_a_turn(tmp_path, capsys):
    """Invariant 3 — same rule audit.py and the /config dispatch already follow."""
    t = _telemetry(tmp_path)
    t.sink = _Path(tmp_path / "usage.jsonl")

    def boom(*a, **k):
        raise OSError("no space left on device")

    import harness.telemetry as _tmmod
    original = _tmmod.record_turn
    _tmmod.record_turn = boom
    try:
        assert cli.run_turn(_OkAgent(), "hi", {}, telemetry=t) == "done"
        cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    finally:
        _tmmod.record_turn = original
    err = capsys.readouterr().err
    # One warning per RUN, not per turn.
    assert err.count("telemetry: no space left on device") == 1


def test_duration_brackets_the_whole_turn(tmp_path):
    """Invariant 4: wall clock around the turn, tool execution included — not
    model latency. Pinned with a deliberately slow invoke, because the field's
    *meaning* is what makes it useful or misleading and both look identical in
    a JSON file."""
    import time as _time

    class _SlowAgent:
        def invoke(self, *a, **k):
            _time.sleep(0.05)
            return {"messages": [type("M", (), {"content": "ok", "type": "ai"})()]}

    t = _telemetry(tmp_path)
    cli.run_turn(_SlowAgent(), "hi", {}, telemetry=t)
    assert tm.read_records(t.sink)[0]["duration_ms"] >= 50


def test_retry_sleep_is_recorded_not_absorbed(tmp_path, monkeypatch):
    """Invariant 4c. `retry_call` takes `sleep=` as a parameter specifically so
    the caller owns it; passing bare time.sleep is what loses the number."""
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "2")
    monkeypatch.setenv("DEEPAGENTS_RETRY_BASE", "0.02")
    t = _telemetry(tmp_path)
    with pytest.raises(RuntimeError):
        cli.run_turn(_BoomAgent(), "hi", {}, telemetry=t)
    rec = tm.read_records(t.sink)[0]
    assert rec["retry_count"] == 2
    assert rec["retry_sleep_ms"] > 0


def test_context_overflow_trim_is_flagged(tmp_path, monkeypatch):
    """Invariant 4d: a trimmed turn is not comparable to an untrimmed one, and a
    benchmark that mixes them silently is measuring two different things."""
    class _OverflowThenOk:
        def __init__(self):
            self.calls = 0

        def invoke(self, inputs, config=None):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("maximum context length exceeded for this model")
            return {"messages": [type("M", (), {"content": "ok", "type": "ai"})()]}

    t = _telemetry(tmp_path)
    cli.run_turn(
        _OverflowThenOk(), "hi", {},
        extra_messages=[{"role": "user", "content": "recalled"}],
        telemetry=t,
    )
    assert tm.read_records(t.sink)[0]["context_trimmed"] is True


# --- the tool seam (invariants 4e, 4g) ----------------------------------------


class _Req:
    def __init__(self, name):
        self.tool_call = {"name": name, "args": {}}


def test_tool_work_is_recorded_by_name(tmp_path):
    """Invariant 4e: `request.tool_call`, the same field PauseMiddleware reads.
    Reading a top-level `tool_name` is a bug this repo has already shipped once."""
    t = _telemetry(tmp_path)
    t.acc.reset()
    t.wrap_tool_call(_Req("read_file"), lambda r: "ok")
    t.wrap_tool_call(_Req("read_file"), lambda r: "ok")
    t.wrap_tool_call(_Req("execute"), lambda r: "ok")
    assert t.acc.tool_calls == {"read_file": 2, "execute": 1}
    assert t.acc.tool_errors == 0


def test_tool_error_status_counts_as_an_error(tmp_path):
    t = _telemetry(tmp_path)
    t.acc.reset()
    t.wrap_tool_call(_Req("execute"), lambda r: type("TM", (), {"status": "error"})())
    assert t.acc.tool_errors == 1


def test_raising_tool_counts_as_an_error(tmp_path):
    t = _telemetry(tmp_path)
    t.acc.reset()

    def boom(_r):
        raise RuntimeError("tool blew up")

    with pytest.raises(RuntimeError):
        t.wrap_tool_call(_Req("execute"), boom)
    assert t.acc.tool_errors == 1 and t.acc.tool_calls == {"execute": 1}


def test_hitl_control_flow_is_not_a_tool_error(tmp_path):
    """The probe's real finding (milestone6_spec.md §3.1): telemetry is the OUTER
    wrap_tool_call wrapper, and PauseMiddleware's gate raises GraphInterrupt /
    HaltTurn straight through it. Counting those as tool errors would put every
    gated call in the reliability column."""
    hitl_mod = _load("harness.hitl")
    from langgraph.errors import GraphInterrupt

    t = _telemetry(tmp_path)
    t.acc.reset()

    def suspend(_r):
        raise GraphInterrupt(())

    def halt(_r):
        raise hitl_mod.HaltTurn(type("TM", (), {"content": "x"})(), "execute")

    with pytest.raises(GraphInterrupt):
        t.wrap_tool_call(_Req("execute"), suspend)
    with pytest.raises(hitl_mod.HaltTurn):
        t.wrap_tool_call(_Req("execute"), halt)
    assert t.acc.tool_errors == 0
    # And no count: neither entry reached the tool. A gated call re-enters this
    # wrapper on resume, and only that entry may increment the mix.
    assert t.acc.tool_calls == {}


def test_tool_ms_excludes_a_suspended_gate(tmp_path):
    """Invariant 4g, in the composition this repo actually has: the human wait
    happens in run_interrupt_loop, after invoke returns, so it cannot land in
    tool_ms — and the prompt-building time before the suspend must not either."""
    import time as _time
    from langgraph.errors import GraphInterrupt

    t = _telemetry(tmp_path)
    t.acc.reset()

    def slow_suspend(_r):
        _time.sleep(0.05)
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        t.wrap_tool_call(_Req("execute"), slow_suspend)
    assert t.acc.tool_ms == 0


# --- independence from the cost tracker (invariants 4f, 5) --------------------


def test_no_tracker_records_null_cost_not_zero(tmp_path):
    """Invariant 5 / 4f: on `ollama:gemma4` (pricing = "free", the default
    provider and the local-benchmark case) M1 appends NO tracker, and telemetry
    must still record tokens and timings with cost_usd null."""
    t = _telemetry(tmp_path)
    assert t.tracker is None
    cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    rec = tm.read_records(t.sink)[0]
    assert rec["cost_usd"] is None and rec["cost_provenance"] is None
    assert rec["energy_wh"] is None
    assert rec["duration_ms"] >= 0


def test_tokens_use_the_same_split_as_the_ledger_row(tmp_path):
    """Invariant 7's precondition: `input` must mean *fresh* input on both sides.
    `_split_tokens` splits cache-read tokens OUT of input, and that split is what
    UsageAccumulator stores and _cost_totals_for_row writes to the past.sqlite
    row. Hand-rolling usage["input_tokens"] here makes invariant 7 fail as
    arithmetic that looks like a telemetry bug."""
    t = _telemetry(tmp_path)
    t.acc.reset()
    usage = {
        "input_tokens": 1000,
        "output_tokens": 50,
        "input_token_details": {"cache_read": 300, "cache_creation": 100},
    }
    t.acc.add_tokens(usage)
    acc = cost.UsageAccumulator()
    acc.add(usage, cost.Free(), "m")
    assert (t.acc.input, t.acc.output, t.acc.cache_read, t.acc.cache_write) == (
        acc.input, acc.output, acc.cache_read, acc.cache_write
    )
    assert t.acc.input == 600  # fresh input, cache split OUT


# --- the session summary (invariants 6, 8, 9) ---------------------------------


def test_summary_is_written_whether_or_not_the_archive_is_on(tmp_path, monkeypatch):
    """Invariant 8's second half: the _finalize_session call sits inside
    `if archive_conn is not None:`, and the summary write must sit OUTSIDE it or
    DEEPAGENTS_ARCHIVE=0 silently produces no summary."""
    state = tmp_path / "state"
    monkeypatch.setenv("DEEPAGENTS_STATE_DIR", str(state))
    t = _telemetry(tmp_path, run_id="run-sum")
    t.sink = tm.usage_path(state)
    cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    cli._write_session_summary(t, tmp_path, started="2026-01-01T00:00:00Z", elapsed_ms=1234)
    summary = tm.read_session(tm.session_path(state))
    assert summary is not None
    assert summary["run_id"] == "run-sum" and summary["turns"] == 1
    assert summary["duration_ms"] == 1234


def test_summary_totals_equal_the_records(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("DEEPAGENTS_STATE_DIR", str(state))
    t = _telemetry(tmp_path)
    t.sink = tm.usage_path(state)
    for _ in range(2):
        cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    cli._write_session_summary(t, tmp_path, started="s", elapsed_ms=5000)
    summary = tm.read_session(tm.session_path(state))
    records = tm.read_records(t.sink)
    assert summary["turns"] == len(records) == 2
    assert summary["time"]["model_ms"] == sum(r["model_ms"] for r in records)


def test_summary_write_failure_does_not_end_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPAGENTS_STATE_DIR", str(tmp_path / "state"))
    t = _telemetry(tmp_path)

    import harness.telemetry as _tmmod
    original = _tmmod.write_session
    _tmmod.write_session = lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs"))
    try:
        cli._write_session_summary(t, tmp_path, started="s", elapsed_ms=1)
    finally:
        _tmmod.write_session = original
    assert "failed to write session summary" in capsys.readouterr().err


# --- the headless join (invariant 23) -----------------------------------------


def test_batch_payload_carries_the_join_keys(tmp_path):
    t = _telemetry(tmp_path, topic="django__django-11099")
    payload = cli._batch_payload("done", {"configurable": {"thread_id": "th"}},
                                 None, tmp_path, 0, t)
    assert payload["run_id"] == "run-test-1"
    assert payload["topic"] == "django__django-11099"
    assert payload["usage_log"] == str(t.sink)
    # additive: the existing keys keep their names and meanings
    assert payload["thread_id"] == "th"
    assert payload["exit_code"] == 0


def test_batch_payload_join_keys_are_null_not_absent_when_telemetry_is_off(tmp_path):
    """A driver reading payload["run_id"] should get a null it can test, not a
    KeyError that looks like a schema change."""
    payload = cli._batch_payload("done", {"configurable": {"thread_id": "th"}},
                                 None, tmp_path, 0, None)
    assert payload["run_id"] is None
    assert payload["topic"] is None and payload["usage_log"] is None


def test_headless_run_emits_the_join_keys_on_stdout(tmp_path, capsys):
    t = _telemetry(tmp_path)
    cli.run_batch(_OkAgent(), {"configurable": {"thread_id": "th"}}, ["go"], telemetry=t)
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["run_id"] == "run-test-1"
    assert payload["usage_log"] == str(t.sink)


# --- a live /config change reaches the next record ----------------------------


def test_topic_change_applies_from_the_next_turn(tmp_path):
    t = _telemetry(tmp_path, topic="old")
    cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    cli._retopic_telemetry(t, "new")
    cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    assert [r["topic"] for r in tm.read_records(t.sink)] == ["old", "new"]


def test_model_change_is_split_into_provider_and_model(tmp_path):
    t = _telemetry(tmp_path)
    cli._remodel_telemetry(t, "ollama:gemma4")
    assert (t.provider, t.model) == ("ollama", "gemma4")


# --- the dispatch route (invariant 22) ----------------------------------------


def test_dispatch_routes_telemetry(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    t = _telemetry(tmp_path)
    t.sink = tm.usage_path(state)
    cli.run_turn(_OkAgent(), "hi", {}, telemetry=t)
    rc = cli.dispatch(["telemetry", "show", "--state-dir", str(state)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run-test-1" in out
    assert "derived from 1 turn record" in out  # says which source it used


def test_streamed_turn_is_still_recorded(tmp_path):
    """--stream bypasses the resilience layer and the HITL loop, but it is still a
    completed turn. Invariant 1 says one record per turn, and a mode that silently
    produced none would be a hole shaped exactly like a bug."""

    class _StreamAgent:
        def stream(self, inputs, config=None):
            yield {"event": "one"}
            yield {"event": "two"}

    t = _telemetry(tmp_path)
    assert cli.run_turn(_StreamAgent(), "hi", {}, stream=True, telemetry=t) is None
    records = tm.read_records(t.sink)
    assert len(records) == 1
    assert records[0]["failed"] is False and records[0]["turn"] == 1


# --- the turn boundary is run_turn, not before_agent --------------------------
#
# Regression guard for a bug this milestone shipped into review once:
# `before_agent` fires once per INVOKE, not per turn, and a turn can invoke
# several times (a resilience retry; every HITL resume). Resetting the
# accumulator there silently wiped everything measured before the last invoke.


def test_before_agent_does_not_reset_the_accumulator(tmp_path):
    t = _telemetry(tmp_path)
    t.begin_turn()
    t.acc.add_tool("read_file", 0.01, error=False)
    t.acc.add_retry_sleep(500)
    t.acc.retry_count = 1
    # A second invoke inside the same turn fires the hooks again.
    assert not hasattr(t, "before_agent") or t.before_agent(None, None) is None
    assert t.acc.tool_calls == {"read_file": 1}
    assert t.acc.retry_sleep_ms == 500
    assert t.acc.retry_count == 1


def test_retry_numbers_survive_the_re_invoke(tmp_path, monkeypatch):
    """The real shape of the bug: retry sleeps are accumulated BETWEEN invokes,
    so a reset on the next invoke's before_agent erases them."""
    monkeypatch.setenv("DEEPAGENTS_MAX_RETRIES", "2")
    monkeypatch.setenv("DEEPAGENTS_RETRY_BASE", "0.02")

    class _BoomThenBoom:
        """Fires the middleware hooks on every invoke, like the real graph does."""

        def __init__(self, telemetry):
            self.telemetry = telemetry

        def invoke(self, *a, **k):
            self.telemetry.before_model(None, None)
            raise RuntimeError("500 INTERNAL")

    t = _telemetry(tmp_path)
    with pytest.raises(RuntimeError):
        cli.run_turn(_BoomThenBoom(t), "hi", {}, telemetry=t)
    rec = tm.read_records(t.sink)[0]
    assert rec["retry_count"] == 2, "retry count was reset by a mid-turn hook"
    assert rec["retry_sleep_ms"] > 0, "retry sleep was reset by a mid-turn hook"
    assert rec["model_calls"] == 3, "each invoke's model span must survive the retry"


def test_turn_cost_is_a_session_delta_not_tracker_turn(tmp_path):
    """`tracker.turn` has the same per-invoke reset defect, so the per-turn cost
    is read as a delta against `tracker.session`, which is never reset. That also
    makes the per-turn costs sum to the session total by construction -- which is
    what invariant 7 actually needs."""
    tracker = cost.CostTrackerMiddleware(cost.Free(), "m")
    t = _telemetry(tmp_path)
    t.tracker = tracker

    usage = {"input_tokens": 100, "output_tokens": 10}
    t.begin_turn()
    tracker.turn = cost.UsageAccumulator()  # what before_agent does, mid-turn
    tracker.session.add(usage, cost.Free(), "m")
    tracker.turn = cost.UsageAccumulator()  # a second invoke resets it again
    tracker.session.add(usage, cost.Free(), "m")

    delta = cli._session_delta(tracker, t._cost_at_start)
    assert delta.input == 200, "the turn saw two invokes; tracker.turn would report one"
    assert delta.output == 20


def test_per_turn_costs_sum_to_the_session_total(tmp_path):
    tracker = cost.CostTrackerMiddleware(
        cost.RateTable({"m": cost.ModelRates(input=1.0, output=2.0)}), "m"
    )
    t = _telemetry(tmp_path)
    t.tracker = tracker
    usage = {"input_tokens": 1000, "output_tokens": 500}

    for _ in range(3):
        t.begin_turn()
        tracker.session.add(usage, tracker._pricing, "m", rates=tracker._rates)
        rec = t.build_record(duration_ms=1, outcome=tm.OUTCOME_OK)
        tm.record_turn(t.sink, rec)

    records = tm.read_records(t.sink)
    assert sum(r["cost_usd"] for r in records) == pytest.approx(tracker.session.cost, rel=1e-9)
    assert sum(r["input"] for r in records) == 0  # tokens come from the model seam, not the tracker
