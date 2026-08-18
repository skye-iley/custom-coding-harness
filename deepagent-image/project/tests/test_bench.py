"""Tests for harness/bench/{dataset,runner,driver}.py — Milestone 8 B3.

Host tier: stdlib only, no langchain, no model, no network, no Docker. **The
subprocess launch is injected, never run** — what is under test is the sweep's
bookkeeping (resume, per-instance flushing, the join, the refusal to start
unbounded), and a test that needed a container could not run in CI at all.

The rule the whole milestone rests on, restated for this file: *a measurement
that cannot distinguish a harness defect from a model limitation is not a
measurement*. Everything below exists to make one of those two readings a test
failure rather than a judgement call — a resumed sweep that silently drops rows,
a null cost summed as zero, a join written against `thread_id`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from _bootstrap import _load

dataset_mod = _load("harness.bench.dataset")
runner_mod = _load("harness.bench.runner")
driver = _load("harness.bench.driver")
patch_mod = _load("harness.bench.patch")


# --- helpers ------------------------------------------------------------------


def _instance(**over) -> dict:
    base = {
        "instance_id": "gold-001-off-by-one",
        "workspace": "gold/001",
        "task_prompt": "The paginator returns one row too many. Fix it.",
        "base_commit": "HEAD",
        "fail_to_pass": ["pytest tests/test_paginate.py::test_last_page"],
        "pass_to_pass": ["pytest tests/test_paginate.py"],
    }
    base.update(over)
    return base


def _dataset(tmp_path: Path, *objs: dict, name: str = "gold.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(o) for o in objs) + "\n", encoding="utf-8")
    return p


def _payload(**over) -> dict:
    base = {
        "final_message": "done",
        "thread_id": "session-1",
        "run_id": "run-20260817-101010-aaaaaa",
        "topic": "gold-001-off-by-one",
        "usage_log": "/project/state/usage.jsonl",
        "tokens": 120,
        "cost_usd": None,
        "model": "ollama:gemma4",
        "branch": None,
        "pr_url": None,
        "model_patch": None,
        "exit_code": 0,
    }
    base.update(over)
    return base


def _usage_record(**over) -> dict:
    base = {
        "schema": 1,
        "run_id": "run-20260817-101010-aaaaaa",
        "thread_id": "session-1",
        "turn": 1,
        "input": 100, "output": 20, "cache_read": 0, "cache_write": 0,
        "cost_usd": None,
        "duration_ms": 5000, "model_ms": 3000, "tool_ms": 1000,
        "retry_sleep_ms": 0, "paced_sleep_ms": 0, "hitl_wait_ms": 0,
        "model_calls": 3,
        "tool_calls": {"read_file": 2, "write_file": 1},
        "tool_errors": 0,
        "outcome": "ok",
        "stop_reason": None,
        "failed": False,
    }
    base.update(over)
    return base


class FakeRunner:
    """A `Runner` that returns canned results, so the sweep runs without Docker."""

    def __init__(self, results, state_dir=None, caps=None):
        self.results = list(results)
        self.calls = []
        self._state_dir = state_dir
        self._caps = caps or frozenset({"tokens", "cost", "tool_calls", "run_id"})

    def invoke(self, workspace, prompt, limits):
        self.calls.append((Path(workspace), prompt, limits))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def capabilities(self):
        return self._caps

    def state_dir_for(self, workspace):
        return self._state_dir


def _result(**over):
    base = dict(exit_code=0, payload=_payload(), duration_ms=5200.0)
    base.update(over)
    return runner_mod.RunResult(**base)


def _seeded_repo(root: Path, name: str = "001") -> Path:
    """A minimal instance: an initialised repo with one commit, clean tree."""
    ws = root / name
    ws.mkdir(parents=True)
    (ws / "app.py").write_text("VALUE = 1\n")
    for args in (["init", "-q", "."], ["config", "user.email", "t@e.c"],
                 ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(ws), *args], check=True, capture_output=True)
    return ws


needs_git = pytest.mark.skipif(
    not patch_mod.git_available(), reason="no git binary on PATH"
)


# =============================================================================
# dataset
# =============================================================================


def test_a_well_formed_dataset_parses(tmp_path):
    path = _dataset(tmp_path, _instance(), _instance(instance_id="gold-002", workspace="gold/002"))
    instances = dataset_mod.load_dataset(path)
    assert [i.instance_id for i in instances] == ["gold-001-off-by-one", "gold-002"]
    assert instances[0].fail_to_pass == ("pytest tests/test_paginate.py::test_last_page",)
    assert instances[0].repo is None  # tier-3 field, optional today


def test_blank_lines_and_comments_are_skipped(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(
        "# the gold set\n\n" + json.dumps(_instance()) + "\n\n", encoding="utf-8"
    )
    assert len(dataset_mod.load_dataset(path)) == 1


@pytest.mark.parametrize(
    "line, expected",
    [
        ("{not json", "not valid JSON"),
        (json.dumps({"workspace": "x", "task_prompt": "y"}), "missing required field"),
        (json.dumps(_instance(task_promt="typo")), "unknown field"),
        (json.dumps(_instance(fail_to_pass=7)), "must be a string or a list"),
        ("[]", "expected a JSON object"),
    ],
)
def test_a_malformed_line_is_fatal_and_names_its_line(tmp_path, line, expected):
    """Fatal, not skipped.

    A dataset is authored, so a bad line is a bug in the file — and a sweep that
    quietly ran 4 of 5 instances would report a pass rate over a set nobody
    chose. Same class of silent partiality invariant 18 forbids.
    """
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(_instance()) + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(dataset_mod.DatasetError) as exc:
        dataset_mod.load_dataset(path)
    assert expected in str(exc.value)
    assert "d.jsonl:2" in str(exc.value)


def test_a_duplicate_instance_id_is_rejected(tmp_path):
    # Both output files are keyed by it, and invariant 18 requires every instance
    # in each exactly once.
    path = _dataset(tmp_path, _instance(), _instance())
    with pytest.raises(dataset_mod.DatasetError, match="duplicate instance_id"):
        dataset_mod.load_dataset(path)


def test_an_empty_dataset_is_rejected(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(dataset_mod.DatasetError, match="no instances"):
        dataset_mod.load_dataset(path)


def test_workspace_resolves_relative_to_the_dataset_not_the_cwd(tmp_path):
    # So a dataset is relocatable as a unit and a sweep does not depend on where
    # it was launched from.
    sub = tmp_path / "benchmarks"
    sub.mkdir()
    path = _dataset(sub, _instance())
    inst = dataset_mod.load_dataset(path)[0]
    assert inst.resolve_workspace(path) == (sub / "gold" / "001").resolve()


def test_select_applies_only_then_limit(tmp_path):
    path = _dataset(
        tmp_path,
        _instance(instance_id="a", workspace="a"),
        _instance(instance_id="b", workspace="b"),
        _instance(instance_id="c", workspace="c"),
    )
    instances = dataset_mod.load_dataset(path)
    assert [i.instance_id for i in dataset_mod.select(instances, limit=2)] == ["a", "b"]
    assert [i.instance_id for i in dataset_mod.select(instances, only=("c", "a"))] == ["a", "c"]


def test_an_only_id_that_matches_nothing_is_an_error(tmp_path):
    """Not an empty sweep. The usual cause is a typo, and silently running zero
    instances then reporting a clean sweep is the worst possible answer."""
    path = _dataset(tmp_path, _instance(instance_id="a", workspace="a"))
    with pytest.raises(dataset_mod.DatasetError, match="unknown instance"):
        dataset_mod.select(dataset_mod.load_dataset(path), only=("typo",))


# =============================================================================
# runner: command construction (pure — no Docker)
# =============================================================================


def _runner(tmp_path, **over):
    launcher = tmp_path / "run-docker.sh"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    kwargs = dict(repo_root=tmp_path, launcher=launcher, platform="linux")
    kwargs.update(over)
    return runner_mod.HolderRunner(**kwargs)


def test_every_forwarded_flag_is_spelled_with_a_double_dash(tmp_path):
    """§13 item 1, measured on PowerShell 5.1 and pinned here.

    A `--`-prefixed token binds to the launcher's `ValueFromRemainingArguments`
    positional and reaches `main.py` intact. A **single**-dash `-model` binds to
    the launcher's own `-Model` parameter and never arrives — silently, with the
    sweep then running whatever the container auto-selected.
    """
    r = _runner(tmp_path, model="ollama:gemma4")
    cmd = r.build_command(tmp_path / "ws", "fix it", runner_mod.Limits(40, 600))
    forwarded = [a for a in cmd if a.startswith("-")]
    single_dash = [a for a in forwarded if not a.startswith("--")]
    assert not single_dash, f"single-dash flags would bind to the launcher: {single_dash}"
    assert "--headless" in cmd
    assert cmd[cmd.index("--max-steps") + 1] == "40"
    assert cmd[cmd.index("--model") + 1] == "ollama:gemma4"
    assert cmd[-1] == "fix it"


def test_the_stop_parsing_token_is_never_used(tmp_path):
    # `--%` was named as a fallback in an early draft and would itself have been
    # the bug: it lands as a literal argument and collapses the rest into one
    # string (§13 item 1's fifth measured case).
    r = _runner(tmp_path, platform="win32", launcher=tmp_path / "run-docker.ps1")
    (tmp_path / "run-docker.ps1").write_text("", encoding="utf-8")
    assert "--%" not in r.build_command(tmp_path / "ws", "task", runner_mod.Limits(40, 600))


def test_the_launcher_is_chosen_by_platform(tmp_path):
    assert runner_mod.default_launcher(tmp_path, "win32").name == "run-docker.ps1"
    assert runner_mod.default_launcher(tmp_path, "linux").name == "run-docker.sh"
    assert runner_mod.default_launcher(tmp_path, "darwin").name == "run-docker.sh"


def test_the_windows_launcher_takes_the_workspace_as_a_parameter(tmp_path):
    # The .ps1 has a named -WorkspacePath; the .sh reads $WORKSPACE. Asymmetric
    # by the launchers' own design, so the runner has to know both.
    (tmp_path / "run-docker.ps1").write_text("", encoding="utf-8")
    r = _runner(tmp_path, platform="win32", launcher=tmp_path / "run-docker.ps1")
    ws = tmp_path / "ws"
    cmd = r.build_command(ws, "task", runner_mod.Limits(40, 600))
    assert cmd[cmd.index("-WorkspacePath") + 1] == str(ws)
    assert "WORKSPACE" not in r.build_env(ws)


def test_the_posix_launcher_takes_the_workspace_from_the_environment(tmp_path):
    r = _runner(tmp_path)
    ws = tmp_path / "ws"
    assert r.build_env(ws)["WORKSPACE"] == str(ws)


def test_the_git_lifecycle_is_pointed_at_a_directory_that_does_not_exist(tmp_path):
    """Invariant 25, at the seam.

    The loader returns early on a missing directory (§13 item 3), so `git-branch`
    and `git-pr` never run. Without this the session-end commit empties
    `git diff <base>` and the patch is silently lost — and a 50-instance sweep
    would open 50 pull requests.
    """
    r = _runner(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    workflows = Path(r.build_env(ws)["DEEPAGENTS_WORKFLOWS_DIR"])
    assert not workflows.exists()


def test_telemetry_is_forced_on_for_a_sweep(tmp_path):
    # The join is the point; a stray `DEEPAGENTS_TELEMETRY=0` in someone's .env
    # must not silently produce a sweep with no numbers in it.
    r = _runner(tmp_path, env={"DEEPAGENTS_TELEMETRY": "0"})
    assert r.build_env(tmp_path / "ws")["DEEPAGENTS_TELEMETRY"] == "1"


def test_the_state_dir_is_pinned_per_instance_when_a_root_is_given(tmp_path):
    r = _runner(tmp_path, state_root=tmp_path / "state")
    ws = tmp_path / "ws" / "gold-001"
    assert r.build_env(ws)["STATE_HOST_DIR"] == str(tmp_path / "state" / "gold-001")
    assert r.state_dir_for(ws) == tmp_path / "state" / "gold-001"


def test_without_a_state_root_the_driver_does_not_guess_where_telemetry_landed(tmp_path):
    """The launcher derives the state dir from a hash of the workspace path. The
    driver re-deriving that hash would be a third mirror of launcher logic; it
    reports `None` instead and the join degrades to the payload."""
    r = _runner(tmp_path)
    assert r.state_dir_for(tmp_path / "ws") is None
    assert "STATE_HOST_DIR" not in r.build_env(tmp_path / "ws")


def test_net_jail_is_passed_through_rather_than_assumed_off(tmp_path):
    # Tier 1 does not need it, but must not contradict the anti-cheat posture
    # tier 3 inherits (§7).
    assert _runner(tmp_path, net_jail=True).build_env(tmp_path / "ws")["NET_JAIL"] == "1"
    assert _runner(tmp_path).build_env(tmp_path / "ws")["NET_JAIL"] == "0"


def test_a_missing_launcher_raises_rather_than_failing_every_instance(tmp_path):
    r = runner_mod.HolderRunner(repo_root=tmp_path, launcher=tmp_path / "nope.sh",
                                platform="linux")
    with pytest.raises(runner_mod.RunnerError, match="launcher not found"):
        r.invoke(tmp_path, "task", runner_mod.Limits(40, 600))


def test_the_hard_timeout_sits_above_the_harness_bound(tmp_path):
    # Slack, not equality: the harness bound is checked at a step boundary and the
    # container has its own start-up cost, so an equal timeout would kill healthy
    # runs that were about to stop themselves.
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(_payload()), "")

    r = _runner(tmp_path, run=fake_run)
    r.invoke(tmp_path, "task", runner_mod.Limits(40, 600))
    assert seen["timeout"] > 600


def test_a_container_that_outlives_its_timeout_is_recorded_not_raised(tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    result = _runner(tmp_path, run=fake_run).invoke(
        tmp_path, "task", runner_mod.Limits(40, 600)
    )
    assert result.payload is None
    assert "hard timeout" in result.error


# --- payload parsing ----------------------------------------------------------


def test_the_payload_is_read_off_stdout(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps(_payload(final_message="ok")) + "\n", "[harness] thinking\n"
        )

    result = _runner(tmp_path, run=fake_run).invoke(tmp_path, "task", runner_mod.Limits(40, 600))
    assert result.payload["final_message"] == "ok"
    assert result.error is None


def test_a_stray_line_before_the_payload_does_not_break_the_parse():
    obj, err = runner_mod.parse_payload(
        "some workflow printed this\n" + json.dumps(_payload()) + "\n"
    )
    assert obj is not None and err is None


def test_no_payload_is_reported_rather_than_guessed_at():
    obj, err = runner_mod.parse_payload("")
    assert obj is None and "no stdout" in err
    obj, err = runner_mod.parse_payload("nothing structured here\n")
    assert obj is None and "no headless JSON" in err


# =============================================================================
# the join (invariants 19-22)
# =============================================================================


def test_the_join_key_is_run_id_never_thread_id(tmp_path):
    """Invariant 19, with a fixture in which two instances SHARE a thread_id.

    `thread_id` repeats across resumes and is explicitly not the `past.sqlite`
    key. A driver written against it — the older, more obvious field — merges two
    instances silently, and the merged row looks perfectly plausible.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "usage.jsonl").write_text(
        json.dumps(_usage_record(run_id="run-A", thread_id="shared", input=100)) + "\n"
        + json.dumps(_usage_record(run_id="run-B", thread_id="shared", input=999)) + "\n",
        encoding="utf-8",
    )
    records = driver.read_usage_records(state, "run-A")
    assert [r["input"] for r in records] == [100]


