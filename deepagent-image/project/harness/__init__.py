"""Deep Agents coding harness package.

Split out of the original single-file main.py for navigation:

  providers.py  model/provider registry + selection + chat-model resolution
  loaders.py    optional-file IO: AGENTS.md text, .mcp.json tools, hooks.json
  workflows.py  §3 workflow engine: folder format + gates + side-effect steps,
                WorkflowMiddleware (agent/model/tool events); hooks.json precursor
  agent.py      workspace resolution, system prompt, build_agent, result text
  resilience.py Milestone 3 P1: retry/backoff + context-overflow classification
  interrupt.py  Milestone 3 S1: interrupt request model, render, headless policy
  config.py     Milestone 3 S2: .harness-config.yaml + review_triggers matching
  audit.py      Milestone 3 S7: scrubbed interrupt audit trail (interrupts.jsonl)
  hitl.py       Milestone 3 glue: interrupt resume loop, pause gate, ask_human
  cli.py        argparse + main() (the run loop, checkpointer, session workflows)
  mask.py       Milestone 4 resolver: gitignore-parity matcher, 3-tier policy,
                designated-secret floor, snapshot + protection-reduction checks
  mask_scan.py  Milestone 4 CLI wrapper for mask resolution (mask-scan subcommand)
  pathguard.py  Milestone 4 defense-in-depth traversal check (commonpath guard)
  doctor.py     Milestone 4 pre-flight config validation (doctor subcommand)

Entry points (both run cli.main from WORKDIR /project):
  python3 main.py        thin shim at the package parent
  python3 -m harness     via __main__.py
"""

from harness.cli import main

__all__ = ["main"]
