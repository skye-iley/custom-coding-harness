"""Guards the one-directional harness import contract.

`harness.cost` holds the cost/energy math and must NEVER import another harness
module — above all `harness.providers`. The dependency runs providers -> cost, so
if cost ever imported providers (or anything that does) the two would form a
cycle (docs/milestones/milestone1.md §2.4 / CLAUDE.md "Cost / token / energy
tracking"). This is the invariant under test, and it holds in every environment.

Note this does NOT forbid langchain/langgraph: cost.py optionally imports the
real AgentMiddleware base and falls back to `object` when langchain is absent (so
the pure-math tests still import it on a bare host). In the runtime image langchain
*is* present, so it legitimately shows up in sys.modules — that's a soft dep, not
a cycle, and asserting against it would be environment-dependent.

Each check runs in a fresh subprocess so imports leaked into THIS process by
other tests (which do pull providers) can't mask a real violation. The subprocess
loads cost.py exactly the way _bootstrap does: by file path, under a bare
`harness` package, so harness/__init__.py never runs.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_HARNESS = Path(__file__).resolve().parent.parent / "harness"

# cost.py must pull in NO sibling harness module — that's the acyclic guard, true
# in every environment. providers is the headline case; the rest would each imply
# a back-edge into cost.
_FORBIDDEN = (
    "harness.providers",
    "harness.cli",
    "harness.agent",
    "harness.hooks",
    "harness.loaders",
    "harness.sync_models",
)


def _modules_after_importing_cost() -> set[str]:
    """Import harness.cost in a clean interpreter; return its sys.modules keys."""
    script = textwrap.dedent(
        f"""
        import importlib.util, sys, types
        from pathlib import Path
        harness_dir = Path(r{str(_HARNESS)!r})
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(harness_dir)]
        sys.modules["harness"] = pkg
        spec = importlib.util.spec_from_file_location("harness.cost", harness_dir / "cost.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["harness.cost"] = mod
        spec.loader.exec_module(mod)
        # Print every top-level module name now loaded, one per line.
        print("\\n".join(sorted({{m.split(".")[0] for m in sys.modules}} | set(sys.modules))))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


def test_cost_imports_no_sibling_harness_module():
    loaded = _modules_after_importing_cost()
    offenders = [m for m in _FORBIDDEN if m in loaded]
    assert not offenders, (
        f"harness.cost pulled in sibling modules {offenders}; the import must "
        "stay one-directional (providers -> cost) so the two can't form a cycle."
    )


def test_cost_imports_cleanly_in_subprocess():
    # Belt-and-suspenders: the import itself must succeed with exit 0 (the helper
    # uses check=True, so a failed import would raise CalledProcessError here).
    assert "harness" in _modules_after_importing_cost()
