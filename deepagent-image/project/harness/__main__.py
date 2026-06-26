import sys

from harness.cli import dispatch

if __name__ == "__main__":
    # Subcommand dispatch lives in cli.dispatch so this and main.py share it.
    # Default (no subcommand) is the agent run loop; `sync-models` is a dev-time
    # registry refresh (needs API keys + network), not part of the sealed
    # runtime — see harness/sync_models.py.
    sys.exit(dispatch(sys.argv[1:]))
