import sys

from harness.cli import main

if __name__ == "__main__":
    # Subcommand dispatch kept minimal: the default (no subcommand) is the agent
    # run loop. `sync-models` is a dev-time registry refresh (needs API keys +
    # network), not part of the sealed runtime — see harness/sync_models.py.
    if len(sys.argv) > 1 and sys.argv[1] == "sync-models":
        from harness.sync_models import sync_models_main

        sys.exit(sync_models_main(sys.argv[2:]))
    sys.exit(main())
