"""Unit tests for harness/workflows.py (the §3 deterministic workflow engine).

Pure: no langchain (workflows.py degrades AgentMiddleware to object when it is
absent), no network. Gate-eval tests that need a POSIX `sh` skip themselves on a
host without one; everything else runs anywhere.

Run inside the image with pytest (`python3 -m pytest tests/`), or standalone on
any box with `python3 tests/test_workflows.py` (a built-in runner at the bottom
calls every test_* with no extra dependency).
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

# --- bootstrap: load harness.workflows WITHOUT triggering harness/__init__.py
# (which pulls cli -> dotenv/langgraph/deepagents, none installed on a plain
# test host). Mirror tests/test_cost.py.
_HARNESS = Path(__file__).resolve().parent.parent / "harness"


def _load(modname: str) -> types.ModuleType:
    if "harness" not in sys.modules:
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(_HARNESS)]  # mark as a package
        sys.modules["harness"] = pkg
    spec = importlib.util.spec_from_file_location(
        modname, _HARNESS / f"{modname.split('.')[-1]}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


wf = _load("harness.workflows")

_HAS_SH = shutil.which("sh") is not None


def _make_workflow(
    name: str = "demo",
    hook: str = "session.start",
    gate_name: str = "trigger.sh",
    gate_body: str = "#!/bin/sh\nexit 0\n",
    steps: list[str] | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Write a workflow folder under a fresh temp dir; return the temp root
    (the workflows/ root, with one <name>/ folder inside)."""
    root = Path(tempfile.mkdtemp())
    folder = root / name
    folder.mkdir()
    steps = steps if steps is not None else ["./step.sh"]
    steps_block = "".join(f"  - {s}\n" for s in steps)
    manifest = f"---\nname: {name}\nhook: {hook}\ngate: {gate_name}\nsteps:\n{steps_block}---\nbody\n"
    (folder / "workflow.md").write_text(manifest, encoding="utf-8")
    if gate_name:
        (folder / gate_name).write_text(gate_body, encoding="utf-8")
    for fname, body in (extra_files or {}).items():
        (folder / fname).write_text(body, encoding="utf-8")
    return root


# --- loading + validation ----------------------------------------------------


def test_load_valid_workflow():
    root = _make_workflow(name="demo", hook="agent.start", steps=["./a.sh", "/abs/b.sh"])
    workflows = wf.load_workflows(root)
    assert len(workflows) == 1
    w = workflows[0]
    assert w.name == "demo"
    assert w.hook == "agent.start"
    assert w.gate.name == "trigger.sh"
    assert not w.gate_is_python
    # relative step resolved against the folder, absolute step passed through
    assert w.steps[0].endswith("a.sh") and str(root) in w.steps[0]
    assert w.steps[1] == "/abs/b.sh"


def test_folder_without_manifest_is_skipped():
    root = Path(tempfile.mkdtemp())
    (root / "not-a-workflow").mkdir()
    assert wf.load_workflows(root) == []


def test_missing_dir_is_empty():
    assert wf.load_workflows(Path(tempfile.mkdtemp()) / "nope") == []


def test_name_must_match_folder():
    root = _make_workflow(name="demo")
    # rewrite manifest with a mismatched name
    (root / "demo" / "workflow.md").write_text(
        "---\nname: other\nhook: session.start\ngate: trigger.sh\nsteps:\n  - ./s.sh\n---\n",
        encoding="utf-8",
    )
    _expect_systemexit(root)


def test_unknown_hook_rejected():
    root = _make_workflow(name="demo", hook="not.a.hook")
    _expect_systemexit(root)


def test_missing_gate_rejected():
    root = _make_workflow(name="demo", gate_name="")  # no gate file written
    _expect_systemexit(root)


def test_both_gates_rejected():
    root = _make_workflow(name="demo", gate_name="trigger.sh")
    (root / "demo" / "trigger.py").write_text("def gate(ctx): return True\n", encoding="utf-8")
    _expect_systemexit(root)


