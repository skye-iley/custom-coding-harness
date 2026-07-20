"""Tests for harness/audit.py (Milestone 3 slice S7, interrupt audit trail).

Stdlib-only; writes go to pytest tmp_path (suite convention). Verifies the
scrub, the record shape, that ``context`` is never persisted, and the append/read
round-trip.
"""

from __future__ import annotations

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
