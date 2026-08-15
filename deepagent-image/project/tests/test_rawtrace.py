"""Tests for harness/rawtrace.py — Milestone 7 S1 (the sink and its renderers).

Host tier: stdlib only, no langchain, no model, no network. Everything that needs
the agent runtime (the middleware's seam position, the turn bracket, the console
substitution) lives in ``test_agent.py`` / ``test_cli.py``.

The rule the whole milestone rests on: **a trace that disagrees with what the
model received is worse than no trace**, because it is trusted. Every fidelity
case below exists to make that disagreement a test failure rather than a
debugging dead end — which is why the sharpest ones assert what is *not*
dropped (invariants 5a–5c) rather than what is present.
"""

from __future__ import annotations

import json
from pathlib import Path

from _bootstrap import _load

rt = _load("harness.rawtrace")


# --- helpers: duck-typed stand-ins for the langchain objects -------------------
#
# The sink reads shapes, never types (invariant 21 keeps langchain out of this
# module), so a namespace with the right attributes is a faithful stand-in.


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _sink(tmp_path, mode=rt.MODE_FILE, **kw):
    return rt.TraceSink(mode, tmp_path / "trace.log", run_id="run-1", env={}, **kw)


def _drive(sink, body):
    sink.open_record("HEADER")
    sink.append(body)
    sink.close_record()


# --- fidelity: bodies verbatim, additions structural only (invariants 1-2) ----


def test_system_and_message_bodies_are_verbatim():
    request = _Obj(
        system_message="line one\nline two  with   spacing",
        messages=[_Obj(type="human", content="do the thing\n\tindented")],
        tools=[],
        model="ollama:gemma4",
    )
    out = rt.format_request(request)
    assert "line one\nline two  with   spacing" in out
    assert "do the thing\n\tindented" in out
    # Additions are structural only: rules and counts, no commentary.
    assert "--- system ---" in out
    assert "--- messages (1) ---" in out
    assert "--- tools (0) ---" in out


def test_a_system_message_object_renders_its_text_not_its_repr():
    """Regression: `ModelRequest.system_message` is a SystemMessage object on the
    real path, and rendering it straight put the entire prompt inside a `repr`
    (``content=[{'type': 'text', 'text': 'You are...``) — nothing dropped, but
    not verbatim, and unreadable exactly where readability is the point. Caught
    by running a real model, not by a stub."""
    prompt = "You are an expert coding assistant.\nRule two."
    obj = _Obj(content=[{"type": "text", "text": prompt}], type="system")
    request = _Obj(system_message=obj, messages=[], tools=[])
    out = rt.format_request(request)
    assert prompt in out
    assert "content=[" not in out


def test_a_plain_string_system_message_still_passes_through():
    request = _Obj(system_message="just a prompt", messages=[], tools=[])
    assert "just a prompt" in rt.format_request(request)


def test_nothing_is_truncated_however_long():
    long_result = "x" * 50_000
    request = _Obj(
        system_message="s",
        messages=[_Obj(type="tool", content=long_result, name="read_file")],
        tools=[],
    )
    # A trace that elides the long tool result hides the reason the model got
    # confused, which is the whole point of reading one.
    assert long_result in rt.format_request(request)


def test_tool_schemas_are_recorded_literally():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    request = _Obj(
        system_message="s",
        messages=[],
        tools=[{"name": "write_file", "parameters": schema}],
    )
    out = rt.format_request(request)
    assert "write_file:" in out
    assert '"properties"' in out and '"path"' in out


def test_header_names_the_fidelity_level():
    # milestone7.md §14 "false confidence": an operator may read an L1 record as
    # the model server's template-rendered token string. The header says which.
    header = rt.format_header(run_id="r", turn=3, call=2, model="ollama:gemma4", ts="TS")
    assert "run r | turn 3 | call 2 | TS | ollama:gemma4" in header
    assert "fidelity=" in header and "message-level" in header


# --- fidelity: nothing on the response is dropped (invariant 5a) ---------------


def test_response_records_every_block_in_order_with_index_and_type():
    message = _Obj(
        content=[
            {"type": "reasoning", "reasoning": "I should read the file"},
            {"type": "text", "text": "Here is the answer"},
        ],
        tool_calls=[],
        response_metadata={"finish_reason": "stop"},
        usage_metadata={"input_tokens": 5},
        additional_kwargs={},
    )
    out = rt.format_response(_Obj(result=[message]), elapsed_ms=1284)

    assert "[block 0] reasoning:" in out
    assert "I should read the file" in out
    assert "[block 1] text:" in out
    assert out.index("[block 0]") < out.index("[block 1]")
    assert "finish_reason=stop" in out


