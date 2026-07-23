"""Custom workflow engine (design_doc.md §3).

A *workflow* is trigger (gate) × hook point × action (steps), discovered as a
self-contained folder under a `workflows/` root. This module is the
**deterministic slice**: predicate gates (`trigger.py` in-process /
`trigger.sh` subprocess) plus the **side-effect** action tier (run step
scripts, result discarded). The classifier gate and the context-mutation /
control-flow action tiers are planned, not built (§3 "Built vs. planned").

`hooks.json` is the flat precursor: each entry becomes a synthetic workflow
with an `always` gate (no trigger file) and shell-command steps — one code path
for both (`hooks_to_workflows`).

Stdlib only at import (the `AgentMiddleware` import degrades to `object` when
langchain is absent, like `cost.py`) so the loader/parser/gate logic imports on
a bare test host, and because `trigger.py` gates run **inside the harness venv**
— engine code, stdlib + engine API only, never workspace deps (the two-stack
rule, §3).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from harness._compat import compat_import

AgentMiddleware = compat_import("langchain.agents.middleware.types", "AgentMiddleware")  # type: ignore[assignment,misc]


# The 7 lifecycle hook points (§3). session.* fire once around the whole run in
# cli.main(); the rest fire per agent/model/tool event via WorkflowMiddleware.
HOOK_POINTS = (
    "session.start",
    "session.end",
    "agent.start",
    "agent.end",
    "model.start",
    "model.end",
    "tool.start",
    "tool.end",
)
SESSION_EVENTS = ("session.start", "session.end")

GATE_BASENAME = "trigger"  # fixed; the gate is always ./trigger.{py,sh} in the folder
MANIFEST_NAME = "workflow.md"

# Per-subprocess wall-clock cap so a hung gate/step can't freeze the whole
# session (the REPL is otherwise blocked in the synchronous middleware dispatch).
# Overridable via env for slow but legitimate workflows; <=0 disables the cap.
# Carried over from the old hooks.json engine (DEEPAGENTS_HOOK_TIMEOUT).
_DEFAULT_HOOK_TIMEOUT = 30.0


def _side_effect_stdout():
    """Where a gate/step subprocess's stdout goes.

    Never the harness's own stdout: a headless run (`cli.run_batch`) reserves
    stdout for the single JSON result line, so any workflow/hook step that prints
    (e.g. a git-pr status line, or a stray `echo`) would corrupt that contract if
    it inherited stdout. Route step stdout to **stderr** — visible for debugging,
    off the machine-readable channel — mirroring how the harness writes its own
    stage markers. Falls back to DEVNULL if stderr has no real fileno (e.g. under a
    capturing test harness) so the redirect can never itself raise."""
    try:
        sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL
    return sys.stderr


def _hook_timeout() -> float | None:
    raw = os.getenv("DEEPAGENTS_HOOK_TIMEOUT")
    if raw is None:
        return _DEFAULT_HOOK_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_HOOK_TIMEOUT
    return value if value > 0 else None


@dataclass(frozen=True)
class GateContext:
    """Live state a gate may read. `trigger.py` receives this object directly;
    `trigger.sh` receives the scalar fields as environment variables."""

    event: str
    workspace: Path
    prompt: str | None = None  # latest user input, when one exists at this event

    def as_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DEEPAGENTS_HOOK_EVENT"] = self.event
        env["DEEPAGENTS_WORKSPACE"] = str(self.workspace)
        if self.prompt is not None:
            env["DEEPAGENTS_PROMPT"] = self.prompt
        return env


@dataclass(frozen=True)
class Workflow:
    """One discovered workflow. `gate is None` means `always` (the hooks.json
    precursor). `folder is None` for synthetic hooks.json workflows."""

    name: str
    hook: str
    steps: tuple[str, ...]  # resolved command strings (abs paths or shell passthrough)
    folder: Path | None = None
    gate: Path | None = None  # absolute path to trigger.py / trigger.sh, or None = always

    @property
    def gate_is_python(self) -> bool:
        return self.gate is not None and self.gate.suffix == ".py"


# --- loading -----------------------------------------------------------------


def load_workflows(workflows_dir: Path) -> list[Workflow]:
    """Discover every `workflows/<name>/` folder holding a `workflow.md`.
    Folders without a manifest are skipped; a malformed manifest fails loudly."""
    if not workflows_dir.is_dir():
        return []
    workflows: list[Workflow] = []
    for folder in sorted(p for p in workflows_dir.iterdir() if p.is_dir()):
        manifest = folder / MANIFEST_NAME
        if manifest.is_file():
            workflows.append(_load_workflow(folder, manifest))
    return workflows


def _load_workflow(folder: Path, manifest: Path) -> Workflow:
    meta = _parse_frontmatter(manifest)
    name = meta.get("name")
    hook = meta.get("hook")
    steps = meta.get("steps", [])

    if name != folder.name:
        raise SystemExit(
            f"workflow {manifest}: name '{name}' must equal folder name '{folder.name}'"
        )
    if hook not in HOOK_POINTS:
        raise SystemExit(
            f"workflow '{folder.name}': hook '{hook}' is not one of {', '.join(HOOK_POINTS)}"
        )
    if not isinstance(steps, list):
        raise SystemExit(f"workflow '{folder.name}': steps must be a list")

    gate = _resolve_gate(folder)
    resolved_steps = tuple(_resolve_step(folder, str(s)) for s in steps)
    return Workflow(name=name, hook=hook, steps=resolved_steps, folder=folder, gate=gate)


def _resolve_gate(folder: Path) -> Path:
    """Resolve the fixed-basename gate: trigger.py first, then trigger.sh.
    Exactly one must exist (§3)."""
    py = folder / f"{GATE_BASENAME}.py"
    sh = folder / f"{GATE_BASENAME}.sh"
    found = [p for p in (py, sh) if p.is_file()]
    if not found:
        raise SystemExit(
            f"workflow '{folder.name}': missing required gate "
            f"{GATE_BASENAME}.py or {GATE_BASENAME}.sh"
        )
    if len(found) == 2:
        raise SystemExit(
            f"workflow '{folder.name}': both {GATE_BASENAME}.py and {GATE_BASENAME}.sh "
            "present; exactly one allowed"
        )
    return found[0]


def _resolve_step(folder: Path, step: str) -> str:
    """Relative paths resolve against the workflow folder; absolute paths run
    as-is (§3). POSIX-absolute ('/...') is honored too, so a manifest authored
    for the linux image resolves the same on a non-POSIX dev host."""
    step = step.strip()
    if step.startswith("/") or Path(step).is_absolute():
        return step
    return str((folder / step).resolve())


# --- frontmatter (minimal, stdlib-only) --------------------------------------
# A purpose-built parser for the tiny YAML subset the manifest uses (scalar
# `key: value` and a `key:` block list of `- item`). Keeps the engine free of a
# yaml dependency so it imports on a bare test host (the gate runs here too).


def _parse_frontmatter(manifest: Path) -> dict:
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SystemExit(f"workflow {manifest}: must open with a '---' frontmatter line")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SystemExit(f"workflow {manifest}: unterminated '---' frontmatter")
    return _parse_block(lines[1:end], manifest)


def _parse_block(body: list[str], manifest: Path) -> dict:
    meta: dict = {}
    list_key: str | None = None
    for raw in body:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- "):
            if list_key is None:
                raise SystemExit(f"workflow {manifest}: list item '{stripped}' before any key")
            meta[list_key].append(_scalar(stripped[2:]))
            continue
        if ":" not in raw:
            raise SystemExit(f"workflow {manifest}: bad frontmatter line '{raw.rstrip()}'")
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            meta[key] = []
            list_key = key
        else:
            meta[key] = _scalar(value)
            list_key = None
    return meta


def _scalar(value: str) -> str:
    """Strip an inline ' # comment' (unquoted only), then surrounding quotes."""
    value = value.strip()
    if value and value[0] not in "'\"":
        hash_pos = value.find(" #")
        if hash_pos != -1:
            value = value[:hash_pos].rstrip()
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        value = value[1:-1]
    return value


# --- gate evaluation + step execution ----------------------------------------


def evaluate_gate(wf: Workflow, ctx: GateContext) -> bool:
    """True when the workflow's steps should run. `None` gate = always (§3)."""
    if wf.gate is None:
        return True
    if wf.gate_is_python:
        return _run_python_gate(wf, ctx)
    return _run_shell_gate(wf, ctx)