def test_a_row_carries_the_decomposition_and_the_tool_mix(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "usage.jsonl").write_text(json.dumps(_usage_record()) + "\n", encoding="utf-8")
    records = driver.read_usage_records(state, "run-20260817-101010-aaaaaa")

    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_run_row(
        inst, _result(), patch="diff --git a/x b/x\n", records=records,
        capabilities=frozenset({"tokens", "cost", "tool_calls", "run_id"}),
        started_at="A", ended_at="B",
    )
    assert row["run_id"] == "run-20260817-101010-aaaaaa"
    assert row["tool_calls"] == {"read_file": 2, "write_file": 1}
    assert row["tokens"]["input"] == 100
    assert row["time"]["model_ms"] == 3000
    # Stored, not left for a reader to recompute -- an explicit residual is
    # auditable, an implicit one is invisible (invariant 22).
    assert row["time"]["residual_ms"] == 5000 - (3000 + 1000)


def test_a_null_cost_survives_as_null(tmp_path):
    """Invariant 21. On the free local model — the benchmark's default case —
    `cost_usd` is null, and summing null as zero would report a $0 sweep as a
    *priced* one. `null` and `0.0` are different claims."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "usage.jsonl").write_text(
        json.dumps(_usage_record(cost_usd=None)) + "\n", encoding="utf-8"
    )
    records = driver.read_usage_records(state, "run-20260817-101010-aaaaaa")
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_run_row(
        inst, _result(), patch="", records=records,
        capabilities=frozenset({"tokens", "cost", "tool_calls"}),
        started_at="A", ended_at="B",
    )
    assert row["cost_usd"] is None


def test_a_metric_a_runner_cannot_measure_is_null_never_zero():
    """`capabilities()`'s whole purpose. A runner that exposes no token counts
    must leave the field NULL — an estimate or a zero would make a cross-harness
    comparison a comparison of who guesses more confidently."""
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_run_row(
        inst, _result(), patch="", records=[_usage_record()],
        capabilities=frozenset(),  # a runner that measures nothing
        started_at="A", ended_at="B",
    )
    assert row["tokens"] is None
    assert row["cost_usd"] is None
    assert row["tool_calls"] is None


def test_a_stopped_instance_carries_its_stop_reason_into_the_ledger():
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_run_row(
        inst, _result(exit_code=43), patch="",
        records=[_usage_record(outcome="stopped", stop_reason="steps")],
        capabilities=frozenset({"tokens"}), started_at="A", ended_at="B",
    )
    assert (row["outcome"], row["stop_reason"]) == ("stopped", "steps")


def test_a_run_with_no_telemetry_still_produces_a_row():
    # Telemetry off, or a crash before the sink existed. The row must still exist
    # and be honest about what is missing rather than being skipped.
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_run_row(
        inst, _result(exit_code=1, payload=None), patch=None, records=[],
        capabilities=frozenset({"tokens", "cost", "tool_calls"}),
        started_at="A", ended_at="B",
    )
    assert row["instance_id"] == "gold-001-off-by-one"
    assert row["outcome"] is None and row["tokens"] is None
    assert row["patch_empty"] is True


# =============================================================================
# predictions (invariant 13)
# =============================================================================


def test_a_prediction_carries_exactly_three_keys():
    """Anything extra risks a scorer rejecting the file. Everything else the
    harness knows belongs in `runs.jsonl`."""
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_prediction_row(inst, "ollama:gemma4", "diff --git a/x b/x\n")
    assert set(row) == {"instance_id", "model_name_or_path", "model_patch"}
    assert row["model_patch"].startswith("diff --git")


def test_a_missing_patch_is_an_empty_string_not_a_null():
    # The official format expects a string; a null would make a scorer's own
    # parse fail rather than score zero.
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    assert driver.build_prediction_row(inst, None, None)["model_patch"] == ""
    assert driver.build_prediction_row(inst, None, None)["model_name_or_path"] == "unknown"


# =============================================================================
# the sweep: resume, per-instance flush, failure isolation
# =============================================================================


@needs_git
def test_a_sweep_writes_one_prediction_and_one_run_row_per_instance(tmp_path):
    bench = tmp_path / "b"
    _seeded_repo(bench, "001")
    _seeded_repo(bench, "002")
    ds = _dataset(
        bench,
        _instance(instance_id="a", workspace="001"),
        _instance(instance_id="b", workspace="002"),
    )
    out = tmp_path / "out"
    runner = FakeRunner([_result(), _result()])

    rc = driver.run_sweep(
        ds, out, limits=runner_mod.Limits(40, 600), runner=runner,
        scratch_root=tmp_path / "scratch",
    )
    assert rc == 0
    preds = _read(out / "predictions.jsonl")
    runs = _read(out / "runs.jsonl")
    assert [p["instance_id"] for p in preds] == ["a", "b"]
    assert [r["instance_id"] for r in runs] == ["a", "b"]


@needs_git
def test_a_sweep_resumes_and_does_not_duplicate(tmp_path):
    """Invariant 15 and 18 together: killed at instance k of n and re-run, it
    skips the first k and completes the rest — with no row written twice."""
    bench = tmp_path / "b"
    for name in ("001", "002", "003"):
        _seeded_repo(bench, name)
    ds = _dataset(
        bench,
        _instance(instance_id="a", workspace="001"),
        _instance(instance_id="b", workspace="002"),
        _instance(instance_id="c", workspace="003"),
    )
    out = tmp_path / "out"
    limits = runner_mod.Limits(40, 600)

    # First pass: only two results available, so the third instance raises and
    # the sweep stops the way a kill would leave it.
    first = FakeRunner([_result(), _result(), KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        driver.run_sweep(ds, out, limits=limits, runner=first,
                         scratch_root=tmp_path / "scratch")
    assert len(_read(out / "predictions.jsonl")) == 2  # flushed per instance

    second = FakeRunner([_result()])
    assert driver.run_sweep(ds, out, limits=limits, runner=second,
                            scratch_root=tmp_path / "scratch") == 0
    assert len(second.calls) == 1, "a resumed sweep re-ran an instance it had done"
    ids = [p["instance_id"] for p in _read(out / "predictions.jsonl")]
    assert ids == ["a", "b", "c"]
    assert len(ids) == len(set(ids))


@needs_git
def test_one_instance_failing_never_aborts_the_sweep(tmp_path):
    """Invariant 16. A crashed, stopped or timed-out instance yields a prediction
    with an empty `model_patch` and a ledger row carrying its outcome, and the
    driver continues."""
    bench = tmp_path / "b"
    _seeded_repo(bench, "001")
    _seeded_repo(bench, "002")
    ds = _dataset(
        bench,
        _instance(instance_id="a", workspace="001"),
        _instance(instance_id="b", workspace="002"),
    )
    out = tmp_path / "out"
    runner = FakeRunner([RuntimeError("docker daemon is not running"), _result()])

    assert driver.run_sweep(ds, out, limits=runner_mod.Limits(40, 600), runner=runner,
                            scratch_root=tmp_path / "scratch") == 0
    preds = _read(out / "predictions.jsonl")
    runs = _read(out / "runs.jsonl")
    assert [p["instance_id"] for p in preds] == ["a", "b"]
    assert preds[0]["model_patch"] == ""
    assert "docker daemon" in runs[0]["error"]


@needs_git
def test_an_instance_whose_workspace_is_missing_still_gets_both_rows(tmp_path):
    # A sweep must account for every instance in the dataset exactly once; a
    # missing row is indistinguishable from one nobody looked at.
    bench = tmp_path / "b"
    _seeded_repo(bench, "001")
    ds = _dataset(
        bench,
        _instance(instance_id="a", workspace="001"),
        _instance(instance_id="gone", workspace="nowhere"),
    )
    out = tmp_path / "out"
    runner = FakeRunner([_result()])
    assert driver.run_sweep(ds, out, limits=runner_mod.Limits(40, 600), runner=runner,
                            scratch_root=tmp_path / "scratch") == 0
    ids = [p["instance_id"] for p in _read(out / "predictions.jsonl")]
    assert ids == ["a", "gone"]
    assert "does not exist" in _read(out / "runs.jsonl")[1]["error"]


@needs_git
def test_a_plain_source_with_no_git_gets_a_base_commit_in_the_scratch_copy(tmp_path):
    """§13 item 6 / §0.1 item 20. A gold-set instance ships as plain files, not
    its own repo (a nested `.git` does not survive being tracked as ordinary
    content — see `milestone8_next_session.md` §1). `prepare_workspace` must
    still hand back something `patch.extract_patch` can diff against."""
    bench = tmp_path / "b"
    plain = bench / "001"
    plain.mkdir(parents=True)
    (plain / "a.py").write_text("x = 1\n")
    ds = _dataset(bench, _instance(instance_id="a", workspace="001"))
    out = tmp_path / "out"

    class _EditingRunner(FakeRunner):
        def invoke(self, workspace, prompt, limits):
            (Path(workspace) / "a.py").write_text("x = 2\n")
            return super().invoke(workspace, prompt, limits)

    driver.run_sweep(ds, out, limits=runner_mod.Limits(40, 600),
                     runner=_EditingRunner([_result()]), scratch_root=tmp_path / "scratch")
    row = _read(out / "runs.jsonl")[0]
    assert row["error"] is None
    patch = _read(out / "predictions.jsonl")[0]["model_patch"]
    assert "x = 2" in patch


@needs_git
def test_prepare_workspace_creates_a_base_commit_with_deterministic_identity(tmp_path, monkeypatch):
    """No git identity configured on the runner must not fail the sweep at
    instance 1, and the base SHA must not vary run to run for the same
    instance content."""
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)

    bench = tmp_path / "b"
    plain = bench / "001"
    plain.mkdir(parents=True)
    (plain / "a.py").write_text("x = 1\n")

    target_a = driver.prepare_workspace(plain, tmp_path / "scratch" / "a")
    target_b = driver.prepare_workspace(plain, tmp_path / "scratch" / "b")

    assert patch_mod.resolve_base(target_a) == patch_mod.resolve_base(target_b)


@needs_git
def test_a_source_missing_entirely_is_still_reported_by_name(tmp_path):
    """A typo'd `workspace` path (not merely "no `.git`") must still surface as
    an error naming the instance, not as a silently created empty repo."""
    bench = tmp_path / "b"
    bench.mkdir()
    ds = _dataset(bench, _instance(instance_id="a", workspace="nowhere"))
    out = tmp_path / "out"
    driver.run_sweep(ds, out, limits=runner_mod.Limits(40, 600),
                     runner=FakeRunner([]), scratch_root=tmp_path / "scratch")
    assert "does not exist" in _read(out / "runs.jsonl")[0]["error"]


@needs_git
def test_the_scratch_copy_keeps_git_and_drops_conda(tmp_path):
    """Invariant 12b. `.git` surviving the copy is what the whole patch design
    rests on; `.conda` is a rebuildable environment that can be gigabytes."""
    src = _seeded_repo(tmp_path / "b", "001")
    (src / ".conda").mkdir()
    (src / ".conda" / "marker").write_text("x" * 100)

    target = driver.prepare_workspace(src, tmp_path / "scratch" / "a")
    assert (target / ".git").is_dir()
    assert not (target / ".conda").exists()
    # And the copy is a usable repo: there is a base to diff against.
    assert patch_mod.resolve_base(target) == patch_mod.resolve_base(src)


@needs_git
def test_a_source_that_already_ships_git_is_copied_as_is_not_reinitialised(tmp_path):
    """A foreign/tier-3 instance with its own history keeps its own base SHA —
    the driver must not overwrite it with a fresh commit."""
    src = _seeded_repo(tmp_path / "b", "001")
    expected = patch_mod.resolve_base(src)
    target = driver.prepare_workspace(src, tmp_path / "scratch" / "a")
    assert patch_mod.resolve_base(target) == expected


@needs_git
def test_the_scratch_copy_is_discarded_after_each_instance(tmp_path):
    bench = tmp_path / "b"
    _seeded_repo(bench, "001")
    ds = _dataset(bench, _instance(instance_id="a", workspace="001"))
    scratch = tmp_path / "scratch"
    driver.run_sweep(ds, tmp_path / "out", limits=runner_mod.Limits(40, 600),
                     runner=FakeRunner([_result()]), scratch_root=scratch)
    assert not (scratch / "a").exists()


@needs_git
def test_the_patch_a_sweep_records_comes_from_the_scratch_tree(tmp_path):
    """The end-to-end shape of invariant 12a: no `--emit-patch` anywhere.

    The fake runner edits the workspace the way a container would, and the driver
    extracts the patch itself — which is exactly what a foreign harness would
    require.
    """
    bench = tmp_path / "b"
    _seeded_repo(bench, "001")
    ds = _dataset(bench, _instance(instance_id="a", workspace="001"))
    out = tmp_path / "out"

    class _EditingRunner(FakeRunner):
        def invoke(self, workspace, prompt, limits):
            (Path(workspace) / "app.py").write_text("VALUE = 2\n")
            (Path(workspace) / "new.py").write_text("NEW = True\n")
            return super().invoke(workspace, prompt, limits)

    driver.run_sweep(ds, out, limits=runner_mod.Limits(40, 600),
                     runner=_EditingRunner([_result()]),
                     scratch_root=tmp_path / "scratch")
    patch = _read(out / "predictions.jsonl")[0]["model_patch"]
    assert set(patch_mod.changed_paths(patch)) == {"app.py", "new.py"}


@needs_git
def test_a_dry_run_writes_nothing(tmp_path):
    bench = tmp_path / "b"
    _seeded_repo(bench, "001")
    ds = _dataset(bench, _instance(instance_id="a", workspace="001"))
    out = tmp_path / "out"
    runner = FakeRunner([])
    assert driver.run_sweep(ds, out, limits=runner_mod.Limits(40, 600), runner=runner,
                            scratch_root=tmp_path / "scratch", dry_run=True) == 0
    assert runner.calls == []
    assert not (out / "predictions.jsonl").exists()


def test_a_half_written_last_line_does_not_break_resume(tmp_path):
    # Exactly the state a killed sweep leaves. Skipping the truncated line is
    # right; the instance simply gets re-run.
    preds = tmp_path / "predictions.jsonl"
    preds.write_text(
        json.dumps({"instance_id": "a", "model_name_or_path": "m", "model_patch": ""}) + "\n"
        + '{"instance_id": "b", "model_pa',
        encoding="utf-8",
    )
    assert driver.completed_instance_ids(preds) == {"a"}


# =============================================================================
# the bounds refusal (invariant 7) and `bench show`
# =============================================================================


@pytest.mark.parametrize("argv", [
    ["run", "d.jsonl"],
    ["run", "d.jsonl", "--max-steps", "40"],
    ["run", "d.jsonl", "--max-seconds", "600"],
])
def test_bench_run_refuses_to_start_without_both_bounds(argv, capsys):
    """Invariant 7. An unbounded sweep is the failure mode this milestone exists
    to remove; it must not be reachable by forgetting a flag. Exits non-zero and
    writes nothing."""
    with pytest.raises(SystemExit) as exc:
        driver.bench_main(argv)
    assert exc.value.code != 0
    assert "required" in capsys.readouterr().err


def test_bench_run_rejects_a_nonsensical_bound(capsys):
    with pytest.raises(SystemExit):
        driver.bench_main(["run", "d.jsonl", "--max-steps", "0", "--max-seconds", "600"])


def test_show_reports_empty_patches_prominently(tmp_path):
    """Invariant 17. An all-empty sweep must be LOUD — silence there is the §0
    failure mode reproduced inside the instrument."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "predictions.jsonl").write_text(
        "\n".join(json.dumps({"instance_id": i, "model_name_or_path": "m", "model_patch": ""})
                  for i in ("a", "b")) + "\n",
        encoding="utf-8",
    )
    (out / "runs.jsonl").write_text(
        "\n".join(json.dumps({
            "instance_id": i, "outcome": "ok", "duration_ms": 1000,
            "time": {"model_ms": 600, "tool_ms": 100, "retry_sleep_ms": 0,
                     "paced_sleep_ms": 0, "hitl_wait_ms": 0, "residual_ms": 300},
            "cost_usd": None, "model": "ollama:gemma4",
        }) for i in ("a", "b")) + "\n",
        encoding="utf-8",
    )
    summary = driver.summarize(out)
    assert summary["empty_patches"] == 2
    text = driver.render_show(summary)
    assert "empty patches  2" in text
    assert "nothing to score" in text
    # Invariant 21 again, at the report: a free sweep is not a $0.00 sweep.
    assert "not priced" in text
    # Invariant 22: the residual is surfaced, not hidden.
    assert "residual" in text
    # And the two clocks stay apart. Measured on the first real sweep: the
    # container lived 30.9s while the harness measured 14.8s of turn, so a single
    # "wall clock" number silently swallowed 16s of container start-up -- the
    # exact shape of gap invariant 22 exists to make visible.
    assert "container launch" in text and "harness" in text


