"""Tests for harness/telemetry.py — Milestone 6 T1/T3 (record, derivation, renderers).

Host tier: stdlib only, no langchain, no model, no network. Everything that needs
the agent runtime (the middleware, ``run_turn``'s write, the ``_batch_payload``
join keys) is exercised in the image tier from ``test_cli.py``.

The invariants under test here are the ones a stub *can* check honestly: record
shape and nullability (5), derivation arithmetic (6), the wall-clock
decomposition (4a), containment (10/11/12), and that the module is stdlib-weight
(22). Absolute metric accuracy is deliberately not asserted — there is no second
source for "what this run cost", so the invariants pin internal consistency and
leave the pricing math to M1's own tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from _bootstrap import _load

tm = _load("harness.telemetry")

_HARNESS = Path(__file__).resolve().parent.parent / "harness"


def _record(**over) -> "tm.TurnRecord":
    base = dict(
        run_id="run-20260812-143013-a1b2c3",
        thread_id="session-20260812-143013",
        topic="django__django-11099",
        turn=1,
        provider="ollama",
        model="gemma4",
        input=100,
        output=20,
        duration_ms=1000,
        model_ms=600,
        tool_ms=200,
        model_calls=2,
        tool_calls={"read_file": 2},
    )
    base.update(over)
    return tm.TurnRecord(**base)


# --- record shape -------------------------------------------------------------


def test_schema_key_is_present_and_first():
    # A reader that finds an unknown schema version must be able to skip the line
    # rather than mis-parse it — which only works if the key is always there.
    d = _record().to_dict()
    assert next(iter(d)) == "schema"
    assert d["schema"] == tm.SCHEMA_VERSION


def test_every_record_carries_the_same_key_set(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1), env={})
    tm.record_turn(sink, _record(turn=2, outcome=tm.OUTCOME_ERROR, tool_calls={}), env={})
    records = tm.read_records(sink)
    assert len(records) == 2
    assert set(records[0]) == set(records[1])


def test_record_has_no_field_for_prompt_or_reply_or_tool_args():
    """Invariant 10, structurally: containment by *absence of a field*, not by a
    filter. The same guard audit.py applies by dropping ``context`` outright."""
    keys = set(_record().to_dict())
    for forbidden in ("prompt", "content", "messages", "response", "args", "tool_args", "text"):
        assert forbidden not in keys


def test_unpriced_model_records_null_cost_not_zero():
    # Invariant 5. `0.0` reads as "free"; `null` reads as "nothing priced this".
    # They are different claims and the schema must not conflate them.
    d = _record().to_dict()
    assert d["cost_usd"] is None
    assert d["cost_provenance"] is None


def test_tool_calls_is_an_object_never_null():
    assert _record(tool_calls={}).to_dict()["tool_calls"] == {}
    assert _record(tool_calls=None).to_dict()["tool_calls"] == {}


def test_paced_sleep_is_zero_not_null_when_unpaced():
    # With no tier selected there is no limiter at all, so 0 here means "not
    # paced" — which is true, and different from "unknown".
    assert _record().to_dict()["paced_sleep_ms"] == 0


def test_ms_fields_are_non_negative_floor_rounded_ints():
    d = _record(duration_ms=1200.7, model_ms=-5).to_dict()
    assert d["duration_ms"] == 1200
    assert d["model_ms"] == 0
    assert all(isinstance(d[k], int) for k in ("duration_ms", "model_ms", "tool_ms"))


def test_topic_is_null_when_unset():
    assert _record(topic=None).to_dict()["topic"] is None


# --- the sink -----------------------------------------------------------------


def test_records_are_append_only_and_line_delimited(tmp_path):
    sink = tmp_path / "usage.jsonl"
    for i in range(3):
        tm.record_turn(sink, _record(turn=i), env={})
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["turn"] == i for i, line in enumerate(lines))


def test_two_runs_separate_by_field_not_by_file_position(tmp_path):
    """Invariant 25: a parallel sweep interleaves lines in one file, so a run's
    records must be recoverable by ``run_id``, never by position."""
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(run_id="run-a", turn=1), env={})
    tm.record_turn(sink, _record(run_id="run-b", turn=1), env={})
    tm.record_turn(sink, _record(run_id="run-a", turn=2), env={})
    assert [r["turn"] for r in tm.read_records(sink, run_id="run-a")] == [1, 2]
    assert [r["run_id"] for r in tm.read_records(sink, run_id="run-b")] == ["run-b"]


def test_reader_skips_a_torn_or_unknown_schema_line(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1), env={})
    with sink.open("a", encoding="utf-8") as fh:
        fh.write('{"schema": 99, "run_id": "future"}\n')
        fh.write('{"schema": 1, "run_i\n')  # torn mid-write
    tm.record_turn(sink, _record(turn=2), env={})
    assert [r["turn"] for r in tm.read_records(sink)] == [1, 2]


def test_read_records_on_a_missing_sink_is_empty(tmp_path):
    assert tm.read_records(tmp_path / "nope.jsonl") == []


def test_sink_creates_its_parent_directory(tmp_path):
    sink = tmp_path / "state" / "usage.jsonl"
    tm.record_turn(sink, _record(), env={})
    assert sink.is_file()


def test_paths_live_beside_each_other_in_the_state_dir(tmp_path):
    # Invariant 14 in its module-local half: both files resolve under the state
    # dir it is handed. That the state dir is outside the workspace is cli's job
    # (and doctor's check), not a second guard here — invariant 19.
    assert tm.usage_path(tmp_path) == tmp_path / "usage.jsonl"
    assert tm.session_path(tmp_path) == tmp_path / "session.json"


# --- containment --------------------------------------------------------------


def test_planted_secret_is_scrubbed_out_of_a_record(tmp_path):
    sink = tmp_path / "usage.jsonl"
    env = {"MY_API_KEY": "abcdef123456xyz"}
    written = tm.record_turn(
        sink, _record(topic="job abcdef123456xyz", tool_calls={"sk-abcdefghijklmnopqrstuv": 1}), env
    )
    blob = json.dumps(written)
    assert "abcdef123456xyz" not in blob
    assert "sk-abcdefghijklmnopqrstuv" not in blob
    assert "REDACTED" in blob


def test_a_secret_in_a_turn_record_cannot_reach_the_pr_body(tmp_path):
    """Invariant 12. Two independent reasons it cannot: the record was scrubbed on
    the way in, and the block is built from the summary, which has no free-text
    field. Assert the end-to-end path, not either half."""
    sink = tmp_path / "usage.jsonl"
    env = {"MY_API_KEY": "abcdef123456xyz"}
    tm.record_turn(sink, _record(topic="abcdef123456xyz"), env)
    summary = tm.derive_session(tm.read_records(sink), usage_log=str(sink))
    body = tm.render_pr_block(summary)
    assert "abcdef123456xyz" not in body


# --- derivation ---------------------------------------------------------------


def _three_turns(tmp_path) -> Path:
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1, input=100, output=10, duration_ms=1000,
                                 model_ms=600, tool_ms=200, tool_calls={"read_file": 2}), env={})
    tm.record_turn(sink, _record(turn=2, input=200, output=20, duration_ms=2000,
                                 model_ms=1200, tool_ms=400, tool_calls={"read_file": 1, "execute": 1},
                                 tool_errors=1, retry_count=1, retry_sleep_ms=100), env={})
    tm.record_turn(sink, _record(turn=3, input=300, output=30, duration_ms=3000,
                                 model_ms=1500, tool_ms=500, tool_calls={"execute": 2},
                                 outcome=tm.OUTCOME_ERROR, context_trimmed=True), env={})
    return sink


# --- outcome vs. failure (invariant 2a, generalized) --------------------------


def test_failed_is_derived_from_outcome_and_only_error_counts():
    """`failed` is a view of `outcome`, so the two cannot disagree.

    The distinction is the whole point of the field: a budget cap firing, an
    operator's Ctrl-C, and a fail-closed headless abort are the harness doing what
    it was configured to do. Counting them as failures puts a governance signal in
    the reliability column, which is the mistake invariant 2a already rejects for
    an operator deny.
    """
    assert _record(outcome=tm.OUTCOME_ERROR).failed is True
    for good in (tm.OUTCOME_OK, tm.OUTCOME_DENIED, tm.OUTCOME_BUDGET,
                 tm.OUTCOME_CANCELLED, tm.OUTCOME_ABORTED, tm.OUTCOME_STOPPED):
        record = _record(outcome=good)
        assert record.failed is False, good
        assert record.to_dict()["failed"] is False, good
        assert record.to_dict()["outcome"] == good


def test_summary_counts_every_outcome_and_they_sum_to_turns(tmp_path):
    # The identity is what makes the block auditable: a reader can check the
    # outcomes account for every turn without trusting the harness's arithmetic.
    sink = tmp_path / "usage.jsonl"
    for i, outcome in enumerate(
        (tm.OUTCOME_OK, tm.OUTCOME_BUDGET, tm.OUTCOME_OK,
         tm.OUTCOME_ERROR, tm.OUTCOME_DENIED, tm.OUTCOME_CANCELLED),
        start=1,
    ):
        tm.record_turn(sink, _record(turn=i, outcome=outcome), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["outcomes"] == {"ok": 2, "denied": 1, "budget": 1, "cancelled": 1, "error": 1}
    assert sum(s["outcomes"].values()) == s["turns"] == 6
    # Only the genuine failure lands in the reliability column.
    assert s["turns_failed"] == 1


def test_a_budget_stop_is_not_reported_as_a_failed_turn(tmp_path):
    """The regression this field exists for.

    A sweep run under `--max-cost` used to report every capped instance as a
    failed turn — indistinguishable from a crash, and read as harness
    unreliability by exactly the benchmark aggregation the milestone is for.
    """
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1, outcome=tm.OUTCOME_BUDGET), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["turns_failed"] == 0
    assert s["outcomes"] == {"budget": 1}
    assert "1 budget" in tm.render_pr_block(s)


def test_a_record_written_before_outcome_existed_still_counts(tmp_path):
    """Same schema version, one fewer key: `outcome` was added additively, so a
    sink written by the previous build has `failed` and no `outcome`.

    Falling back to `failed` matters more than it looks — reconstructing `ok`
    for every old record would report zero failures on a run that had them, which
    is a silent wrong answer rather than a missing one."""
    sink = tmp_path / "usage.jsonl"
    old_ok = {k: v for k, v in _record(turn=1).to_dict().items() if k != "outcome"}
    old_failed = {**old_ok, "turn": 2, "failed": True}
    for payload in (old_ok, old_failed):
        tm.record_turn(sink, payload, env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["turns"] == 2
    assert s["turns_failed"] == 1
    assert s["outcomes"] == {"ok": 1, "error": 1}


def test_show_names_the_outcome_mix():
    s = tm.derive_session([_record(outcome=tm.OUTCOME_CANCELLED).to_dict()])
    assert "cancelled=1" in tm.format_show(s, "read from session.json")


def test_summary_totals_equal_the_sum_of_the_records(tmp_path):
    # Invariant 6: derived, never independently accumulated.
    records = tm.read_records(_three_turns(tmp_path))
    s = tm.derive_session(records)
    assert s["tokens"]["input"] == 600
    assert s["tokens"]["output"] == 60
    assert s["tokens"]["total"] == 660
    assert s["turns"] == 3
    assert s["turns_failed"] == 1
    assert s["tools"] == {"read_file": 3, "execute": 3}
    assert s["tool_errors"] == 1
    assert s["retries"] == 1
    assert s["context_trims"] == 1
    assert s["models"] == {"ollama:gemma4": 3}


def test_wall_clock_decomposes_with_a_bounded_residual(tmp_path):
    """Invariant 4a. Every component is measured at its own seam; only the
    residual is inferred, and it is stored rather than left implicit."""
    records = tm.read_records(_three_turns(tmp_path))
    s = tm.derive_session(records, duration_ms=6500)
    t = s["time"]
    components = sum(v for k, v in t.items() if k != "residual_ms")
    assert components <= s["duration_ms"]
    assert t["residual_ms"] == s["duration_ms"] - components
    assert t["residual_ms"] >= 0
    assert t["model_ms"] == 3300 and t["tool_ms"] == 1100 and t["retry_sleep_ms"] == 100


def test_residual_accounts_for_hitl_wait(tmp_path):
    # Human think time is wall clock inside the turn that is neither the harness's
    # nor the model's. Omitting the term is how 4a would fail the first time
    # anyone ran with HITL on — the shape of invariant that gets weakened, not fixed.
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(duration_ms=5000, model_ms=1000, tool_ms=500,
                                 hitl_wait_ms=3000), env={})
    t = tm.derive_session(tm.read_records(sink))["time"]
    assert t["hitl_wait_ms"] == 3000
    assert t["residual_ms"] == 500


def test_paced_sleep_is_a_separate_term_from_model_time(tmp_path):
    # Invariant 4b: without this a throttled free-tier run reports ~60s "model
    # latency" that is a property of the plan, not the model.
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(duration_ms=61000, model_ms=1000, paced_sleep_ms=60000), env={})
    t = tm.derive_session(tm.read_records(sink))["time"]
    assert t["paced_sleep_ms"] == 60000
    assert t["model_ms"] == 1000


def test_cost_is_null_when_no_record_priced_anything(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["cost_usd"] is None
    assert s["cost_provenance"] is None
    assert s["energy_wh"] is None


def test_cost_sums_only_the_priced_records(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1, cost_usd=0.25, cost_provenance="official"), env={})
    tm.record_turn(sink, _record(turn=2, cost_usd=None), env={})
    tm.record_turn(sink, _record(turn=3, cost_usd=0.5, cost_provenance="estimate"), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["cost_usd"] == pytest.approx(0.75)
    assert s["cost_provenance"] == "estimate"  # end-of-run posture, per the last priced turn


def test_zero_turn_run_still_derives_a_valid_summary():
    """Invariant 9: "opened a session and typed /exit" must not yield a file the
    reader has to special-case."""
    s = tm.derive_session([], run_id="run-empty", duration_ms=1200)
    assert s["turns"] == 0 and s["turns_failed"] == 0
    assert s["tokens"]["total"] == 0
    assert s["models"] == {} and s["tools"] == {}
    assert s["cost_usd"] is None
    assert s["time"]["residual_ms"] == 1200
    assert tm.render_pr_block(s)  # renders rather than crashing


def test_model_mix_is_a_map_so_a_mid_session_switch_survives(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1, provider="ollama", model="gemma4"), env={})
    tm.record_turn(sink, _record(turn=2, provider="anthropic", model="claude-x"), env={})
    tm.record_turn(sink, _record(turn=3, provider="anthropic", model="claude-x"), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["models"] == {"ollama:gemma4": 1, "anthropic:claude-x": 2}


def test_interrupts_are_summed_from_the_records(tmp_path):
    # The summary's interrupt count has to be derivable from the records, or it is
    # a second accumulator and invariant 6 stops holding.
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1, interrupts=2), env={})
    tm.record_turn(sink, _record(turn=2, interrupts=1), env={})
    assert tm.derive_session(tm.read_records(sink))["interrupts"] == 3


# --- summary file round-trip --------------------------------------------------


def test_summary_write_read_round_trip(tmp_path):
    summary = tm.derive_session(tm.read_records(_three_turns(tmp_path)), duration_ms=6500)
    path = tm.session_path(tmp_path)
    tm.write_session(path, summary, env={})
    assert tm.read_session(path) == summary


def test_read_session_returns_none_rather_than_raising(tmp_path):
    assert tm.read_session(tmp_path / "absent.json") is None
    bad = tmp_path / "session.json"
    bad.write_text("{not json", encoding="utf-8")
    assert tm.read_session(bad) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    assert tm.read_session(wrong) is None


# --- renderers ----------------------------------------------------------------


def test_pr_block_is_empty_without_a_summary():
    # So the caller's fallback is "append nothing", not a branch — invariant 13.
    assert tm.render_pr_block(None) == ""
    assert tm.render_pr_block({}) == ""


def test_pr_block_carries_the_marker_and_the_aggregates(tmp_path):
    s = tm.derive_session(tm.read_records(_three_turns(tmp_path)), duration_ms=6500)
    block = tm.render_pr_block(s)
    assert tm.PR_BLOCK_MARKER in block
    assert "### Run telemetry" in block
    # Named, not just counted: "1 error" and "1 budget" are different facts about
    # a run, and the PR body is where a reviewer forms the first impression.
    assert "3 (1 error)" in block
    assert "660" in block
    assert "ollama:gemma4" in block


def test_pr_block_says_not_priced_rather_than_zero(tmp_path):
    s = tm.derive_session(tm.read_records(_three_turns(tmp_path)))
    assert "not priced" in tm.render_pr_block(s)
    assert "$0.0000" not in tm.render_pr_block(s)


def test_duration_formatting():
    assert tm.format_duration(0) == "0s"
    assert tm.format_duration(52_000) == "52s"
    assert tm.format_duration(412_330) == "6m 52s"
    assert tm.format_duration(3_843_000) == "1h 04m 03s"


def test_cost_formatting_marks_an_estimate():
    assert tm.format_cost(None) == "not priced (no cost tracker for this model)"
    assert tm.format_cost(0.0, "official") == "$0.0000"
    assert tm.format_cost(1.5, "estimate") == "~$1.5000 (estimated)"


def test_show_names_its_source(tmp_path):
    s = tm.derive_session(tm.read_records(_three_turns(tmp_path)))
    text = tm.format_show(s, "read from session.json")
    assert "read from session.json" in text
    assert "run_id" in text and "tokens" in text


# --- import weight ------------------------------------------------------------


def test_telemetry_imports_no_sibling_but_scrub():
    """Invariant 22's checkable half: the module itself is stdlib-weight.

    Not "the process is keyless" — ``harness/__init__.py`` imports ``cli``
    unconditionally, so every subcommand already pays langchain's import cost
    (M5 §0.1 F6, deferred). What holds is that this route adds nothing on top.
    """
    script = textwrap.dedent(
        f"""
        import importlib.util, sys, types
        from pathlib import Path
        harness_dir = Path(r{str(_HARNESS)!r})
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(harness_dir)]
        sys.modules["harness"] = pkg
        for name in ("scrub", "telemetry"):
            spec = importlib.util.spec_from_file_location(f"harness.{{name}}", harness_dir / f"{{name}}.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[f"harness.{{name}}"] = mod
            spec.loader.exec_module(mod)
        print("\\n".join(sorted(m for m in sys.modules if m.startswith("harness."))))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    loaded = set(out.stdout.split())
    assert loaded == {"harness.scrub", "harness.telemetry"}, (
        f"harness.telemetry pulled in siblings beyond harness.scrub: {loaded}"
    )


# --- Milestone 8 B1: the `stopped` outcome and `stop_reason` -------------------
#
# The distinction these pin: a bound the operator set firing is NOT the harness
# breaking, and *which* bound fired is the actionable half. Before M8,
# `GraphRecursionError` fell through to `error`, so a truncated instance and a
# crashed one were the same row in a sweep (`milestone8.md` §3).


def test_a_stopped_turn_round_trips_and_is_not_a_failure(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(
        sink, _record(turn=1, outcome=tm.OUTCOME_STOPPED, stop_reason="steps"), env={}
    )
    (row,) = tm.read_records(sink)
    assert row["outcome"] == tm.OUTCOME_STOPPED
    assert row["stop_reason"] == "steps"
    assert row["failed"] is False
    s = tm.derive_session([row])
    assert s["turns_failed"] == 0
    assert s["outcomes"] == {"stopped": 1}


def test_stop_reason_is_null_on_a_turn_no_bound_stopped(tmp_path):
    # Present-and-null, never absent: a reader must be able to tell "no bound
    # fired" from "the field is gone", the same convention M6 used for the join
    # keys on the headless payload.
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1), env={})
    (row,) = tm.read_records(sink)
    assert "stop_reason" in row
    assert row["stop_reason"] is None


