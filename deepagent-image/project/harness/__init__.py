"""Deep Agents coding harness package.

Split out of the original single-file main.py for navigation:

  providers.py  model/provider registry + selection + chat-model resolution
  loaders.py    optional-file IO: AGENTS.md text, .mcp.json tools, hooks.json
  workflows.py  §3 workflow engine: folder format + gates + side-effect steps,
                WorkflowMiddleware (agent/model/tool events); hooks.json precursor
  agent.py      workspace resolution, system prompt, build_agent, result text
  cli.py        argparse + main() (the run loop, checkpointer, session workflows)

Entry points (both run cli.main from WORKDIR /project):
  python3 main.py        thin shim at the package parent
  python3 -m harness     via __main__.py
"""

from harness.cli import main

__all__ = ["main"]
