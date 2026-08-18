"""The runner seam (Milestone 8, slice B3).

One method stands between the driver and "a coding harness ran on this
workspace". `HolderRunner` is the only implementation in this milestone; the
protocol exists so tier 2 can point the same dataset at Aider, SWE-agent or
Claude Code without a redesign.

**Patch extraction is deliberately NOT on the protocol.** The driver does it
uniformly for every runner (`harness.bench.patch`), which is what would make a
comparison fair rather than a comparison of whose extractor is better. It is also
why `--emit-patch` is a convenience rather than the mechanism, and why the driver
— not `EPHEMERAL=1` — owns the scratch workspace: ephemeral mode reverts the tree
*on container close*, so only our harness could take a patch from inside it.

`capabilities()` exists so a runner reports what it can measure and the ledger
writes **`null`** for the rest, never an estimate. Only patch, exit code and wall
clock are universal; tokens/cost/tool-calls depend on what a given harness
exposes, and step bounds have no cross-harness equivalent at all.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# What a runner can measure. The driver writes null for anything absent here
# rather than a zero — `null` and `0.0` are different claims (M6's rule).
CAP_TOKENS = "tokens"
CAP_COST = "cost"
CAP_TOOL_CALLS = "tool_calls"
CAP_RUN_ID = "run_id"
CAP_STEP_BOUND = "step_bound"


class RunnerError(RuntimeError):
    """The runner could not be invoked at all (missing launcher, bad platform)."""


@dataclass(frozen=True)
class Limits:
    """The bounds every instance runs under.

    `max_turns` defaults to 1 because a benchmark instance **is** one turn. The
    other two have no default on purpose: `harness bench run` refuses to start
    without them (invariant 7), and a default here would be exactly the silent
    fallback that refusal exists to prevent.
    """

    max_steps: int
    max_seconds: float
    max_turns: int = 1


@dataclass
class RunResult:
    """One instance's run, as the driver sees it."""

    exit_code: int
    payload: dict | None
    duration_ms: float
    stderr_tail: str = ""
    error: str | None = None


class Runner(Protocol):
    """What the driver needs from a coding harness."""

    def invoke(self, workspace: Path, prompt: str, limits: Limits) -> RunResult: ...

    def capabilities(self) -> frozenset[str]: ...


# How much stderr to keep on a failed instance. Enough to diagnose, bounded so a
# runaway log cannot make `runs.jsonl` unreadable (or enormous).
_STDERR_TAIL_CHARS = 4000


def default_launcher(repo_root: Path, platform: str | None = None) -> Path:
    """`run-docker.ps1` on Windows, `run-docker.sh` elsewhere.

    Cross-platform in v1 by launcher selection (§12 fork 4): the primary
    development host for this repo is Windows/PowerShell, so a Linux-first driver
    is one the author cannot run.
    """
    platform = platform if platform is not None else sys.platform
    name = "run-docker.ps1" if platform == "win32" else "run-docker.sh"
    return Path(repo_root) / "deepagent-image" / "scripts" / name


def find_repo_root(start: Path | None = None) -> Path:
    """The harness repo root, found by walking up for `deepagent-image/scripts`.

    Anchored on this file by default, so a sweep works from any CWD.
    """
    here = Path(start) if start is not None else Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "deepagent-image" / "scripts").is_dir():
            return candidate
    raise RunnerError(
        "could not locate the harness repo root (no deepagent-image/scripts above "
        f"{here})"
    )