def test_missing_frontmatter_rejected():
    root = Path(tempfile.mkdtemp())
    folder = root / "demo"
    folder.mkdir()
    (folder / "workflow.md").write_text("no frontmatter here\n", encoding="utf-8")
    (folder / "trigger.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _expect_systemexit(root)


def _expect_systemexit(root: Path):
    try:
        wf.load_workflows(root)
    except SystemExit:
        return
    raise AssertionError("expected SystemExit")


# --- frontmatter parsing -----------------------------------------------------


def test_frontmatter_inline_comment_and_quotes():
    root = Path(tempfile.mkdtemp())
    folder = root / "demo"
    folder.mkdir()
    (folder / "workflow.md").write_text(
        "---\n"
        "name: demo          # the name\n"
        'hook: "session.end"\n'
        "gate: trigger.sh\n"
        "steps:\n"
        "  - ./s.sh   # a step\n"
        "---\n",
        encoding="utf-8",
    )
    (folder / "trigger.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (folder / "s.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    w = wf.load_workflows(root)[0]
    assert w.name == "demo"
    assert w.hook == "session.end"
    assert w.steps[0].endswith("s.sh")
    # inline comment must not leak into the resolved step path
    assert "#" not in w.steps[0]


# --- gate evaluation ---------------------------------------------------------


def test_always_gate_when_none():
    w = wf.Workflow(name="x", hook="session.start", steps=())
    ctx = wf.GateContext("session.start", Path("."))
    assert wf.evaluate_gate(w, ctx) is True


def test_shell_gate_pass_and_fail():
    if not _HAS_SH:
        print("  skip test_shell_gate_pass_and_fail (no sh)")
        return
    root_pass = _make_workflow(name="p", gate_body="#!/bin/sh\nexit 0\n", steps=[])
    root_fail = _make_workflow(name="f", gate_body="#!/bin/sh\nexit 1\n", steps=[])
    ctx = wf.GateContext("session.start", Path("."))
    assert wf.evaluate_gate(wf.load_workflows(root_pass)[0], ctx) is True
    assert wf.evaluate_gate(wf.load_workflows(root_fail)[0], ctx) is False


def test_shell_gate_reads_event_env():
    if not _HAS_SH:
        print("  skip test_shell_gate_reads_event_env (no sh)")
        return
    # gate passes only on session.end, proving DEEPAGENTS_HOOK_EVENT reaches it
    body = '#!/bin/sh\n[ "$DEEPAGENTS_HOOK_EVENT" = "session.end" ]\n'
    root = _make_workflow(name="ev", gate_body=body, steps=[])
    w = wf.load_workflows(root)[0]
    assert wf.evaluate_gate(w, wf.GateContext("session.end", Path("."))) is True
    assert wf.evaluate_gate(w, wf.GateContext("session.start", Path("."))) is False


def test_python_gate():
    body = "def gate(ctx):\n    return ctx.event == 'agent.start'\n"
    root = _make_workflow(name="py", hook="agent.start", gate_name="trigger.py", gate_body=body, steps=[])
    w = wf.load_workflows(root)[0]
    assert w.gate_is_python
    assert wf.evaluate_gate(w, wf.GateContext("agent.start", Path("."))) is True
    assert wf.evaluate_gate(w, wf.GateContext("model.start", Path("."))) is False


def test_python_gate_missing_function_rejected():
    root = _make_workflow(
        name="py", gate_name="trigger.py", gate_body="x = 1\n", steps=[]
    )
    w = wf.load_workflows(root)[0]
    try:
        wf.evaluate_gate(w, wf.GateContext("session.start", Path(".")))
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for trigger.py with no gate()")


def test_run_steps_runs_sh_without_exec_bit_and_passes_env():
    if not _HAS_SH:
        print("  skip test_run_steps_runs_sh_without_exec_bit_and_passes_env (no sh)")
        return
    out = Path(tempfile.mkdtemp()) / "out.txt"
    # Step writes the event + workspace it received; never marked executable.
    step_body = f'#!/bin/sh\necho "$DEEPAGENTS_HOOK_EVENT $DEEPAGENTS_WORKSPACE" > "{out}"\n'
    root = _make_workflow(
        name="w", gate_body="#!/bin/sh\nexit 0\n", steps=["./run.sh"],
        extra_files={"run.sh": step_body},
    )
    w = wf.load_workflows(root)[0]
    ws = Path("/some/ws")
    wf.run_workflow(w, wf.GateContext("session.end", ws))
    # str(ws) differs by host (POSIX vs Windows); compare to what as_env emits.
    assert out.read_text(encoding="utf-8").strip() == f"session.end {ws}"


def test_run_steps_skipped_when_gate_fails():
    if not _HAS_SH:
        print("  skip test_run_steps_skipped_when_gate_fails (no sh)")
        return
    out = Path(tempfile.mkdtemp()) / "out.txt"
    step_body = f'#!/bin/sh\necho ran > "{out}"\n'
    root = _make_workflow(
        name="w", gate_body="#!/bin/sh\nexit 1\n", steps=["./run.sh"],
        extra_files={"run.sh": step_body},
    )
    w = wf.load_workflows(root)[0]
    wf.run_workflow(w, wf.GateContext("session.start", Path(".")))
    assert not out.exists()  # gate failed -> step never ran


# --- hooks.json adapter + grouping -------------------------------------------


def test_hooks_to_workflows_always_gate():
    workflows = wf.hooks_to_workflows({"session.end": ["echo hi"], "tool.start": ["echo t"]})
    assert {w.hook for w in workflows} == {"session.end", "tool.start"}
    for w in workflows:
        assert w.gate is None  # always
        assert wf.evaluate_gate(w, wf.GateContext(w.hook, Path("."))) is True


def test_hooks_to_workflows_unknown_event_rejected():
    try:
        wf.hooks_to_workflows({"bogus.event": ["echo x"]})
    except SystemExit:
        return
    raise AssertionError("expected SystemExit for unknown hooks.json event")


def test_workflows_by_hook_groups():
    a = wf.Workflow(name="a", hook="session.start", steps=())
    b = wf.Workflow(name="b", hook="session.start", steps=())
    c = wf.Workflow(name="c", hook="tool.end", steps=())
    grouped = wf.workflows_by_hook([a, b, c])
    assert [w.name for w in grouped["session.start"]] == ["a", "b"]
    assert [w.name for w in grouped["tool.end"]] == ["c"]


def test_build_middleware_only_for_non_session_hooks():
    # session-only -> no middleware appended (those fire in cli.main())
    session_only = wf.workflows_by_hook([wf.Workflow(name="s", hook="session.start", steps=())])
    assert wf.build_workflow_middleware(session_only, Path(".")) == []
    # a per-turn hook -> one middleware
    with_turn = wf.workflows_by_hook([wf.Workflow(name="t", hook="agent.start", steps=())])
    assert len(wf.build_workflow_middleware(with_turn, Path("."))) == 1


# --- standalone runner -------------------------------------------------------

if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