def test_summary_counts_which_bound_stopped_each_turn(tmp_path):
    sink = tmp_path / "usage.jsonl"
    for i, reason in enumerate(("steps", "seconds", "steps"), start=1):
        tm.record_turn(
            sink,
            _record(turn=i, outcome=tm.OUTCOME_STOPPED, stop_reason=reason),
            env={},
        )
    tm.record_turn(sink, _record(turn=4, outcome=tm.OUTCOME_OK), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["stop_reasons"] == {"steps": 2, "seconds": 1}
    # Derived from the same records as `outcomes`, so the two agree by
    # construction rather than by a second accumulator (invariant 6).
    assert sum(s["stop_reasons"].values()) == s["outcomes"][tm.OUTCOME_STOPPED]


def test_stop_reasons_is_empty_when_nothing_was_stopped(tmp_path):
    sink = tmp_path / "usage.jsonl"
    tm.record_turn(sink, _record(turn=1), env={})
    s = tm.derive_session(tm.read_records(sink))
    assert s["stop_reasons"] == {}
    # And `show` says nothing about bounds on a run where none fired.
    assert "[" not in "\n".join(
        line for line in tm.format_show(s, "records").splitlines() if "outcomes" in line
    )


def test_show_names_the_bound_that_fired(tmp_path):
    s = tm.derive_session(
        [_record(turn=1, outcome=tm.OUTCOME_STOPPED, stop_reason="seconds").to_dict()]
    )
    line = next(l for l in tm.format_show(s, "records").splitlines() if "outcomes" in l)
    assert "stopped=1" in line and "seconds=1" in line


def test_a_stopped_run_is_named_in_the_pr_block(tmp_path):
    # The PR body already names every non-`ok` outcome, so `stopped` rides in for
    # free -- pinned because a reviewer reading "(1 stopped)" and "(1 failed)"
    # takes different actions, and the generic rendering is what keeps that true
    # without a per-outcome branch.
    s = tm.derive_session(
        [_record(turn=1, outcome=tm.OUTCOME_STOPPED, stop_reason="turns").to_dict()]
    )
    assert "1 stopped" in tm.render_pr_block(s)


def test_an_unknown_outcome_still_degrades_to_error():
    # Unchanged by M8, and re-pinned because the milestone added a member to the
    # enum: an OLD reader meeting a NEWER record must fail safe, which is why
    # `schema` did not have to bump for either addition.
    assert _record(outcome="teleported").to_dict()["outcome"] == tm.OUTCOME_ERROR


def test_stopped_is_in_the_declared_outcome_enum():
    # `_outcome_counts` orders by OUTCOMES, and `to_dict` degrades anything
    # outside it -- so a constant that exists but was never added to the tuple
    # would write every stopped turn to disk as `error`, which is precisely the
    # defect this milestone removes.
    assert tm.OUTCOME_STOPPED in tm.OUTCOMES
