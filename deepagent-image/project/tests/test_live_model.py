"""Live-model tier: real prompts to a real model, real replies asserted.

Off unless `DEEPAGENTS_LIVE_MODEL=1` (see the `live_model` marker + fixture in
conftest.py). Everything else in the suite stubs the model, which is right for
determinism but structurally blind to a whole class of bug: the harness can be
internally consistent and still not work, because the *model* does not behave
the way the stub does. These cases cover the properties the harness silently
depends on and a stub always satisfies for free.

Run against the shipped default (a local ollama model, no quota to burn):

    DEEPAGENTS_LIVE_MODEL=1 python3 -m pytest tests/test_live_model.py -v

Point `DEEPAGENTS_MODEL` elsewhere to check another provider.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live_model


def test_resolved_model_answers(live_model):
    """The spec `choose_model` picks resolves to something that actually replies.

    The narrowest possible end-to-end check on model routing: registry parse →
    prefix match → client construction → a reply with text in it. A routing
    regression that no unit test catches (a wrong prefix, a base_url that isn't
    read, a client built but never pointed anywhere) fails right here.
    """
    reply = live_model.invoke("Reply with the single word: pong")
    assert isinstance(reply.content, str) and reply.content.strip()


def test_model_reports_usage_metadata(live_model):
    """A real reply carries the token counts the cost tracker bills from.

    `cost.py` reads `usage_metadata.input_tokens` / `output_tokens` off the last
    AIMessage and treats a missing value as 0. Stubs always populate it, so a
    provider that omits it reads as a free session rather than an error — a
    silently wrong ledger, which is worse than a loud one. Assert the real
    provider supplies it.
    """
    reply = live_model.invoke("Say hello.")
    usage = getattr(reply, "usage_metadata", None)
    assert usage, "no usage_metadata on the reply — cost tracking would bill $0"
    assert int(usage.get("input_tokens") or 0) > 0
    assert int(usage.get("output_tokens") or 0) > 0


def test_registry_options_reach_the_client(live_model):
    """The registry's `[options]` land on the constructed client.

    This is the payoff for the `[options]` table: one Ollama tag serves every
    context size, because per-request options override the tag's Modelfile
    PARAMETER block. Worth a live case because the failure is silent — a dropped
    `num_ctx` doesn't error, it just quietly truncates context, and the model
    forgets the top of the conversation instead of saying anything.
    """
    _load_providers = __import__("_bootstrap", fromlist=["_load"])._load
    providers = _load_providers("harness.providers")
    spec = providers.choose_model(None)
    expected = providers.resolve_model_options(spec)
    if "num_ctx" not in expected:
        pytest.skip(f"no num_ctx declared for {spec!r}")
    assert getattr(live_model, "num_ctx", None) == expected["num_ctx"], (
        "num_ctx did not reach the client — the model would silently run at the "
        "Modelfile default and truncate context"
    )


def test_model_can_emit_tool_calls(live_model):
    """The model can actually call a bound tool.

    The whole ReAct loop rests on this, and no stubbed test can check it: a
    local model that ignores its tool schemas produces an agent that chats
    politely and edits nothing. That failure looks like a bad prompt, not a
    model-capability problem, which is exactly why it is worth an explicit case.
    """
    if not hasattr(live_model, "bind_tools"):
        pytest.skip(f"{type(live_model).__name__} has no bind_tools")

    def get_weather(city: str) -> str:
        """Get the current weather for a city."""
        return "sunny"

    bound = live_model.bind_tools([get_weather])
    reply = bound.invoke("What is the weather in Paris? Use the tool.")
    calls = getattr(reply, "tool_calls", None) or []
    assert calls, (
        "model emitted no tool_calls — it cannot drive the ReAct loop. "
        "Pick a tool-capable tag (ollama: check the model's template)."
    )
    assert calls[0]["name"] == "get_weather"


# --- Milestone 6: telemetry against a real model ------------------------------


def test_a_real_turn_produces_a_decomposable_record(live_model, tmp_path):
    """One real turn through a real agent leaves a record whose wall clock
    decomposes, with non-zero tokens and a model_ms that is a real fraction of
    duration_ms.

    Worth a live case for the reason the whole tier exists: a stub populates
    `usage_metadata` however the test wrote it, so a harness that records zeros
    forever reads green. `usage_metadata` is exactly the field providers have
    silently omitted before -- and telemetry parses it independently of the cost
    tracker, which M1 does not even build on the default (free) local model, so
    this is the only place that path is exercised end to end.

    It also pins the decomposition on real timings rather than injected ones:
    model_ms and tool_ms come from separate seams, and a real turn is where a
    double-count or a missed seam would actually show.
    """
    from _bootstrap import _load

    pytest.importorskip("deepagents")
    agent_mod = _load("harness.agent")
    cli = _load("harness.cli")
    tm = _load("harness.telemetry")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    telemetry = cli.TelemetryMiddleware(
        tm.usage_path(tmp_path / "state"),
        run_id="run-live-1",
        thread_id="live",
        topic="live-telemetry",
        provider="live",
        model=str(getattr(live_model, "model", "unknown")),
    )
    agent = agent_mod.build_agent(live_model, workspace, middleware=[telemetry])

    cli.run_turn(agent, "Reply with the single word: pong", {}, telemetry=telemetry)

    records = tm.read_records(telemetry.sink)
    assert len(records) == 1, "a completed turn must leave exactly one record"
    rec = records[0]

    assert rec["failed"] is False
    assert rec["model_calls"] >= 1
    assert rec["input"] > 0, (
        "no input tokens on a real turn -- usage_metadata did not reach telemetry, "
        "which would report every real run as free"
    )
    assert rec["output"] > 0

    # The decomposition, on real timings.
    components = (
        rec["model_ms"] + rec["tool_ms"] + rec["retry_sleep_ms"]
        + rec["paced_sleep_ms"] + rec["hitl_wait_ms"]
    )
    assert components <= rec["duration_ms"], "the components must not exceed the whole"
    assert rec["model_ms"] > 0, "model time was not measured at its seam"
    # A real fraction, not a rounding artefact: the model call dominates a turn
    # this small, so anything under a tenth means the seam is not really wired.
    assert rec["model_ms"] >= 0.1 * rec["duration_ms"]

    # No cost tracker on the default free local model -- the field must stay NULL
    # rather than claim the run was free (invariant 5).
    assert rec["cost_usd"] is None

    summary = tm.derive_session(records, run_id="run-live-1")
    assert summary["tokens"]["total"] == (
        rec["input"] + rec["output"] + rec["cache_read"] + rec["cache_write"]
    )
    assert summary["time"]["residual_ms"] >= 0


@pytest.mark.live_model
def test_a_real_turn_does_not_silently_discard_generated_tokens(live_model, tmp_path):
    """The regression for milestone7.md §3.1, and a stub cannot hold it.

    Measured 2026-08-17: `langchain-ollama` 1.1.0 captures Ollama's
    `message.thinking` only when `reasoning` is truthy, so a thinking-by-default
    tag produced 450 output tokens and an `AIMessage` with `content=""` and
    `additional_kwargs={}`. The turn rendered blank, no error anywhere, and every
    stubbed test stayed green — a stub populates `usage_metadata` and `content`
    from the same fixture, so the two can never disagree. Only a real call can.

    Asserts the invariant, not the fix: tokens the model reports generating must
    be reachable in SOME field. A future client version, a new default, or a
    deleted registry entry all break that the same way.
    """
    reply = live_model.invoke("Reply with the single word: pineapple")

    produced = (reply.usage_metadata or {}).get("output_tokens") or 0
    if produced <= 0:
        pytest.skip("this provider reports no output_tokens; nothing to reconcile")

    reachable = bool(
        (reply.content if isinstance(reply.content, str) else reply.content)
        or getattr(reply, "tool_calls", None)
        or getattr(reply, "invalid_tool_calls", None)
        or getattr(reply, "additional_kwargs", None)
    )
    assert reachable, (
        f"the model reports {produced} output tokens but every field of the message "
        "is empty -- the provider's parser dropped generated text before the harness "
        "could see it (milestone7.md §3.1). Check the tag's `reasoning` option."
    )


def test_a_real_turn_leaves_a_faithful_raw_trace(live_model, tmp_path):
    """The one thing a stub cannot check: that the trace describes something real.

    A stubbed test proves the sink can format whatever the test handed it. It
    cannot catch a trace that is internally consistent and describes nothing —
    the wrong tool list, a reply the turn never used, a response shape the
    renderer silently drops. So this asserts the record against the *same
    objects the turn actually used*:

    * the recorded tool schemas are the tools the agent was really built with;
    * the recorded reply is the reply `run_turn` returned;
    * reasoning/thinking blocks, when a model emits them, appear in the trace
      and **not** in `final_message_text`'s output — which is the entire point
      of the milestone (`agent.py`'s extractor drops them by design).
    """
    from _bootstrap import _load

    pytest.importorskip("deepagents")
    agent_mod = _load("harness.agent")
    cli = _load("harness.cli")
    rawtrace = _load("harness.rawtrace")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("the file says pineapple\n", encoding="utf-8")

    trace = cli.RawTraceMiddleware(rawtrace.TraceSink(
        rawtrace.MODE_FILE, rawtrace.trace_path(tmp_path / "state", "run-live-trace"),
        run_id="run-live-trace",
    ))
    agent = agent_mod.build_agent(live_model, workspace, raw_trace=trace)

    answer = cli.run_turn(
        agent, "Read hello.txt and tell me the one unusual word in it.", {},
        raw_trace=trace,
    )
    text = trace.sink.path.read_text(encoding="utf-8", errors="replace")

    assert "===== run run-live-trace | turn 1 | call 1" in text
    assert "--- system ---" in text and agent_mod.BASE_SYSTEM_PROMPT[:40] in text
    # Verbatim, not a `repr` of the SystemMessage that happens to CONTAIN the
    # prompt — the bug a real turn surfaced and a substring check would miss.
    system_section = text.split("--- system ---", 1)[1].split("--- messages", 1)[0]
    assert "content=[" not in system_section

    # The recorded tools are the tools the model was really offered. A trace one
    # middleware layer out would list tools the model never received.
    assert "--- tools (" in text
    assert "read_file" in text, "the fs tools the agent was built with are missing"

    # The recorded reply is the reply the turn used.
    if answer:
        first_line = answer.strip().splitlines()[0][:40]
        assert first_line in text, "the recorded response is not the one the turn returned"

    # Reasoning, if this model emits any, is in the trace and not in the answer --
    # UNLESS reasoning was the entire output, the one case where showing it beats
    # rendering the turn as nothing (milestone7.md §3.1). That case announces itself
    # with the marker, so it is distinguishable rather than an exception that
    # swallows the leak this asserts against.
    if "] reasoning:" in text or "] thinking:" in text:
        marker = "] reasoning:" if "] reasoning:" in text else "] thinking:"
        body = text.split(marker, 1)[1].splitlines()[1].strip()
        reasoning_only = agent_mod._REASONING_ONLY_NOTE in (answer or "")
        if body and answer and not reasoning_only:
            assert body not in answer, (
                "final_message_text leaked a reasoning block into the answer -- "
                "it is supposed to drop them, which is why the raw trace exists"
            )
