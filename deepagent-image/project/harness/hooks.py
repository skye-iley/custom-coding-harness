"""hooks.json lifecycle hooks.

Two scopes:
  session.start / session.end  fire once in cli.main() around the whole run.
  everything else              fire per agent/model/tool event, via the
                               ShellHooksMiddleware below.
"""

from __future__ import annotations

import subprocess

from langchain.agents.middleware.types import AgentMiddleware

# Events fired once in main() around the whole run (process/session scope),
# NOT per agent invocation. Everything else is per-turn / per-call middleware.
SESSION_EVENTS = ("session.start", "session.end")


def _run_hook_commands(commands: list[str]) -> None:
    # shell=True is intentional: hooks.json declares shell commands. Trust it.
    for command in commands:
        subprocess.run(command, shell=True, check=False)


class ShellHooksMiddleware(AgentMiddleware):
    """Run hooks.json shell commands on agent/model/tool lifecycle events.

    Scope (see LangChain middleware execution flow):
      agent.start / agent.end  -> once per user input (.invoke), via before/after_agent
      model.start / model.end  -> once per LLM call (fires on every reasoning step)
      tool.start / tool.end    -> once per tool call, around each tool execution

    Session-scoped hooks (session.start/session.end) are NOT handled here; they
    fire once in main() around the whole run.
    """

    def __init__(self, by_event: dict[str, list[str]]):
        super().__init__()
        self._by_event = by_event

    def before_agent(self, state, runtime):
        _run_hook_commands(self._by_event.get("agent.start", []))

    def after_agent(self, state, runtime):
        _run_hook_commands(self._by_event.get("agent.end", []))

    def before_model(self, state, runtime):
        _run_hook_commands(self._by_event.get("model.start", []))

    def after_model(self, state, runtime):
        _run_hook_commands(self._by_event.get("model.end", []))

    def wrap_tool_call(self, request, handler):
        _run_hook_commands(self._by_event.get("tool.start", []))
        try:
            return handler(request)
        finally:
            _run_hook_commands(self._by_event.get("tool.end", []))


def build_hook_middleware(by_event: dict[str, list[str]]) -> list[AgentMiddleware]:
    """Middleware for the non-session events (empty if none declared)."""
    if any(event not in SESSION_EVENTS for event in by_event):
        return [ShellHooksMiddleware(by_event)]
    return []
