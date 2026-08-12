import sys

from harness.entry import dispatch

if __name__ == "__main__":
    # Subcommand dispatch lives in entry.dispatch so this and main.py share it —
    # and so a keyless subcommand routes without importing cli.py's runtime stack
    # (milestone5.md §0.1 F6). cli.dispatch re-exports the same function.
    # Default (no subcommand) is the agent run loop; `sync-models` is a dev-time
    # registry refresh (needs API keys + network), not part of the sealed
    # runtime — see harness/sync_models.py.
    sys.exit(dispatch(sys.argv[1:]))