def test_response_records_the_metadata_bags_and_raw_tool_call_args():
    # Providers put refusals, safety verdicts, logprobs and model-version drift
    # in these, and none of it reaches a human today.
    message = _Obj(
        content="ok",
        tool_calls=[{"name": "write_file", "args": {"path": "a.py"}, "id": "call_1"}],
        invalid_tool_calls=[{"name": "write_file", "args": '{"path": ', "error": "unterminated"}],
        response_metadata={"finish_reason": "tool_calls", "model": "gemma4"},
        usage_metadata={"input_tokens": 11, "output_tokens": 3},
        additional_kwargs={"refusal": None},
    )
    out = rt.format_response(_Obj(result=[message]), elapsed_ms=5)

    assert "--- tool_calls (1) ---" in out and "call_1" in out
    # The malformed JSON, pre-repair, is the direct answer to "why did this local
    # model's tool call do nothing" — so it gets its own section.
    assert "--- invalid_tool_calls (1) ---" in out
    assert "unterminated" in out
    for key in ("usage_metadata", "response_metadata", "additional_kwargs"):
        assert f"{key}:" in out


def test_unknown_block_types_are_dumped_never_skipped():
    # Invariant 5b: a block type nobody anticipated is the most interesting thing
    # that can appear in a trace; silence about it is the one unrecoverable
    # failure. Contrast final_message_text, which drops exactly this.
    block = {"type": "quantum_flux", "payload": {"nested": [1, 2]}, "n": 7}
    out = rt.format_response(_Obj(result=[_Obj(content=[block])]), elapsed_ms=1)
    assert "[block 0] quantum_flux:" in out
    assert "quantum_flux" in out and "nested" in out and "7" in out


def test_a_known_block_with_extra_keys_keeps_the_extras():
    out = rt.format_block(0, {"type": "text", "text": "hi", "annotations": ["a"]})
    assert "hi" in out
    assert "annotations" in out  # a provider extension must not vanish


def test_a_bare_string_content_is_one_block():
    assert rt.format_content("just text") == "just text"
    assert "[block 0] text:" in rt.format_block(0, "just text")


# --- fidelity: reasoning, encrypted or not (invariant 5c) ---------------------


def test_plaintext_reasoning_is_recorded_verbatim_in_position():
    blocks = [
        {"type": "thinking", "thinking": "step one\nstep two"},
        {"type": "text", "text": "answer"},
    ]
    out = rt.format_content(blocks)
    assert "step one\nstep two" in out
    assert out.index("[block 0]") < out.index("[block 1]")


def test_encrypted_reasoning_is_a_placeholder_with_type_and_size_never_ciphertext():
    ciphertext = "A" * 2481
    out = rt.format_block(1, {"type": "redacted_thinking", "data": ciphertext})
    assert "[block 1] <encrypted reasoning block: type=redacted_thinking, 2481 bytes>" == out
    # Position, type and size — never the payload. It is unreadable by
    # construction, and kilobytes of base64 would bury the readable blocks.
    assert ciphertext not in out


def test_an_encrypted_block_is_never_omitted_even_mid_list():
    blocks = [
        {"type": "text", "text": "before"},
        {"type": "encrypted_reasoning", "ciphertext": "zz"},
        {"type": "text", "text": "after"},
    ]
    out = rt.format_content(blocks)
    # A trace in which reasoning happened but nothing marks the spot is a false
    # negative — the failure this invariant exists to prevent.
    assert "[block 1] <encrypted reasoning block:" in out
    assert out.index("before") < out.index("[block 1]") < out.index("after")


# --- the three-phase writer (invariant 23) ------------------------------------


def test_one_shot_and_n_appends_produce_identical_bytes(tmp_path):
    one = rt.TraceSink(rt.MODE_FILE, tmp_path / "one.log", run_id="r", env={})
    one.open_record("H")
    one.append("A\nB\nC")
    one.close_record()

    many = rt.TraceSink(rt.MODE_FILE, tmp_path / "many.log", run_id="r", env={})
    many.open_record("H")
    for chunk in ("A", "B", "C"):
        many.append(chunk)
    many.close_record()

    # An API that only works when the whole body is known in advance is exactly
    # what a streaming implementation would have to rewrite (milestone7.md §9).
    assert (tmp_path / "one.log").read_bytes() == (tmp_path / "many.log").read_bytes()


def test_close_record_writes_the_footer(tmp_path):
    sink = _sink(tmp_path)
    _drive(sink, "body")
    assert (tmp_path / "trace.log").read_text(encoding="utf-8").rstrip().endswith("=====")


# --- destinations (invariants 9-10) -------------------------------------------