@dataclass
class HolderRunner:
    """This harness, one container per instance, driven through `run-docker`.

    On the **host**, not inside the container, for three reasons: each instance
    must get a clean container (that *is* the isolation); the whole security
    posture — mask, state dir, netjail, caps — comes along for free rather than
    being re-implemented; and it keeps the driver keyless and stdlib-only.

    Two launch-time decisions are the driver's, not the harness's:

    * **`DEEPAGENTS_WORKFLOWS_DIR` points at a path that does not exist.** The
      loader returns early on a missing directory (§13 item 3), so `git-branch`
      and `git-pr` never run. Without this the session-end commit empties
      `git diff <base>` and the patch is silently lost — and a 50-instance sweep
      would open 50 pull requests.
    * **`STATE_HOST_DIR` is set per instance**, so the telemetry the driver joins
      against is this instance's and nobody else's.
    """

    repo_root: Path
    launcher: Path | None = None
    model: str | None = None
    net_jail: bool = False
    state_root: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_slack_seconds: float = 120.0
    # Injected so the host tier can exercise the whole command construction
    # without Docker. The real sweep leaves it alone.
    run: object = subprocess.run
    platform: str | None = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({CAP_TOKENS, CAP_COST, CAP_TOOL_CALLS, CAP_RUN_ID, CAP_STEP_BOUND})

    # --- command construction (pure, so it is testable without Docker) --------

    def _launcher_path(self) -> Path:
        return self.launcher or default_launcher(self.repo_root, self.platform)

    def _is_windows(self) -> bool:
        platform = self.platform if self.platform is not None else sys.platform
        return platform == "win32"

    def build_command(self, workspace: Path, prompt: str, limits: Limits) -> list[str]:
        """The argv for one instance.

        Every forwarded harness flag is spelled with a **double dash**. Measured
        (§13 item 1): PowerShell binds `--`-prefixed tokens into the launcher's
        `ValueFromRemainingArguments` positional and they reach `main.py` intact,
        while a single-dash `-model` would bind to the launcher's *own* `-Model`
        parameter and never arrive. No stop-parsing token is needed, and `--%`
        would itself be the bug.
        """
        launcher = self._launcher_path()
        forwarded = [
            "--headless",
            "--topic", self._topic(workspace),
            "--max-steps", str(limits.max_steps),
            "--max-seconds", str(limits.max_seconds),
            "--max-turns", str(limits.max_turns),
        ]
        if self.model:
            forwarded += ["--model", self.model]
        forwarded.append(prompt)

        if self._is_windows():
            return [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(launcher),
                "-WorkspacePath", str(workspace),
                *forwarded,
            ]
        return ["bash", str(launcher), *forwarded]

    def _topic(self, workspace: Path) -> str:
        return workspace.name

    def build_env(self, workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.env)
        # The git lifecycle must not run: its commit swallows the diff, and a
        # sweep must not open a PR per instance (invariant 25).
        env["DEEPAGENTS_WORKFLOWS_DIR"] = str(workspace / ".no-workflows-for-bench")
        # Telemetry is the whole point of the join; never let a stray .env off
        # switch silence it mid-sweep.
        env["DEEPAGENTS_TELEMETRY"] = "1"
        env["NET_JAIL"] = "1" if self.net_jail else "0"
        # A benchmark instance must be exactly what its dataset says it is.
        # Measured on the first end-to-end sweep: the launcher's workspace seeding
        # put environment.yml, .gitignore and scripts/run-in-env.sh into the tree,
        # and all three came straight out in the extracted patch -- three harness
        # files a scorer would have been handed alongside the fix. An instance that
        # needs a conda env ships its own environment.yml in its commit.
        env["SEED_WORKSPACE"] = "0"
        if self.state_root is not None:
            env["STATE_HOST_DIR"] = str(Path(self.state_root) / workspace.name)
        if not self._is_windows():
            # The .sh launcher takes its workspace from the environment; the .ps1
            # takes a named parameter (handled in build_command).
            env["WORKSPACE"] = str(workspace)
        return env

    def state_dir_for(self, workspace: Path) -> Path | None:
        """Where this instance's `usage.jsonl` lands on the host, or `None`.

        `None` when the driver did not pin a state root — then the launcher
        derives one from a hash of the workspace path and the driver has no
        business guessing it. The join degrades to whatever the headless payload
        carries rather than reading a file it inferred the location of.
        """
        if self.state_root is None:
            return None
        return Path(self.state_root) / workspace.name

    # --- the invocation -------------------------------------------------------

    def invoke(self, workspace: Path, prompt: str, limits: Limits) -> RunResult:
        cmd = self.build_command(workspace, prompt, limits)
        env = self.build_env(workspace)
        launcher = self._launcher_path()
        if not launcher.is_file():
            raise RunnerError(f"launcher not found: {launcher}")

        # A hard ceiling above the harness's own `--max-seconds`, so a container
        # that never starts (or never dies) cannot wedge the sweep. Slack, not
        # equality: the harness bound is checked at a step boundary and the
        # container has its own start-up cost, so an equal timeout would kill
        # healthy runs that were about to stop themselves.
        timeout = limits.max_seconds + self.timeout_slack_seconds

        started = time.perf_counter()
        try:
            proc = self.run(
                cmd, capture_output=True, text=True, env=env,
                cwd=str(self.repo_root), timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                exit_code=124,
                payload=None,
                duration_ms=(time.perf_counter() - started) * 1000,
                stderr_tail=_tail(getattr(exc, "stderr", "") or ""),
                error=f"the container outlived its {timeout:g}s hard timeout",
            )
        except OSError as exc:
            return RunResult(
                exit_code=127,
                payload=None,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=f"could not launch {launcher}: {exc}",
            )
        duration_ms = (time.perf_counter() - started) * 1000

        payload, parse_error = parse_payload(proc.stdout or "")
        return RunResult(
            exit_code=int(proc.returncode or 0),
            payload=payload,
            duration_ms=duration_ms,
            stderr_tail=_tail(proc.stderr or ""),
            error=parse_error,
        )


def parse_payload(stdout: str) -> tuple[dict | None, str | None]:
    """The headless JSON object from a run's stdout, or `(None, why-not)`.

    Reads the **last** JSON object on stdout rather than the whole stream: stage
    markers go to stderr by contract, but a workflow step or a stray print is not
    something a sweep should die on. Anything unparseable is reported, never
    guessed at.
    """
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "exit_code" in obj:
            return obj, None
    if not lines:
        return None, "the run produced no stdout, so there is no result to read"
    return None, "no headless JSON object found on stdout"


def _tail(text: str) -> str:
    text = text or ""
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return "…" + text[-_STDERR_TAIL_CHARS:]