def test_show_names_the_bound_a_sweep_was_measuring(tmp_path):
    # A sweep that is mostly stopped/steps is reporting the bound, not the
    # harness (§8), and `bench show` has to say so.
    out = tmp_path / "out"
    out.mkdir()
    (out / "predictions.jsonl").write_text(
        json.dumps({"instance_id": "a", "model_name_or_path": "m", "model_patch": "x"}) + "\n",
        encoding="utf-8",
    )
    (out / "runs.jsonl").write_text(
        json.dumps({"instance_id": "a", "outcome": "stopped", "stop_reason": "steps",
                    "duration_ms": 1000, "time": {}, "cost_usd": None}) + "\n",
        encoding="utf-8",
    )
    text = driver.render_show(driver.summarize(out))
    assert "stopped=1" in text and "steps=1" in text


def test_show_on_an_empty_directory_does_not_crash(tmp_path):
    assert "instances      0" in driver.render_show(driver.summarize(tmp_path))


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_launcher_does_not_seed_a_bench_workspace(tmp_path):
    """A benchmark instance must be exactly what its dataset says it is.

    Measured on the first end-to-end sweep, not reasoned: the launcher's
    workspace seeding wrote `environment.yml`, `.gitignore` and
    `scripts/run-in-env.sh` into the tree, and all three came straight out in the
    extracted patch — three harness files a scorer would have been handed
    alongside the fix, on every instance. An instance that needs a conda env
    ships its own `environment.yml` in its commit.
    """
    r = _runner(tmp_path)
    assert r.build_env(tmp_path / "ws")["SEED_WORKSPACE"] == "0"


