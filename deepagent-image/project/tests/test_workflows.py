"""Tests for harness/workflows.py (the §3 deterministic workflow engine).

Pure: workflows.py degrades its AgentMiddleware base to `object` when langchain
is absent, so the loader / parser / gate logic and the middleware routing all
run on a bare host. Gate-eval tests that need a POSIX `sh` skip themselves
without one. Filesystem writes go to pytest's `tmp_path` (never under the
mounted project/), per the suite convention.

Run with pytest: `python3 -m pytest tests/` (in the test image, or any box with
a POSIX shell). The shared lazy loader lives in `_bootstrap.py`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from _bootstrap import _load

wf = _load("harness.workflows")

_HAS_SH = shutil.which("sh") is not None
needs_sh = pytest.mark.skipif(not _HAS_SH, reason="needs a POSIX sh")


def _write_workflow(
    root,
    name="demo",
    hook="session.start",
    gate_name="trigger.sh",
    gate_body="#!/bin/sh\nexit 0\n",
    steps=("./step.sh",),
    extra_files=None,
):
    """Write a `<root>/<name>/` workflow folder; return `root` (a workflows/
    root holding one workflow). `root` is a pytest tmp_path subdir."""
    folder = root / name
    folder.mkdir(parents=True)
    steps_block = "".join(f"  - {s}\n" for s in steps)
    manifest = (
        f"---\nname: {name}\nhook: {hook}\ngate: {gate_name}\nsteps:\n{steps_block}---\nbody\n"
    )
    (folder / "workflow.md").write_text(manifest, encoding="utf-8")
    if gate_name:
        (folder / gate_name).write_text(gate_body, encoding="utf-8")
    for fname, body in (extra_files or {}).items():
        (folder / fname).write_text(body, encoding="utf-8")
    return root


# --- loading + validation ----------------------------------------------------

def test_load_valid_workflow(tmp_path):
    root = _write_workflow(tmp_path, name="demo", hook="agent.start", steps=["./a.sh", "/abs/b.sh"])
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


def test_folder_without_manifest_is_skipped(tmp_path):
    (tmp_path / "not-a-workflow").mkdir()
    assert wf.load_workflows(tmp_path) == []


def test_missing_dir_is_empty(tmp_path):
    assert wf.load_workflows(tmp_path / "nope") == []


def test_name_must_match_folder(tmp_path):
    root = _write_workflow(tmp_path, name="demo")
    (root / "demo" / "workflow.md").write_text(
        "---\nname: other\nhook: session.start\ngate: trigger.sh\nsteps:\n  - ./s.sh\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        wf.load_workflows(root)


def test_unknown_hook_rejected(tmp_path):
    root = _write_workflow(tmp_path, name="demo", hook="not.a.hook")
    with pytest.raises(SystemExit):
        wf.load_workflows(root)


def test_missing_gate_rejected(tmp_path):
    root = _write_workflow(tmp_path, name="demo", gate_name="")  # no gate file
    with pytest.raises(SystemExit):
        wf.load_workflows(root)


def test_both_gates_rejected(tmp_path):
    root = _write_workflow(tmp_path, name="demo", gate_name="trigger.sh")
    (root / "demo" / "trigger.py").write_text("def gate(ctx): return True\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        wf.load_workflows(root)


def test_missing_frontmatter_rejected(tmp_path):
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "workflow.md").write_text("no frontmatter here\n", encoding="utf-8")
    (folder / "trigger.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        wf.load_workflows(tmp_path)


# --- frontmatter parsing -----------------------------------------------------

def test_frontmatter_inline_comment_and_quotes(tmp_path):
    folder = tmp_path / "demo"
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
    w = wf.load_workflows(tmp_path)[0]
    assert w.name == "demo"
    assert w.hook == "session.end"
    assert w.steps[0].endswith("s.sh")
    assert "#" not in w.steps[0]  # inline comment must not leak into the path


# --- gate evaluation ---------------------------------------------------------

def test_always_gate_when_none(tmp_path):
    w = wf.Workflow(name="x", hook="session.start", steps=())
    assert wf.evaluate_gate(w, wf.GateContext("session.start", tmp_path)) is True


@needs_sh
def test_shell_gate_pass_and_fail(tmp_path):
    root_pass = _write_workflow(tmp_path / "p", name="p", gate_body="#!/bin/sh\nexit 0\n", steps=[])
    root_fail = _write_workflow(tmp_path / "f", name="f", gate_body="#!/bin/sh\nexit 1\n", steps=[])
    ctx = wf.GateContext("session.start", tmp_path)
    assert wf.evaluate_gate(wf.load_workflows(root_pass)[0], ctx) is True
    assert wf.evaluate_gate(wf.load_workflows(root_fail)[0], ctx) is False


@needs_sh
def test_shell_gate_reads_event_env(tmp_path):
    body = '#!/bin/sh\n[ "$DEEPAGENTS_HOOK_EVENT" = "session.end" ]\n'
    root = _write_workflow(tmp_path, name="ev", gate_body=body, steps=[])
    w = wf.load_workflows(root)[0]
    assert wf.evaluate_gate(w, wf.GateContext("session.end", tmp_path)) is True
    assert wf.evaluate_gate(w, wf.GateContext("session.start", tmp_path)) is False


def test_python_gate(tmp_path):
    body = "def gate(ctx):\n    return ctx.event == 'agent.start'\n"
    root = _write_workflow(
        tmp_path, name="py", hook="agent.start", gate_name="trigger.py", gate_body=body, steps=[]
    )
    w = wf.load_workflows(root)[0]
    assert w.gate_is_python
    assert wf.evaluate_gate(w, wf.GateContext("agent.start", tmp_path)) is True
    assert wf.evaluate_gate(w, wf.GateContext("model.start", tmp_path)) is False


def test_python_gate_missing_function_rejected(tmp_path):
    root = _write_workflow(tmp_path, name="py", gate_name="trigger.py", gate_body="x = 1\n", steps=[])
    w = wf.load_workflows(root)[0]
    with pytest.raises(SystemExit):
        wf.evaluate_gate(w, wf.GateContext("session.start", tmp_path))


# --- step execution ----------------------------------------------------------

@needs_sh
def test_run_steps_runs_sh_without_exec_bit_and_passes_env(tmp_path):
    out = tmp_path / "out.txt"
    # Step writes the event + workspace it received; never marked executable.
    step_body = f'#!/bin/sh\necho "$DEEPAGENTS_HOOK_EVENT $DEEPAGENTS_WORKSPACE" > "{out}"\n'
    root = _write_workflow(
        tmp_path / "wf", name="w", gate_body="#!/bin/sh\nexit 0\n", steps=["./run.sh"],
        extra_files={"run.sh": step_body},
    )
    w = wf.load_workflows(root)[0]
    ws = tmp_path / "some-ws"
    wf.run_workflow(w, wf.GateContext("session.end", ws))
    assert out.read_text(encoding="utf-8").strip() == f"session.end {ws}"


@needs_sh
def test_run_steps_skipped_when_gate_fails(tmp_path):
    out = tmp_path / "out.txt"
    root = _write_workflow(
        tmp_path / "wf", name="w", gate_body="#!/bin/sh\nexit 1\n", steps=["./run.sh"],
        extra_files={"run.sh": f'#!/bin/sh\necho ran > "{out}"\n'},
    )
    w = wf.load_workflows(root)[0]
    wf.run_workflow(w, wf.GateContext("session.start", tmp_path))
    assert not out.exists()  # gate failed -> step never ran


# --- subprocess timeout (carried over from the hooks.json engine) ------------

def test_hook_timeout_default_and_overrides(monkeypatch):
    monkeypatch.delenv("DEEPAGENTS_HOOK_TIMEOUT", raising=False)
    assert wf._hook_timeout() == wf._DEFAULT_HOOK_TIMEOUT
    monkeypatch.setenv("DEEPAGENTS_HOOK_TIMEOUT", "5")
    assert wf._hook_timeout() == 5.0
    monkeypatch.setenv("DEEPAGENTS_HOOK_TIMEOUT", "0")  # <=0 disables the cap
    assert wf._hook_timeout() is None
    monkeypatch.setenv("DEEPAGENTS_HOOK_TIMEOUT", "nope")  # garbage -> default
    assert wf._hook_timeout() == wf._DEFAULT_HOOK_TIMEOUT


def test_step_timeout_is_caught_not_propagated(tmp_path, monkeypatch, capsys):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(wf.subprocess, "run", boom)
    w = wf.Workflow(name="t", hook="session.end", steps=("echo hi",))
    wf.run_steps(w, wf.GateContext("session.end", tmp_path))  # must not raise
    assert "timed out" in capsys.readouterr().err


def test_gate_timeout_skips(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(wf.subprocess, "run", boom)
    root = _write_workflow(tmp_path, name="g", steps=[])
    w = wf.load_workflows(root)[0]
    assert wf.evaluate_gate(w, wf.GateContext("session.start", tmp_path)) is False


# --- hooks.json adapter + grouping -------------------------------------------

def test_hooks_to_workflows_always_gate(tmp_path):
    workflows = wf.hooks_to_workflows({"session.end": ["echo hi"], "tool.start": ["echo t"]})
    assert {w.hook for w in workflows} == {"session.end", "tool.start"}
    for w in workflows:
        assert w.gate is None  # always
        assert wf.evaluate_gate(w, wf.GateContext(w.hook, tmp_path)) is True


def test_hooks_to_workflows_unknown_event_rejected():
    with pytest.raises(SystemExit):
        wf.hooks_to_workflows({"bogus.event": ["echo x"]})


def test_workflows_by_hook_groups():
    a = wf.Workflow(name="a", hook="session.start", steps=())
    b = wf.Workflow(name="b", hook="session.start", steps=())
    c = wf.Workflow(name="c", hook="tool.end", steps=())
    grouped = wf.workflows_by_hook([a, b, c])
    assert [w.name for w in grouped["session.start"]] == ["a", "b"]
    assert [w.name for w in grouped["tool.end"]] == ["c"]


def test_build_middleware_only_for_non_session_hooks(tmp_path):
    session_only = wf.workflows_by_hook([wf.Workflow(name="s", hook="session.start", steps=())])
    assert wf.build_workflow_middleware(session_only, tmp_path) == []
    with_turn = wf.workflows_by_hook([wf.Workflow(name="t", hook="agent.start", steps=())])
    assert len(wf.build_workflow_middleware(with_turn, tmp_path)) == 1


# --- WorkflowMiddleware event routing ----------------------------------------

@pytest.fixture
def routed(monkeypatch):
    """Capture (event, [workflow names]) each middleware event dispatches,
    without running real gates/steps."""
    fired = []
    monkeypatch.setattr(
        wf, "run_hook", lambda workflows_for_hook, ctx: fired.append(
            (ctx.event, [w.name for w in workflows_for_hook])
        )
    )
    return fired


def _mw(by_hook, workspace):
    return wf.WorkflowMiddleware(by_hook, workspace)


def test_before_agent_routes_to_agent_start(routed, tmp_path):
    mw = _mw({"agent.start": [wf.Workflow(name="a", hook="agent.start", steps=())]}, tmp_path)
    mw.before_agent({"messages": []}, None)
    assert routed == [("agent.start", ["a"])]


def test_model_events_route_to_their_hooks(routed, tmp_path):
    mw = _mw(
        {
            "model.start": [wf.Workflow(name="s", hook="model.start", steps=())],
            "model.end": [wf.Workflow(name="e", hook="model.end", steps=())],
        },
        tmp_path,
    )
    mw.before_model({"messages": []}, None)
    mw.after_model({"messages": []}, None)
    assert routed == [("model.start", ["s"]), ("model.end", ["e"])]


def test_wrap_tool_call_brackets_handler_and_returns_result(routed, tmp_path):
    mw = _mw(
        {
            "tool.start": [wf.Workflow(name="s", hook="tool.start", steps=())],
            "tool.end": [wf.Workflow(name="e", hook="tool.end", steps=())],
        },
        tmp_path,
    )
    seen = []

    def handler(req):
        seen.append(req)
        return "RESULT"

    out = mw.wrap_tool_call("REQ", handler)
    assert out == "RESULT"
    assert [e for e, _ in routed] == ["tool.start", "tool.end"]
    assert seen == ["REQ"]


def test_wrap_tool_call_runs_tool_end_even_on_error(routed, tmp_path):
    mw = _mw({"tool.start": [], "tool.end": []}, tmp_path)

    def boom(req):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        mw.wrap_tool_call("REQ", boom)
    assert [e for e, _ in routed] == ["tool.start", "tool.end"]  # tool.end via finally


# --- latest-user-text helper -------------------------------------------------

def test_latest_user_text_picks_last_human():
    state = {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]}
    assert wf._latest_user_text(state) == "second"


def test_latest_user_text_none_when_absent():
    assert wf._latest_user_text({"messages": []}) is None
    assert wf._latest_user_text({}) is None


# --- step/gate stdout isolation (headless JSON-contract regression) ----------

def test_run_steps_redirects_stdout_off_the_harness_stdout(monkeypatch):
    # Regression: a workflow/hook step that prints must NOT inherit the harness's
    # own stdout — headless run_batch reserves stdout for its single JSON line.
    # run_steps must pass an explicit stdout redirect (never None/inherited).
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(wf.subprocess, "run", fake_run)
    workflow = wf.Workflow(name="hooks.json:session.end", hook="session.end",
                           steps=("echo leak-to-stdout",))
    wf.run_steps(workflow, wf.GateContext("session.end", workspace=__import__("pathlib").Path(".")))
    assert calls, "step should have invoked subprocess.run"
    assert calls[0].get("stdout") is not None, "step stdout must be redirected, not inherited"


def test_side_effect_stdout_is_stderr_or_devnull():
    import subprocess as _sp
    assert wf._side_effect_stdout() in (sys.stderr, _sp.DEVNULL)


# --- M4 §15.1: git-pr must never blank a masked secret into a commit --------

import os  # noqa: E402
import pathlib  # noqa: E402

_HAS_GIT = shutil.which("git") is not None
_HAS_PY3 = shutil.which("python3") is not None
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]  # deepagent-image/project
_STAGE_SCRIPT = _PROJECT_ROOT / "workflows" / "git-pr" / "stage-commit-push.sh"


@pytest.mark.skipif(
    not (_HAS_SH and _HAS_GIT and _HAS_PY3),
    reason="git-pr exclusion needs sh, git, and python3",
)
def test_git_pr_excludes_masked_secret_from_staging(tmp_path):
    """§15.1: the docker mask makes .env read empty in the container; git-pr must
    unstage it so the emptied file is NOT committed (the real secret is preserved
    on the branch). Regression for the secret-blanking hazard."""
    # The git-pr step shells out to `python3 -m harness mask-scan`; skip unless the
    # `python3` on PATH is actually the harness interpreter (in the image it is; on
    # a dev host `python3` may be a stub without harness deps).
    probe = subprocess.run(
        ["python3", "-c", "import harness.mask"],
        env={**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)},
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("python3 on PATH is not the harness interpreter")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")

    # .env tracked in HEAD with the real secret; app.py gives us a real change to commit.
    (repo / ".env").write_text("REALSECRET=1\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    # Simulate the container view: the mask overlay makes .env read EMPTY. Plus an
    # unrelated real edit so the commit has content.
    (repo / ".env").write_text("", encoding="utf-8")
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")

    state = tmp_path / "state"
    state.mkdir()
    (state / "session.env").write_text(
        "DEEPAGENTS_SESSION_ID=test\nDEEPAGENTS_SESSION_BRANCH=agent/test\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["DEEPAGENTS_WORKSPACE"] = str(repo)
    env["DEEPAGENTS_STATE_DIR"] = str(state)
    env["PYTHONPATH"] = str(_PROJECT_ROOT)
    # No remote → the script's push fails gracefully and it exits 0.
    subprocess.run(["sh", str(_STAGE_SCRIPT)], env=env, check=True, capture_output=True, text=True)

    # The committed .env must still be the real secret, never the masked empty.
    head_env = subprocess.run(
        ["git", "show", "HEAD:.env"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert head_env == "REALSECRET=1\n", "git-pr blanked the masked secret into the commit"
    # And the unrelated change DID commit (proves the run actually committed).
    head_app = subprocess.run(
        ["git", "show", "HEAD:app.py"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert head_app == "x = 2\n"
