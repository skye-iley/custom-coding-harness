"""Guards the one-directional harness import contract, and the keyless-path
contract F6 added on top of it (milestone5.md §0.1 F6).

Two separate invariants live here:

1. `harness.cost` must never import a sibling (the acyclic guard, below).
2. The **keyless** modules — `config_cli`, `doctor`, `entry`, and the `harness`
   package itself — must import without pulling the runtime stack, so
   `harness config` / `harness doctor` run on a host that has no langgraph or
   deepagents installed. Unlike the cost guard, these load through the *real*
   package (`import harness.config_cli`), because `harness/__init__.py` running
   or not is exactly what is under test.

`harness.cost` holds the cost/energy math and must NEVER import another harness
module — above all `harness.providers`. The dependency runs providers -> cost, so
if cost ever imported providers (or anything that does) the two would form a
cycle (docs/milestones/complete/milestone1.md §2.4 / CLAUDE.md "Cost / token / energy
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


# --- the keyless path stays keyless (milestone5.md §0.1 F6) -----------------

_PROJECT_ROOT = _HARNESS.parent

# The runtime stack a keyless subcommand must not drag in.
#
# langchain AND langgraph are deliberately absent from this list. cost.py
# soft-imports `langchain.agents.middleware.types` and falls back to `object`
# when it is missing, and in the image langchain's agent middleware is itself
# built on langgraph — so `import harness.config_cli` legitimately shows both
# there (via config_cli -> providers -> cost) while showing neither on a bare
# host. Asserting against them would fail in the image for a reason that has
# nothing to do with F6. What stays forbidden is everything the *harness*
# controls: the two modules that pull the stack unconditionally, plus the two
# hard deps only they have.
#
# Both environments still catch a regression. In the image, re-eagerizing the
# import puts `harness.cli` in sys.modules. On a bare host it fails harder:
# there is no langgraph to import at all, so the subprocess dies and check=True
# turns that into a failure here — which is exactly what `harness config` did
# on a dev host before this fix.
_RUNTIME_STACK = ("deepagents", "dotenv", "harness.cli", "harness.agent")


def _modules_after(statement: str) -> set[str]:
    """Run `statement` in a clean interpreter rooted at project/; return sys.modules.

    This one imports through the real package on purpose — no bare-`harness`
    shim like the cost helper uses — because whether `harness/__init__.py` pulls
    the runtime stack is the property under test.
    """
    # Concatenated rather than an indented f-string template: a multi-line
    # `statement` interpolated into one would leave its first line indented and
    # the rest flush, which textwrap.dedent cannot repair (IndentationError).
    script = (
        "import sys\n"
        f"sys.path.insert(0, r{str(_PROJECT_ROOT)!r})\n"
        + textwrap.dedent(statement).strip("\n")
        + "\n"
        'print("\\n".join(sorted(set(sys.modules))))\n'
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


def _assert_no_runtime_stack(loaded: set[str], what: str) -> None:
    offenders = [m for m in _RUNTIME_STACK if m in loaded]
    assert not offenders, (
        f"importing {what} pulled in {offenders}; the keyless path must stay "
        "runnable on a host with no runtime stack installed (milestone5.md §0.1 F6)."
    )


def test_importing_the_package_does_not_load_cli():
    # The F6 headline: `harness/__init__.py` used to do an unconditional
    # `from harness.cli import main`, so this alone loaded langgraph.
    _assert_no_runtime_stack(_modules_after("import harness"), "harness")


def test_config_cli_imports_without_the_runtime_stack():
    # What config_cli.py's own docstring has always claimed about itself, now
    # true through the package too.
    _assert_no_runtime_stack(
        _modules_after("import harness.config_cli"), "harness.config_cli"
    )


def test_doctor_imports_without_the_runtime_stack():
    _assert_no_runtime_stack(_modules_after("import harness.doctor"), "harness.doctor")


def test_telemetry_imports_without_the_runtime_stack():
    # Milestone 6 invariant 22, in its strong form. The M6 branch could only claim
    # the narrow version ("adds no import cost config/doctor do not already pay")
    # because at the time the package pulled cli unconditionally; with F6 landed
    # the strong claim holds, so it gets pinned rather than left as prose.
    #
    # This is also what keeps `harness telemetry` readable on a bare host: reading
    # a finished run's numbers must not require the stack that produced them.
    _assert_no_runtime_stack(
        _modules_after("import harness.telemetry"), "harness.telemetry"
    )


def test_entry_routes_without_importing_cli():
    # The second half of the fix: lazy routes are worthless if reaching them
    # means importing cli.py first, which is what living in cli.py meant.
    _assert_no_runtime_stack(_modules_after("import harness.entry"), "harness.entry")


def test_package_getattr_does_not_shadow_submodule_imports():
    # __getattr__ must raise AttributeError on a miss: `from harness import config`
    # asks the package for the attribute first and only falls back to importing
    # the submodule when that raises. Returning a placeholder would break every
    # sibling import in the package (cli.py's `from harness import archive, ...`).
    loaded = _modules_after(
        "from harness import config\n"
        "import harness\n"
        "try:\n"
        "    harness.definitely_not_a_module\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('missing attribute did not raise AttributeError')\n"
    )
    assert "harness.config" in loaded


def test_limits_imports_without_the_runtime_stack():
    """Milestone 8 B1: `harness/limits.py` is stdlib-only by contract.

    That is what keeps the bound arithmetic in the host test tier — the same
    split `telemetry.py`/`TelemetryMiddleware` and `rawtrace.py`/
    `RawTraceMiddleware` already use. The one class that needs the langchain base
    (`cli.DeadlineMiddleware`) deliberately lives in `cli.py` instead, so a
    langchain import creeping in here would silently move the whole module out of
    the tier that can test it without Docker.
    """
    _assert_no_runtime_stack(_modules_after("import harness.limits"), "harness.limits")


def test_limits_imports_no_sibling_harness_module():
    # It must also stay free of *harness* siblings: `cli` imports it, so a
    # back-edge would be a cycle, and `stop_reason_for` deliberately matches
    # langgraph's GraphRecursionError by NAME rather than importing it.
    loaded = _modules_after("import harness.limits")
    siblings = {
        m for m in loaded
        if m.startswith("harness.") and m not in ("harness.limits",)
    }
    assert not siblings, f"harness.limits pulled in siblings: {sorted(siblings)}"


def test_bench_imports_without_the_runtime_stack():
    """Milestone 8 B3's done-when #6, holding from B2 onward.

    `harness bench` is a host-side admin command routed through
    `entry.dispatch`, so it must be keyless in the strong sense: no API key, no
    network, no model, and no runtime stack. The package `__init__` is
    deliberately empty of imports for this reason — a convenience re-export there
    would break the guarantee the moment one submodule grew a dependency, which
    is the defect M5 §0.1 F6 removed from `harness/__init__.py`.
    """
    _assert_no_runtime_stack(_modules_after("import harness.bench"), "harness.bench")
    _assert_no_runtime_stack(
        _modules_after("import harness.bench.patch"), "harness.bench.patch"
    )


def test_bench_patch_imports_no_sibling_harness_module():
    # It runs `git` as a subprocess and reads nothing else the harness owns, so a
    # sibling import here would be a new coupling rather than a reuse.
    loaded = _modules_after("import harness.bench.patch")
    siblings = {
        m for m in loaded
        if m.startswith("harness.") and m not in ("harness.bench", "harness.bench.patch")
    }
    assert not siblings, f"harness.bench.patch pulled in siblings: {sorted(siblings)}"
