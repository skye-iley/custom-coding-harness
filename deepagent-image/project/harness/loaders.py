"""Optional-file loaders: AGENTS.md text, .mcp.json tools, hooks.json.

All read from paths the caller supplies (CWD-relative in practice) and degrade
to empty when the file is missing, so an unconfigured harness still runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


def _read_optional_text(path: Path) -> str:
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _read_optional_json(path: Path) -> dict:
    text = _read_optional_text(path)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}")


def _normalize_mcp_connections(servers: dict[str, dict]) -> dict[str, dict]:
    """Add the transport field langchain_mcp_adapters needs, inferring it from
    the Claude/Cursor-style .mcp.json shape (command -> stdio, url -> http)."""
    connections: dict[str, dict] = {}
    for name, cfg in servers.items():
        cfg = dict(cfg)
        if "transport" not in cfg:
            if "command" in cfg:
                cfg["transport"] = "stdio"
            elif "url" in cfg:
                cfg["transport"] = "streamable_http"
            else:
                raise SystemExit(
                    f"MCP server '{name}' in .mcp.json needs a 'command' or 'url'."
                )
        connections[name] = cfg
    return connections


def load_mcp_tools(config_path: Path) -> list:
    """Load tools from the MCP servers declared in .mcp.json (empty if none)."""
    servers = _read_optional_json(config_path).get("mcpServers") or {}
    if not servers:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(_normalize_mcp_connections(servers))
    return asyncio.run(client.get_tools())


def load_hooks(config_path: Path) -> dict[str, list[str]]:
    """Read hooks.json into an {event: [commands...]} map (empty if none)."""
    hooks = _read_optional_json(config_path).get("hooks") or []
    by_event: dict[str, list[str]] = {}
    for hook in hooks:
        commands = hook.get("command", [])
        if isinstance(commands, str):
            commands = [commands]
        for event in hook.get("events", []):
            by_event.setdefault(event, []).extend(commands)
    return by_event