def test_a_row_records_both_the_process_and_the_harness_exit_code():
    """They should agree, and the first real sweep is where they did not.

    `run-docker.ps1` ended inside a `try/finally` without re-raising
    `$LASTEXITCODE`, so every instance the step bound stopped — harness exit 43 —
    reached the driver as a clean 0. A ledger that carried only one of the two
    could not have shown that, and the sweep would have read as five successful
    runs that happened to produce two empty patches.
    """
    inst = dataset_mod.parse_instance(_instance(), "x:1")
    row = driver.build_run_row(
        inst, _result(exit_code=0, payload=_payload(exit_code=43)), patch="",
        records=[], capabilities=frozenset(), started_at="A", ended_at="B",
    )
    assert row["exit_code"] == 0
    assert row["harness_exit_code"] == 43


def test_both_launchers_propagate_the_container_exit_code():
    """A textual guard, because a sweep cannot see this any other way.

    The `.sh` has always ended `exit $?`. The `.ps1` did not, and the whole
    container exit code was silently discarded on the platform this repo is
    primarily developed on.
    """
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    sh = (scripts / "run-docker.sh").read_text(encoding="utf-8")
    ps1 = (scripts / "run-docker.ps1").read_text(encoding="utf-8")
    assert "exit $?" in sh
    assert "$LASTEXITCODE" in ps1 and "exit $dockerExit" in ps1
