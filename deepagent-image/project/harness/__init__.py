"""Deep Agents coding harness package.

Split out of the original single-file main.py for navigation:

  providers.py  model/provider registry + selection + chat-model resolution
  loaders.py    optional-file IO: AGENTS.md text, .mcp.json tools, hooks.json
  workflows.py  §3 workflow engine: folder format + gates + side-effect steps,
                WorkflowMiddleware (agent/model/tool events); hooks.json precursor
  agent.py      workspace resolution, system prompt, build_agent, result text
  resilience.py Milestone 3 P1: retry/backoff + context-overflow classification
  interrupt.py  Milestone 3 S1: interrupt request model, render, headless policy
  config.py     Milestone 3 S2: .harness-config.yaml + review_triggers matching,
                Milestone 5's Settings resolver (every run knob, one precedence
                chain: CLI > env > .harness-profile.yaml > default), and
                Milestone 5.1's FIELD_SPECS registry -- the single declaration
                every knob's profile I/O, resolution, display, /config dispatch
                and wizard screen derives from
  config_cli.py Milestone 5 C6/C7: `harness config` / `harness config security`
                keyless pre-spinup wizard + one-shot show/set
  audit.py      Milestone 3 S7: scrubbed interrupt audit trail (interrupts.jsonl)
  hitl.py       Milestone 3 glue: interrupt resume loop, pause gate, ask_human
  cli.py        argparse + main() (the run loop, checkpointer, session workflows)
  entry.py      subcommand routing for both entry points; stdlib-only, so a
                keyless subcommand never imports cli.py (Milestone 5 §0.1 F6)
  mask.py       Milestone 4 resolver: gitignore-parity matcher, 3-tier policy,
                designated-secret floor, snapshot + protection-reduction checks
  mask_scan.py  Milestone 4 CLI wrapper for mask resolution (mask-scan subcommand)
  pathguard.py  Milestone 4 defense-in-depth traversal check (commonpath guard)
  doctor.py     Milestone 4 pre-flight config validation (doctor subcommand)

Entry points (both route through entry.dispatch from WORKDIR /project):
  python3 main.py        thin shim at the package parent
  python3 -m harness     via __main__.py
"""

__all__ = ["main"]


def __getattr__(name: str):
    """Resolve `harness.main` lazily (Milestone 5 §0.1 F6).

    This used to be a module-level `from harness.cli import main`, which made
    *any* `harness.*` import execute cli.py and therefore load dotenv, langgraph
    and deepagents -- so `harness config` / `harness doctor`, which need none of
    them, could not run on a host without the full runtime stack, and the test
    suite had to route around this file entirely (see tests/_bootstrap.py).

    Must raise AttributeError for everything else, not return a placeholder:
    `from harness import config` asks the package for the attribute first and
    only falls back to importing the submodule when that raises AttributeError.
    Swallowing the miss here would break every sibling import in the package.
    """
    if name == "main":
        from harness.cli import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
