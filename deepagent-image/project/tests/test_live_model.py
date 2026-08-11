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
