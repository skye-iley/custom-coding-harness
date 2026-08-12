"""Subcommand routing for both entry points, with no runtime-stack dependency.

Moved out of `cli.py` (Milestone 5 §0.1 F6). The routes below have always
imported their target module lazily, but that was defeated by *where* the
function lived: reaching a lazy import inside `cli.py` first executes `cli.py`,
whose module top pulls dotenv, langgraph (`SqliteSaver`) and — via
`harness.agent` — deepagents. So `python3 main.py config` loaded the whole
runtime stack to run a wizard that needs none of it.

This module imports stdlib only, and `main.py` / `__main__.py` route through it,
so the keyless subcommands (`config`, `doctor`, `mask-scan`, `threads`/`past`,
`seccomp-sync`, `apparmor-sync`) reach their stdlib-only modules without
`cli.py` ever being imported. Only the default route — the agent loop — pays for
the runtime stack. The other half of F6 is `harness/__init__.py`'s lazy
`__getattr__`; either one alone leaves a path that still drags cli in.

`cli.dispatch` re-exports this, so it remains the same callable it always was.
"""

from __future__ import annotations


def dispatch(argv: list[str]) -> int:
    """Shared entry for both `python3 main.py` and `python3 -m harness`.

    Routes the optional dev-time `sync-models` subcommand; anything else runs
    the agent loop. Kept in one place so the two entry points can't drift —
    previously only `-m harness` handled `sync-models` and `main.py sync-models`
    silently swallowed it as an agent task.

    Every import here is function-local **and** must stay that way: a
    module-level import of a routed module would re-import its dependencies for
    every subcommand, and a module-level `harness.cli` import would undo this
    module's entire reason for existing.
    """
    if argv and argv[0] == "sync-models":
        from harness.sync_models import sync_models_main

        return sync_models_main(argv[1:])
    if argv and argv[0] in ("threads", "past"):
        # Keyless lifecycle admin over the two sqlite stores (Milestone 2 §2.6).
        from harness.memadmin import memadmin_main

        return memadmin_main(argv)
    if argv and argv[0] == "mask-scan":
        from harness.mask_scan import mask_scan_main

        return mask_scan_main(argv[1:])
    if argv and argv[0] == "doctor":
        from harness.doctor import doctor_main

        return doctor_main(argv[1:])
    if argv and argv[0] == "config":
        # Milestone 5, C6/C7: keyless pre-spinup config wizard. A separate module
        # (not cli.py's REPL-side /config) so it stays dependency-light -- no
        # deepagents/langgraph/langchain pulled in just to run the wizard.
        from harness.config_cli import config_main

        return config_main(argv[1:])
    if argv and argv[0] == "seccomp-sync":
        from harness.seccomp import seccomp_sync_main

        return seccomp_sync_main(argv[1:])
    if argv and argv[0] == "apparmor-sync":
        from harness.apparmor import apparmor_sync_main

        return apparmor_sync_main(argv[1:])
    # The agent loop, and the only route that needs the runtime stack. Imported
    # as a module attribute rather than bound at import time so a test that
    # monkeypatches `cli.main` still sees its stub here.
    from harness import cli

    return cli.main()
