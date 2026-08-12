"""Thin entrypoint shim. Real code lives in the `harness/` package.

Kept so `python3 main.py` (Dockerfile CMD, run-docker scripts) still works.
`python3 -m harness` is the equivalent package entry. See harness/__init__.py.
"""

import sys

from harness.entry import dispatch

if __name__ == "__main__":
    # dispatch() also routes the `sync-models` dev subcommand, so
    # `python3 main.py sync-models` behaves like `python3 -m harness sync-models`.
    # It lives in harness.entry (stdlib-only) so keyless subcommands route
    # without loading cli.py's runtime stack -- milestone5.md §0.1 F6.
    sys.exit(dispatch(sys.argv[1:]))
