"""Tests for the [pricing] table emit in harness/sync_models.py (Milestone 1).

Pure parse/render — no network. Uses the same path-based bootstrap as
test_cost.py to avoid importing harness/__init__ (cli -> dotenv/langgraph).
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
import types
from pathlib import Path

_HARNESS = Path(__file__).resolve().parent.parent / "harness"


def _load(modname: str) -> types.ModuleType:
    if "harness" not in sys.modules:
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(_HARNESS)]
        sys.modules["harness"] = pkg
    spec = importlib.util.spec_from_file_location(modname, _HARNESS / f"{modname.split('.')[-1]}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


cost = _load("harness.cost")
sm = _load("harness.sync_models")


def test_parse_openrouter_converts_per_token_to_per_mtok():
    data = {"data": [{"id": "x/y", "context_length": 1000,
                      "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
    [info] = sm.parse_openrouter(data, as_of="2026-06-23")
    assert info.pricing["input"] == 1.0      # 1e-6 * 1e6
    assert info.pricing["output"] == 2.0
    assert info.pricing["priced_as_of"] == "2026-06-23"
    assert info.extra["context_window"] == 1000


def test_parse_openrouter_no_pricing_is_none():
    [info] = sm.parse_openrouter({"data": [{"id": "x/y"}]})
    assert info.pricing is None


def test_render_emits_pricing_table_that_reloads():
    [info] = sm.parse_openrouter(
        {"data": [{"id": "a/b", "pricing": {"prompt": "0.000003", "completion": "0.000004"}}]},
        as_of="2026-06-23",
    )
    text = sm.render_model_toml(info)
    assert "[pricing]" in text
    parsed = tomllib.loads(text)              # must be valid TOML
    rates = cost.rates_from_toml(parsed.get("pricing"), parsed.get("energy"))
    assert rates.input == 3.0 and rates.output == 4.0 and rates.priced_as_of == "2026-06-23"


def test_render_no_pricing_stays_flat():
    text = sm.render_model_toml(sm.ModelInfo("plain"))
    assert "[pricing]" not in text and 'name = "plain"' in text


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
