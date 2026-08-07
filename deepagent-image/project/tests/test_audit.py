"""Tests for harness/audit.py (Milestone 3 slice S7, interrupt audit trail).

Stdlib-only; writes go to pytest tmp_path (suite convention). Verifies the
scrub, the record shape, that ``context`` is never persisted, and the append/read
round-trip.
"""

from __future__ import annotations

import json

from _bootstrap import _load

# audit imports harness.interrupt; load that first so the bare-harness package is
# registered and the submodule import inside audit resolves.
it = _load("harness.interrupt")
audit = _load("harness.audit")


def _req():
    return it.new_request(
        it.KIND_APPROVE,
        "run this command?",
        context="SECRET DIFF line\n" * 5,
        source=it.SOURCE_DETERMINISTIC,
    )


def test_record_written_and_readback(tmp_path):
    r = _req()
    rec = audit.record_interrupt(tmp_path, r, True, env={})
    assert rec["id"] == r.id
    assert rec["kind"] == it.KIND_APPROVE
    assert rec["source"] == it.SOURCE_DETERMINISTIC
    assert rec["resolved_value"] == "True"
    assert rec["resolved_by"] == "human"
    assert "raised_at" in rec and "resolved_at" in rec

    back = audit.read_records(tmp_path)
    assert back == [rec]


def test_context_is_never_persisted(tmp_path):
    r = _req()
    rec = audit.record_interrupt(tmp_path, r, False, env={})
    assert "context" not in rec  # the large/secret-laden payload is dropped (§S7)


def test_path_is_under_agent_telemetry(tmp_path):
    p = audit.interrupts_path(tmp_path)
    assert p.name == "interrupts.jsonl"
    assert p.parent.name == ".agent_telemetry"


def test_scrub_redacts_env_secret_values():
    env = {"ANTHROPIC_API_KEY": "supersecretvalue123", "PATH": "/usr/bin"}
    out = audit.scrub("key is supersecretvalue123 here", env=env)
    assert "supersecretvalue123" not in out
    assert "REDACTED" in out


def test_scrub_redacts_key_shapes():
    out = audit.scrub("token sk-abcdefghijklmnopqrstuv leaked", env={})
    assert "sk-abcdefghijklmnopqrstuv" not in out
    assert "REDACTED" in out


def test_scrub_leaves_ordinary_text():
    assert audit.scrub("just a normal prompt", env={}) == "just a normal prompt"


def test_prompt_and_value_scrubbed_in_record(tmp_path):
    env = {"MY_TOKEN": "abcdef123456xyz"}
    r = it.new_request(it.KIND_INPUT, "paste creds: abcdef123456xyz")
    rec = audit.record_interrupt(tmp_path, r, "abcdef123456xyz", env=env)
    assert "abcdef123456xyz" not in rec["prompt"]
    assert "abcdef123456xyz" not in rec["resolved_value"]


def test_append_accumulates(tmp_path):
    audit.record_interrupt(tmp_path, _req(), True, env={})
    audit.record_interrupt(tmp_path, _req(), False, env={})
    assert len(audit.read_records(tmp_path)) == 2


def test_meta_is_persisted(tmp_path):
    # Regression: record_interrupt used to silently drop `meta` entirely, so a
    # path-guard denial's path/op/reason never reached the audit log despite the
    # source claiming to carry it (M4 slice D).
    r = it.new_request(
        it.KIND_APPROVE,
        "path-guard denied an out-of-workspace access: ../etc/passwd",
        source=it.SOURCE_SYSTEM,
        meta={"path": "../etc/passwd", "op": "file", "reason": "workspace escape"},
    )
    rec = audit.record_interrupt(tmp_path, r, False, env={}, resolved_by="system")
    assert rec["meta"] == {"path": "../etc/passwd", "op": "file", "reason": "workspace escape"}

    back = audit.read_records(tmp_path)
    assert back == [rec]


def test_meta_string_values_are_scrubbed(tmp_path):
    env = {"MY_TOKEN": "abcdef123456xyz"}
    r = it.new_request(
        it.KIND_APPROVE, "denied", source=it.SOURCE_SYSTEM,
        meta={"path": "abcdef123456xyz", "count": 3},
    )
    rec = audit.record_interrupt(tmp_path, r, False, env=env)
    assert "abcdef123456xyz" not in rec["meta"]["path"]
    assert rec["meta"]["count"] == 3  # non-string values pass through untouched


def test_meta_scrub_reaches_nested_values(tmp_path):
    # meta is a free-form dict, so a producer can nest. A top-level-only scrub
    # would make nesting a silent way around the §10 backstop -- exactly what
    # dropping `context` exists to prevent.
    env = {"MY_TOKEN": "abcdef123456xyz"}
    r = it.new_request(
        it.KIND_APPROVE, "denied", source=it.SOURCE_SYSTEM,
        meta={"outer": {"inner": "abcdef123456xyz"}, "items": ["abcdef123456xyz", 7]},
    )
    rec = audit.record_interrupt(tmp_path, r, False, env=env)
    assert "abcdef123456xyz" not in json.dumps(rec["meta"])
    assert rec["meta"]["items"][1] == 7


# --- two sinks: in-workspace log vs. agent-unreachable denial log (M4 slice D) --


def test_denials_path_is_under_the_state_dir(tmp_path):
    p = audit.denials_path(tmp_path / "state")
    assert p.name == "denials.jsonl"
    assert p.parent == tmp_path / "state"


def test_sink_overrides_the_default_destination(tmp_path):
    sink = tmp_path / "state" / "denials.jsonl"
    rec = audit.record_interrupt(tmp_path, _req(), False, env={}, sink=sink)

    assert sink.is_file()
    assert audit.read_records(tmp_path, sink=sink) == [rec]
    # the default in-workspace log is untouched
    assert not audit.interrupts_path(tmp_path).exists()
    assert audit.read_records(tmp_path) == []
