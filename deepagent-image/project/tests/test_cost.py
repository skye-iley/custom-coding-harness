"""Unit tests for harness/cost.py and the pricing wiring in harness/providers.py.

These cover the pure math (no langchain / no provider network), so they run on a
bare interpreter. cost.py's AgentMiddleware import falls back to `object` when
langchain is absent, so even CostTrackerMiddleware is constructible here.

Run inside the image with pytest (`python3 -m pytest tests/`), or standalone on
any box with `python3 tests/test_cost.py` (a built-in runner at the bottom calls
every test_* with no extra dependency).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# --- bootstrap: import harness.cost / harness.providers WITHOUT triggering ---
# harness/__init__.py (which pulls cli -> dotenv/langgraph/deepagents, none of
# which are installed on a plain test host). We register a bare `harness`
# package, then load the two modules we need by file path.
_HARNESS = Path(__file__).resolve().parent.parent / "harness"


def _load(modname: str) -> types.ModuleType:
    if "harness" not in sys.modules:
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(_HARNESS)]  # mark as a package
        sys.modules["harness"] = pkg
    spec = importlib.util.spec_from_file_location(modname, _HARNESS / f"{modname.split('.')[-1]}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


cost = _load("harness.cost")


# --- helpers -----------------------------------------------------------------

def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    u = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    details = {}
    if cache_read:
        details["cache_read"] = cache_read
    if cache_creation:
        details["cache_creation"] = cache_creation
    if details:
        u["input_token_details"] = details
    return u


class FakeMsg:
    def __init__(self, usage_metadata=None, response_metadata=None):
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata


# --- token split -------------------------------------------------------------

def test_split_tokens_subtracts_cache_from_input():
    fresh, out, cr, cw = cost._split_tokens(usage(100, 50, cache_read=30, cache_creation=10))
    assert (fresh, out, cr, cw) == (60, 50, 30, 10)


def test_split_tokens_no_details():
    assert cost._split_tokens(usage(100, 50)) == (100, 50, 0, 0)


def test_split_tokens_never_negative():
    # cache buckets larger than reported input shouldn't drive fresh below 0.
    assert cost._split_tokens(usage(20, 0, cache_read=30))[0] == 0


# --- RateTable ---------------------------------------------------------------

def test_rate_table_basic_input_output():
    rt = cost.RateTable({"m": cost.ModelRates(input=1.0, output=2.0)})  # $/Mtok
    # 1e6 input -> $1, 1e6 output -> $2
    assert rt.cost(usage(1_000_000, 1_000_000), "m") == 3.0


def test_rate_table_prices_cache_buckets_separately():
    rt = cost.RateTable({"m": cost.ModelRates(input=10.0, output=0.0, cache_read=1.0, cache_write=5.0)})
    # 1e6 total input, of which 600k cache_read + 100k cache_write -> 300k fresh
    c = rt.cost(usage(1_000_000, 0, cache_read=600_000, cache_creation=100_000), "m")
    expected = (300_000 * 10 + 600_000 * 1 + 100_000 * 5) / 1_000_000
    assert abs(c - expected) < 1e-9


def test_rate_table_falls_back_to_input_rate_when_cache_rate_missing():
    rt = cost.RateTable({"m": cost.ModelRates(input=10.0, output=0.0)})
    # no cache_read rate -> cached tokens billed at input rate (never dropped)
    c = rt.cost(usage(1_000_000, 0, cache_read=500_000), "m")
    assert abs(c - 10.0) < 1e-9  # all 1e6 input tokens at $10/Mtok


def test_rate_table_none_when_model_unpriced():
    rt = cost.RateTable({})
    assert rt.cost(usage(100, 100), "nope") is None


def test_rate_table_none_when_rates_empty():
    rt = cost.RateTable({"m": cost.ModelRates()})  # no input/output
    assert rt.cost(usage(100, 100), "m") is None


# --- Free / ReportedCost -----------------------------------------------------

def test_free_is_zero():
    assert cost.Free().cost(usage(100, 100), "m") == 0.0


def test_reported_cost_from_usage_top_level():
    assert cost.ReportedCost().cost({"cost": 0.42}, "m") == 0.42


def test_reported_cost_from_response_metadata():
    assert cost.ReportedCost().cost(usage(1, 1), "m", {"cost": 0.5}) == 0.5


def test_reported_cost_from_nested_usage():
    assert cost.ReportedCost().cost(usage(1, 1), "m", {"usage": {"cost": 0.25}}) == 0.25


def test_reported_cost_none_when_absent():
    assert cost.ReportedCost().cost(usage(1, 1), "m", {}) is None


# --- energy ------------------------------------------------------------------

def test_energy_blended_per_token():
    r = cost.ModelRates(energy_per_token=0.001)  # Wh/token
    assert cost.estimate_energy_wh(usage(100, 50), r) == 150 * 0.001


def test_energy_split_input_output_wins():
    r = cost.ModelRates(energy_per_token=99, energy_per_input_token=0.001, energy_per_output_token=0.002)
    assert cost.estimate_energy_wh(usage(100, 50), r) == 100 * 0.001 + 50 * 0.002


def test_energy_none_without_data():
    assert cost.estimate_energy_wh(usage(100, 50), cost.ModelRates()) is None
    assert cost.estimate_energy_wh(usage(100, 50), None) is None


def test_measure_local_energy_not_implemented():
    try:
        cost.measure_local_energy_wh()
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError")


# --- pricing_from_strategy / rates_from_toml ---------------------------------

def test_pricing_from_strategy_mapping():
    assert isinstance(cost.pricing_from_strategy("free", {}), cost.Free)
    assert isinstance(cost.pricing_from_strategy("reported", {}), cost.ReportedCost)
    assert isinstance(cost.pricing_from_strategy("rate_table", {}), cost.RateTable)
    assert isinstance(cost.pricing_from_strategy(None, {}), cost.Free)
    assert isinstance(cost.pricing_from_strategy("bogus", {}), cost.Free)


def test_rates_from_toml_parses_both_tables():
    r = cost.rates_from_toml(
        {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.25, "priced_as_of": "2026-06-23"},
        {"per_input_token": 0.001, "per_output_token": 0.002, "source": "nvidia_smi"},
    )
    assert r.input == 1.0 and r.output == 2.0 and r.cache_read == 0.1 and r.cache_write == 1.25
    assert r.priced_as_of == "2026-06-23"
    assert r.energy_per_input_token == 0.001 and r.energy_source == "nvidia_smi"
    assert r.has_price and r.has_energy


def test_rates_from_toml_top_level_is_official():
    r = cost.rates_from_toml({"input": 1.0, "output": 2.0}, None)
    assert r.has_price and r.pricing_source == "official"


def test_rates_from_toml_nested_estimate_is_tagged():
    r = cost.rates_from_toml(
        {"estimate": {"input": 0.9, "output": 4.5, "priced_as_of": "2026-05-01"}}, None
    )
    assert r.input == 0.9 and r.output == 4.5 and r.priced_as_of == "2026-05-01"
    assert r.has_price and r.pricing_source == "estimate"


def test_rates_from_toml_official_wins_over_estimate():
    r = cost.rates_from_toml(
        {"input": 1.0, "output": 2.0, "estimate": {"input": 99.0, "output": 99.0}}, None
    )
    assert r.input == 1.0 and r.output == 2.0 and r.pricing_source == "official"


def test_rates_from_toml_energy_only_has_no_pricing_source():
    r = cost.rates_from_toml(None, {"per_token": 0.001})
    assert not r.has_price and r.pricing_source is None and r.has_energy


# --- UsageAccumulator --------------------------------------------------------

def test_accumulator_priced_call():
    acc = cost.UsageAccumulator()
    rt = cost.RateTable({"m": cost.ModelRates(input=1.0, output=2.0)})
    acc.add(usage(1_000_000, 1_000_000), rt, "m")
    assert acc.input == 1_000_000 and acc.output == 1_000_000
    assert acc.cost == 3.0 and acc.unpriced_calls == 0


def test_accumulator_unpriced_call_is_loud_not_zero():
    acc = cost.UsageAccumulator()
    acc.add(usage(100, 100), cost.RateTable({}), "m")
    assert acc.unpriced_calls == 1
    assert acc.cost == 0.0  # floor, but flagged via unpriced_calls


def test_accumulator_estimate_fallback():
    acc = cost.UsageAccumulator()
    acc.add(usage(1_000_000, 0), cost.RateTable({}), "m", estimate_per_mtok=2.0)
    assert acc.estimated_calls == 1 and acc.unpriced_calls == 0
    assert abs(acc.cost - 2.0) < 1e-9


def test_accumulator_registry_estimate_rate_tags_call():
    # A real rate from a [pricing.estimate] table prices the call but is flagged
    # estimated (so the usage line shows ~/(est)), not treated as a confirmed cost.
    r = cost.ModelRates(input=1.0, output=2.0, pricing_source="estimate")
    acc = cost.UsageAccumulator()
    acc.add(usage(1_000_000, 1_000_000), cost.RateTable({"m": r}), "m", rates=r)
    assert abs(acc.cost - 3.0) < 1e-9
    assert acc.estimated_calls == 1 and acc.unpriced_calls == 0


def test_accumulator_official_rate_not_tagged():
    r = cost.ModelRates(input=1.0, output=2.0, pricing_source="official")
    acc = cost.UsageAccumulator()
    acc.add(usage(1_000_000, 0), cost.RateTable({"m": r}), "m", rates=r)
    assert acc.cost == 1.0 and acc.estimated_calls == 0


def test_accumulator_reported_cost_not_tagged_even_with_estimate_rates():
    # ReportedCost returns the actual in-band bill; estimate-sourced reference
    # rates on the same model must not taint it as estimated.
    r = cost.ModelRates(input=1.0, output=2.0, pricing_source="estimate")
    acc = cost.UsageAccumulator()
    acc.add(usage(100, 100), cost.ReportedCost(), "m", rates=r, response_metadata={"cost": 0.5})
    assert abs(acc.cost - 0.5) < 1e-9 and acc.estimated_calls == 0


def test_accumulator_energy_and_cache_split():
    acc = cost.UsageAccumulator()
    r = cost.ModelRates(input=1.0, output=1.0, energy_per_token=0.001)
    acc.add(usage(100, 50, cache_read=20), cost.RateTable({"m": r}), "m", rates=r)
    assert acc.cache_read == 20 and acc.input == 80
    assert acc.energy_wh == 150 * 0.001


# --- format_line -------------------------------------------------------------

def test_format_line_shows_turn_and_session():
    turn = cost.UsageAccumulator(input=10, output=5, cost=0.01)
    session = cost.UsageAccumulator(input=100, output=50, cost=0.1)
    line = cost.format_line(turn, session)
    assert line.startswith("[harness] usage:")
    assert "turn[" in line and "session[" in line
    assert "in=10" in line and "in=100" in line


def test_format_line_electricity_when_rate_set():
    acc = cost.UsageAccumulator(energy_wh=1000.0)  # 1 kWh
    line = cost.format_line(acc, acc, electricity_rate=0.30)
    assert "energy=1000.000Wh" in line and "elec=$0.3000" in line


def test_format_line_flags_unpriced():
    acc = cost.UsageAccumulator(input=10, unpriced_calls=2)
    assert "unpriced" in cost.format_line(acc, acc)


def test_format_line_marks_estimated():
    acc = cost.UsageAccumulator(input=10, cost=0.0123, estimated_calls=1)
    line = cost.format_line(acc, acc)
    assert "cost=~$0.0123 (est)" in line


def test_format_session_total_marks_estimated():
    s = cost.UsageAccumulator(input=10, output=5, cost=0.05, estimated_calls=2)
    total = cost.format_session_total(s)
    assert "cost=~$0.0500 (estimated)" in total


# --- CostTrackerMiddleware ---------------------------------------------------

def _mw(**kw):
    rt = kw.pop("pricing", cost.RateTable({"m": cost.ModelRates(input=1.0, output=1.0)}))
    return cost.CostTrackerMiddleware(rt, "m", **kw)


def test_middleware_accumulates_after_model():
    mw = _mw()
    mw.before_agent(None, None)
    state = {"messages": [FakeMsg(usage_metadata=usage(1_000_000, 0))]}
    mw.after_model(state, None)
    assert mw.session.input == 1_000_000 and mw.turn.input == 1_000_000
    assert abs(mw.session.cost - 1.0) < 1e-9


def test_middleware_budget_cost_raises():
    mw = _mw(max_cost=0.5)
    state = {"messages": [FakeMsg(usage_metadata=usage(1_000_000, 0))]}
    try:
        mw.after_model(state, None)
    except cost.BudgetExceeded:
        return
    raise AssertionError("expected BudgetExceeded on cost ceiling")


def test_middleware_budget_tokens_raises():
    mw = _mw(max_tokens=100)
    state = {"messages": [FakeMsg(usage_metadata=usage(1000, 0))]}
    try:
        mw.after_model(state, None)
    except cost.BudgetExceeded:
        return
    raise AssertionError("expected BudgetExceeded on token ceiling")


def test_middleware_ignores_message_without_usage():
    mw = _mw()
    mw.after_model({"messages": [FakeMsg()]}, None)  # no usage_metadata
    assert mw.session.total_tokens == 0


def test_middleware_turn_resets_session_persists():
    mw = _mw()
    state = {"messages": [FakeMsg(usage_metadata=usage(100, 0))]}
    mw.before_agent(None, None)
    mw.after_model(state, None)
    mw.before_agent(None, None)  # next turn
    assert mw.turn.input == 0 and mw.session.input == 100


# --- providers wiring (loads cost types from TOML) ---------------------------

def test_providers_load_pricing_from_registry():
    providers = _load("harness.providers")
    # at least one rate_table provider with a priced model after we fill TOMLs
    priced = [p for p in providers.PROVIDERS if isinstance(p.pricing, cost.RateTable) and p.model_rates]
    assert priced, "expected at least one rate_table provider with model rates"
    p = priced[0]
    rates = next(iter(p.model_rates.values()))
    assert rates.has_price
    # Every priced model carries a provenance tag — never an unmarked price.
    for prov in priced:
        for r in prov.model_rates.values():
            if r.has_price:
                assert r.pricing_source in ("official", "estimate")


# --- standalone runner -------------------------------------------------------

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