def _run_shell_gate(wf: Workflow, ctx: GateContext) -> bool:
    # exit 0 = run, non-zero = skip (§3). Runs from the workflow folder so a
    # gate can reference sibling files; live in-memory state is not visible here.
    timeout = _hook_timeout()
    try:
        result = subprocess.run(
            ["sh", str(wf.gate)], cwd=str(wf.folder), env=ctx.as_env(),
            check=False, timeout=timeout, stdout=_side_effect_stdout(),
        )
    except subprocess.TimeoutExpired:
        # A hung gate is killed and treated as "skip" (loud, non-fatal) rather
        # than hanging the session.
        print(
            f"[harness] WARNING: workflow '{wf.name}' gate timed out after "
            f"{timeout}s and was killed; skipping",
            file=sys.stderr,
        )
        return False
    return result.returncode == 0


def _run_python_gate(wf: Workflow, ctx: GateContext) -> bool:
    # In-process: same interpreter as the harness, so the gate can read live
    # state (and, once built, call the classifier directly). Must define
    # `gate(ctx) -> bool`.
    spec = importlib.util.spec_from_file_location(f"_workflow_gate_{wf.name}", wf.gate)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise SystemExit(f"workflow '{wf.name}': cannot load {wf.gate}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gate_fn = getattr(module, "gate", None)
    if not callable(gate_fn):
        raise SystemExit(f"workflow '{wf.name}': {wf.gate.name} must define gate(ctx) -> bool")
    return bool(gate_fn(ctx))


def _step_command(step: str):
    """A resolved path to an existing `.sh` file runs via `sh <path>` — so it
    needs **no exec bit** (Windows-authored scripts COPY into the image without
    one). Everything else (hooks.json shell strings, non-.sh commands) runs
    through the shell unchanged."""
    p = Path(step)
    if p.suffix == ".sh" and p.is_file():
        return ["sh", str(p)]
    return step


def run_steps(wf: Workflow, ctx: GateContext) -> None:
    # Side-effect tier: result discarded (check=False) — parity with the old
    # hooks.json behavior. The gate's env (DEEPAGENTS_HOOK_EVENT / _WORKSPACE /
    # _PROMPT) is passed to steps too, so a step script sees the same context.
    env = ctx.as_env()
    # Folder workflows run from their folder (consistent with the gate); the
    # hooks.json precursor (folder is None) keeps the harness CWD as before.
    cwd = str(wf.folder) if wf.folder is not None else None
    timeout = _hook_timeout()
    for step in wf.steps:
        cmd = _step_command(step)
        try:
            subprocess.run(
                cmd, shell=isinstance(cmd, str), env=env, cwd=cwd,
                check=False, timeout=timeout, stdout=_side_effect_stdout(),
            )
        except subprocess.TimeoutExpired:
            # Loud, non-fatal: a stuck step is killed and the session continues
            # rather than hanging indefinitely (DEEPAGENTS_HOOK_TIMEOUT).
            print(
                f"[harness] WARNING: workflow '{wf.name}' step timed out after "
                f"{timeout}s and was killed: {step!r}",
                file=sys.stderr,
            )


def run_workflow(wf: Workflow, ctx: GateContext) -> None:
    if evaluate_gate(wf, ctx):
        run_steps(wf, ctx)


# --- grouping + dispatch -----------------------------------------------------


def workflows_by_hook(workflows: list[Workflow]) -> dict[str, list[Workflow]]:
    by_hook: dict[str, list[Workflow]] = {}
    for wf in workflows:
        by_hook.setdefault(wf.hook, []).append(wf)
    return by_hook


def run_hook(workflows_for_hook: list[Workflow], ctx: GateContext) -> None:
    for wf in workflows_for_hook:
        run_workflow(wf, ctx)


def hooks_to_workflows(by_event: dict[str, list[str]]) -> list[Workflow]:
    """Adapt the flat hooks.json map into always-gate side-effect workflows so
    the precursor and the folder format share one execution path (§3)."""
    workflows: list[Workflow] = []
    for event, commands in by_event.items():
        if event not in HOOK_POINTS:
            raise SystemExit(f"hooks.json: unknown event '{event}'")
        workflows.append(
            Workflow(name=f"hooks.json:{event}", hook=event, steps=tuple(commands))
        )
    return workflows


# --- middleware (non-session events) -----------------------------------------


def _latest_user_text(state) -> str | None:
    """Best-effort: the most recent human message text, for a gate to read."""
    try:
        messages = state.get("messages", []) if hasattr(state, "get") else []
    except Exception:  # noqa: BLE001 - never let context extraction break a turn
        return None
    for msg in reversed(messages):
        role = getattr(msg, "type", None)
        if role is None and isinstance(msg, dict):
            role = msg.get("role")
        if role in ("human", "user"):
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            return content if isinstance(content, str) else None
    return None


class WorkflowMiddleware(AgentMiddleware):
    """Run gated workflows on the per agent/model/tool events. session.start /
    session.end are fired in cli.main() (process scope), not here — same split
    the old ShellHooksMiddleware used."""

    def __init__(self, by_hook: dict[str, list[Workflow]], workspace: Path):
        super().__init__()
        self._by_hook = by_hook
        self._workspace = workspace

    def _fire(self, event: str, prompt: str | None = None) -> None:
        ctx = GateContext(event=event, workspace=self._workspace, prompt=prompt)
        run_hook(self._by_hook.get(event, []), ctx)

    def before_agent(self, state, runtime):
        self._fire("agent.start", prompt=_latest_user_text(state))

    def after_agent(self, state, runtime):
        self._fire("agent.end", prompt=_latest_user_text(state))

    def before_model(self, state, runtime):
        self._fire("model.start", prompt=_latest_user_text(state))

    def after_model(self, state, runtime):
        self._fire("model.end")

    def wrap_tool_call(self, request, handler):
        self._fire("tool.start")
        try:
            return handler(request)
        finally:
            self._fire("tool.end")


def build_workflow_middleware(
    by_hook: dict[str, list[Workflow]], workspace: Path
) -> list[AgentMiddleware]:
    """Middleware for the non-session events (empty when none are declared, so
    nothing is appended — null behavior)."""
    if any(hook not in SESSION_EVENTS for hook in by_hook):
        return [WorkflowMiddleware(by_hook, workspace)]
    return []
