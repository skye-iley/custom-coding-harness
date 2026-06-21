"""Thin entrypoint shim. Real code lives in the `harness/` package.

Kept so `python3 main.py` (Dockerfile CMD, run-docker scripts) still works.
`python3 -m harness` is the equivalent package entry. See harness/__init__.py.
"""

import sys

from harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