def test_trace_path_is_under_the_state_dir(tmp_path):
    assert rt.trace_path(tmp_path, "run-9") == tmp_path / "raw-trace" / "run-9.log"


def test_off_mode_writes_nothing_anywhere(tmp_path, capsys):
    sink = _sink(tmp_path, mode=rt.MODE_OFF)
    _drive(sink, "body")
    assert not (tmp_path / "trace.log").exists()
    assert capsys.readouterr().out == ""


def test_file_mode_writes_the_file_and_prints_nothing(tmp_path, capsys):
    sink = _sink(tmp_path, mode=rt.MODE_FILE)
    _drive(sink, "body")
    assert "body" in (tmp_path / "trace.log").read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""


def test_console_mode_prints_and_creates_no_file(tmp_path, capsys):
    sink = _sink(tmp_path, mode=rt.MODE_CONSOLE)
    _drive(sink, "body")
    assert "body" in capsys.readouterr().out
    assert not (tmp_path / "trace.log").exists()


def test_both_mode_destinations_carry_identical_content(tmp_path, capsys):
    sink = _sink(tmp_path, mode=rt.MODE_BOTH)
    _drive(sink, "body\nmore")
    printed = capsys.readouterr().out
    on_disk = (tmp_path / "trace.log").read_text(encoding="utf-8")
    assert printed == on_disk


# --- the cap (invariant 11) ---------------------------------------------------


def test_the_file_cap_stops_writing_and_announces_itself_once(tmp_path, capsys):
    sink = _sink(tmp_path, cap_bytes=200)
    for _ in range(5):
        _drive(sink, "y" * 100)
    text = (tmp_path / "trace.log").read_text(encoding="utf-8")
    assert text.count("[raw-trace] cap reached") == 1
    assert len(text) < 500  # stopped, rather than filling the disk


def test_the_run_continues_after_the_cap(tmp_path):
    sink = _sink(tmp_path, cap_bytes=50)
    for _ in range(3):
        _drive(sink, "z" * 100)  # must not raise
    assert sink._capped is True


def test_console_output_is_uncapped(tmp_path, capsys):
    # A cap here would silently hide the thing the operator asked to see.
    sink = _sink(tmp_path, mode=rt.MODE_CONSOLE, cap_bytes=10)
    for _ in range(3):
        _drive(sink, "q" * 100)
    assert capsys.readouterr().out.count("q" * 100) == 3


# --- containment (invariants 13-14, 16) ---------------------------------------


def test_scrub_runs_on_every_section_before_the_file_is_written(tmp_path):
    env = {"OPENAI_API_KEY": "sk-livesecretvalue12345"}
    sink = rt.TraceSink(rt.MODE_FILE, tmp_path / "t.log", run_id="r", env=env)
    request = _Obj(
        system_message="key is sk-livesecretvalue12345",
        messages=[_Obj(type="human", content="use sk-livesecretvalue12345 please")],
        tools=[],
    )
    _drive(sink, rt.format_request(request))
    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "sk-livesecretvalue12345" not in text
    # Visible, not silent: a reader can tell altered text from text the model
    # genuinely saw.
    assert text.count("***REDACTED***") >= 2


def test_scrub_runs_on_the_console_path_too(tmp_path, capsys):
    env = {"ANTHROPIC_API_KEY": "supersecretvalue123"}
    sink = rt.TraceSink(rt.MODE_CONSOLE, None, run_id="r", env=env)
    _drive(sink, "token supersecretvalue123 here")
    out = capsys.readouterr().out
    # The two destinations must not disagree about what is safe.
    assert "supersecretvalue123" not in out
    assert "***REDACTED***" in out


def test_a_sink_failure_never_raises_and_warns_once(tmp_path, capsys):
    # A read-only state dir or a full disk must not kill the run.
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    sink = rt.TraceSink(rt.MODE_FILE, blocked / "sub" / "t.log", run_id="r", env={})
    for _ in range(3):
        _drive(sink, "body")  # must not raise
    assert capsys.readouterr().err.count("raw-trace:") == 1


# --- import profile (invariant 21) --------------------------------------------


def test_rawtrace_imports_stdlib_plus_scrub_only():
    source = (Path(rt.__file__)).read_text(encoding="utf-8")
    harness_imports = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("from harness", "import harness"))
    ]
    # The middleware, which needs the langchain base, lives in agent.py. Same
    # split telemetry.py vs cli.TelemetryMiddleware uses, for the same reason:
    # this module has to stay in the host test tier.
    assert harness_imports == ["from harness.scrub import scrub"]
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from ")) and " import " in line
    ]
    assert not [line for line in imports if "langchain" in line or "deepagents" in line]
