"""Shared lazy harness-module loader for the test suite.

Imports a single `harness.*` submodule by file path WITHOUT triggering
`harness/__init__.py`. That used to be load-bearing on a bare host --
`__init__.py` did an eager `from harness.cli import main`, so any package import
pulled dotenv/langgraph/deepagents -- until M5 §0.1 F6 made `main` lazy. The
remaining reason is the same one that always applied in the test image (FROM
runtime, all deps present): this loader is *lazy* in import **timing**.
`test_providers_load_pricing_from_registry` must import
`harness.providers` only AFTER the `provider_registry` fixture sets
`DEEPAGENTS_PROVIDERS_DIR` (the registry loads at import time). A module-top
`import harness.providers` would bind the live registry before the fixture runs.

Deduplicated here (was copied byte-for-byte into both test modules) so the two
can't drift.

A loaded submodule is cached in `sys.modules` and returned on later calls — so
`harness.cost` is a single module object across every test file. That matters
because the pricing test does `isinstance(p.pricing, cost.RateTable)`, and a
re-execution would mint a *new* `RateTable` class that the cross-module instance
would not match. Caching keeps the harness modules singletons, like a normal
import. "Lazy" here means import *timing* (providers is loaded only when a test
calls `_load`, after the fixture sets DEEPAGENTS_PROVIDERS_DIR) — not repeated
re-execution.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# tests/_bootstrap.py -> parent is tests/, parent.parent is the project root that
# holds harness/. Same anchor the per-file copies used.
_HARNESS = Path(__file__).resolve().parent.parent / "harness"


def _load(modname: str) -> types.ModuleType:
    """Load `harness.<sub>` (or `harness.<pkg>.<sub>`) by file path, registering
    bare package objects along the way so no `__init__.py` ever runs. Returns the
    cached module on repeat calls so each harness submodule is a singleton (see
    module docstring).

    Sub-packages (`harness.bench.patch`) get the same treatment `harness` itself
    does -- a bare package object with a `__path__` and no code -- for the same
    reason: running a real `__init__.py` is what would pull whatever it imports,
    and the point of this loader is to import one module and nothing else.
    """
    cached = sys.modules.get(modname)
    if cached is not None:
        return cached
    if "harness" not in sys.modules:
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(_HARNESS)]  # mark as a package
        sys.modules["harness"] = pkg

    # A bare name ("seccomp") is the long-standing spelling for a top-level
    # harness module and still works; only a dotted name walks sub-packages.
    parts = modname.split(".")
    if parts[0] != "harness":
        parts = ["harness", *parts]
    directory = _HARNESS
    for i, part in enumerate(parts[1:-1], start=2):
        directory = directory / part
        name = ".".join(parts[:i])
        if name not in sys.modules:
            sub = types.ModuleType(name)
            sub.__path__ = [str(directory)]
            sys.modules[name] = sub

    spec = importlib.util.spec_from_file_location(
        modname, directory / f"{parts[-1]}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod
