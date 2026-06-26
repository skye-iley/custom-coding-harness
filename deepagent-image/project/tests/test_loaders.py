"""Tests for harness/loaders.py — the optional-file IO layer.

Covers the pure, dependency-light paths: reading optional text/JSON, normalizing
.mcp.json server entries, and folding hooks.json into an {event: [commands]} map.
`load_mcp_tools`' live path (it spins up MultiServerMCPClient over the network) is
out of scope here — only its empty-config short-circuit is exercised.

stdlib-only (loaders.py imports asyncio/json at module top, the MCP adapter
lazily), so this runs on a bare interpreter via the _bootstrap loader. All writes
go to pytest's tmp_path, which is auto-removed — nothing reaches the repo.
"""

from __future__ import annotations

import pytest

from _bootstrap import _load

loaders = _load("harness.loaders")


# --- _read_optional_text ---------------------------------------------------

def test_read_optional_text_missing_returns_empty(tmp_path):
    assert loaders._read_optional_text(tmp_path / "nope.txt") == ""


def test_read_optional_text_strips_content(tmp_path):
    f = tmp_path / "AGENTS.md"
    f.write_text("  hello \n\n", encoding="utf-8")
    assert loaders._read_optional_text(f) == "hello"


def test_read_optional_text_on_directory_returns_empty(tmp_path):
    # A dir exists but is not a file -> empty, not a crash.
    assert loaders._read_optional_text(tmp_path) == ""


# --- _read_optional_json ---------------------------------------------------

def test_read_optional_json_missing_is_empty_dict(tmp_path):
    assert loaders._read_optional_json(tmp_path / "x.json") == {}


def test_read_optional_json_empty_file_is_empty_dict(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("   ", encoding="utf-8")
    assert loaders._read_optional_json(f) == {}


def test_read_optional_json_parses_object(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": 1, "b": [2, 3]}', encoding="utf-8")
    assert loaders._read_optional_json(f) == {"a": 1, "b": [2, 3]}


def test_read_optional_json_invalid_raises_systemexit(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        loaders._read_optional_json(f)
    assert str(f) in str(exc.value)  # message names the offending file


# --- _normalize_mcp_connections -------------------------------------------

def test_normalize_infers_stdio_from_command():
    out = loaders._normalize_mcp_connections({"srv": {"command": "run-me"}})
    assert out["srv"]["transport"] == "stdio"
    assert out["srv"]["command"] == "run-me"


def test_normalize_infers_http_from_url():
    out = loaders._normalize_mcp_connections({"srv": {"url": "http://x"}})
    assert out["srv"]["transport"] == "streamable_http"


def test_normalize_keeps_explicit_transport():
    out = loaders._normalize_mcp_connections(
        {"srv": {"command": "x", "transport": "sse"}}
    )
    assert out["srv"]["transport"] == "sse"


def test_normalize_does_not_mutate_input():
    original = {"srv": {"command": "x"}}
    loaders._normalize_mcp_connections(original)
    assert "transport" not in original["srv"]  # works on a copy


def test_normalize_without_command_or_url_raises():
    with pytest.raises(SystemExit) as exc:
        loaders._normalize_mcp_connections({"srv": {"env": {}}})
    assert "srv" in str(exc.value)


# --- load_hooks ------------------------------------------------------------

def test_load_hooks_missing_file_is_empty_map(tmp_path):
    assert loaders.load_hooks(tmp_path / "hooks.json") == {}


def test_load_hooks_string_command_becomes_list(tmp_path):
    f = tmp_path / "hooks.json"
    f.write_text(
        '{"hooks": [{"events": ["session.start"], "command": "echo hi"}]}',
        encoding="utf-8",
    )
    assert loaders.load_hooks(f) == {"session.start": ["echo hi"]}


def test_load_hooks_one_hook_fans_out_to_each_event(tmp_path):
    f = tmp_path / "hooks.json"
    f.write_text(
        '{"hooks": [{"events": ["tool.start", "tool.end"], "command": ["a", "b"]}]}',
        encoding="utf-8",
    )
    assert loaders.load_hooks(f) == {"tool.start": ["a", "b"], "tool.end": ["a", "b"]}


def test_load_hooks_multiple_hooks_extend_same_event(tmp_path):
    f = tmp_path / "hooks.json"
    f.write_text(
        '{"hooks": ['
        '  {"events": ["model.start"], "command": "first"},'
        '  {"events": ["model.start"], "command": "second"}'
        ']}',
        encoding="utf-8",
    )
    assert loaders.load_hooks(f) == {"model.start": ["first", "second"]}


def test_load_hooks_no_hooks_key_is_empty(tmp_path):
    f = tmp_path / "hooks.json"
    f.write_text('{"other": 1}', encoding="utf-8")
    assert loaders.load_hooks(f) == {}
