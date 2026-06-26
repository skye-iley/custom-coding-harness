"""Tests for harness/cli.py — arg parsing, env coercion, exit + budget wiring.

cli.py pulls the whole runtime stack (dotenv, langgraph, deepagents via
harness.agent), so the module is gated behind importorskip and runs only in the
runtime/test image. The focus is the deterministic glue: argument defaults, env
float/int coercion, the Python-side exit-command match, and the
"build a cost tracker only when there's something to track" contract that keeps
the harness byte-for-byte MVP when nothing needs tracking (§2.5).
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")  # image-only
pytest.importorskip("dotenv")
pytest.importorskip("deepagents")

from _bootstrap import _load  # noqa: E402

cli = _load("harness.cli")
cost = _load("harness.cost")
providers = _load("harness.providers")


# --- parse_args ------------------------------------------------------------

def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["main.py", *args])


def test_parse_args_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_WORKSPACE", raising=False)
    monkeypatch.delenv("DEEPAGENTS_THREAD_ID", raising=False)
    monkeypatch.delenv("DEEPAGENTS_MAX_COST", raising=False)
    monkeypatch.delenv("DEEPAGENTS_MAX_TOKENS", raising=False)
    _argv(monkeypatch)
    ns = cli.parse_args()
    assert ns.task == []
    assert ns.model is None
    assert ns.thread_id == "default"
    assert ns.workspace.endswith("workspace")
    assert ns.max_cost is None and ns.max_tokens is None
    assert ns.stream is False


def test_parse_args_collects_task_words(monkeypatch):
    _argv(monkeypatch, "fix", "the", "bug")
    assert cli.parse_args().task == ["fix", "the", "bug"]


def test_parse_args_model_and_stream_flags(monkeypatch):
    _argv(monkeypatch, "--model", "openai:gpt", "--stream")
    ns = cli.parse_args()
    assert ns.model == "openai:gpt" and ns.stream is True


def test_parse_args_budget_from_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_MAX_COST", "2.5")
    monkeypatch.setenv("DEEPAGENTS_MAX_TOKENS", "1000")
    _argv(monkeypatch)
    ns = cli.parse_args()
    assert ns.max_cost == 2.5 and ns.max_tokens == 1000


def test_parse_args_cli_budget_overrides_env(monkeypatch):
    monkeypatch.setenv("DEEPAGENTS_MAX_COST", "2.5")
    _argv(monkeypatch, "--max-cost", "9.0")
    assert cli.parse_args().max_cost == 9.0


# --- _env_float / _env_int -------------------------------------------------

def test_env_float_present(monkeypatch):
    monkeypatch.setenv("X", "1.5")
    assert cli._env_float("X") == 1.5


def test_env_float_absent_or_empty(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert cli._env_float("X") is None
    monkeypatch.setenv("X", "")
    assert cli._env_float("X") is None


def test_env_int_present_and_absent(monkeypatch):
    monkeypatch.setenv("N", "42")
    assert cli._env_int("N") == 42
    monkeypatch.delenv("N", raising=False)
    assert cli._env_int("N") is None


# --- _is_exit_command ------------------------------------------------------

@pytest.mark.parametrize("line", ["/exit", "/quit", " /EXIT ", "/Quit\n"])
def test_is_exit_command_true(line):
    assert cli._is_exit_command(line)


@pytest.mark.parametrize("line", ["exit", "/exitnow", "hello", "", "  "])
def test_is_exit_command_false(line):
    assert not cli._is_exit_command(line)


# --- build_cost_tracker — the null=MVP contract ----------------------------

def test_tracker_none_when_nothing_to_track(monkeypatch):
    # Unknown model -> no provider -> Free pricing, no energy, no budget -> None,
    # so main() appends no middleware (byte-for-byte MVP, §2.5).
    monkeypatch.setattr(providers, "PROVIDERS", [])
    assert cli.build_cost_tracker("unknown:x", None, None) is None


def test_tracker_built_when_budget_set(monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", [])
    tracker = cli.build_cost_tracker("unknown:x", 5.0, None)
    assert isinstance(tracker, cost.CostTrackerMiddleware)


def test_tracker_built_for_priced_provider(tmp_path, monkeypatch):
    pdir = tmp_path / "acme"
    (pdir / "models").mkdir(parents=True)
    (pdir / "provider.toml").write_text(
        'api_key_env = "ACME_API_KEY"\nrequires_key = false\npriority = 1\n'
        'pricing = "rate_table"\n',
        encoding="utf-8",
    )
    (pdir / "models" / "m1.toml").write_text(
        'name = "m1"\n[pricing]\ninput = 1.0\noutput = 2.0\n', encoding="utf-8"
    )
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    # Non-Free pricing alone is enough to build the tracker, with no budget set.
    assert cli.build_cost_tracker("acme:m1", None, None) is not None


def test_tracker_built_for_energy_only_model(tmp_path, monkeypatch):
    pdir = tmp_path / "local"
    (pdir / "models").mkdir(parents=True)
    (pdir / "provider.toml").write_text(
        'api_key_env = "LOCAL_API_KEY"\nrequires_key = false\npriority = 1\n',
        encoding="utf-8",  # default pricing = free
    )
    (pdir / "models" / "m1.toml").write_text(
        'name = "m1"\n[energy]\nper_input_token = 0.0002\n', encoding="utf-8"
    )
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    # Free pricing but an energy estimate -> still tracked.
    assert cli.build_cost_tracker("local:m1", None, None) is not None
