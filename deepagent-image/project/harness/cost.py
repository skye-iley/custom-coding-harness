"""Cost / token / energy accounting — the math and plumbing only.

Rate and energy *data* live in the on-disk registry (``providers/<p>/models/
<m>.toml`` ``[pricing]`` / ``[energy]`` tables, read by ``providers.py``); this
module only does arithmetic on the numbers that file feeds it. See
``design_doc_milestone1.md`` §2.3.

Three pieces:
  * ``Pricing`` strategies (``Free`` | ``ReportedCost`` | ``RateTable``) with one
    method, ``cost(usage, bare_model, response_metadata) -> float | None``.
    ``None`` means "no price available" — never a silent ``0`` (the §6 caveat).
  * ``UsageAccumulator`` — running token / cost / energy totals.
  * ``CostTrackerMiddleware`` — the gated ``AgentMiddleware`` that feeds the
    accumulator after each model call and prints the ``[harness] usage:`` line.

Import direction (avoids the providers <-> cost cycle, §2.4): ``providers.py``
imports *from here*; this module must never import ``providers``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

# The pricing/energy math is pure stdlib so it can be unit-tested on a bare
# interpreter (no langchain). Only CostTrackerMiddleware needs the real
# AgentMiddleware base; fall back to `object` when langchain is absent so
# `import harness.cost` still works for the math-only tests.
try:  # pragma: no cover - exercised differently in-container vs. test host
    from langchain.agents.middleware.types import AgentMiddleware
except ModuleNotFoundError:  # pragma: no cover
    AgentMiddleware = object  # type: ignore[assignment,misc]

# Registry rates are declared per MILLION tokens (human-friendly for hand-fill);
# energy is declared per token in watt-hours. Keep these conversions in one place.
PER_MTOK = 1_000_000


# --- rate / energy data carried per model -----------------------------------

@dataclass(frozen=True)
class ModelRates:
    """One model's pricing + energy snapshot, parsed from its TOML.

    Prices are USD per million tokens. ``cache_read`` / ``cache_write`` are the
    split (cached-vs-fresh) input prices: we record them as data fields now even
    though the harness does not *enable* caching yet — if the provider reports
    cached tokens we price them, otherwise they stay 0 (§2.1, §5).

    ``pricing_source`` records the *provenance* of the rates above: ``"official"``
    (vendor-published — incl. rates sync-pulled from a provider API) vs.
    ``"estimate"`` (best-effort, hand-filled, not vendor-confirmed). It drives the
    ``~``/``(est)`` marking on the usage line so a shown dollar figure never hides
    that it rests on a guess. ``None`` => the model carries no price at all.

    Energy fields are watt-hours PER TOKEN (optional, best-effort estimates).
    ``energy_per_token`` is a single blended figure; the per-input/per-output
    pair, when set, wins over it. ``energy_source`` names the (not-yet-built)
    local-device measurement method for self-hosted models — see ENERGY_SPEC.md.
    """

    input: float | None = None         # USD / Mtok, fresh (non-cached) input
    output: float | None = None        # USD / Mtok, output
    cache_read: float | None = None     # USD / Mtok, cached-input read
    cache_write: float | None = None    # USD / Mtok, cache-creation write
    priced_as_of: str | None = None     # ISO date; staleness stamp (§2.2)
    pricing_source: str | None = None   # "official" | "estimate" | None (no price)

    energy_per_token: float | None = None         # Wh / token, blended
    energy_per_input_token: float | None = None   # Wh / input token
    energy_per_output_token: float | None = None  # Wh / output token
    energy_source: str | None = None              # local-device method (spec only)

    @property
    def has_price(self) -> bool:
        return self.input is not None or self.output is not None

    @property
    def has_energy(self) -> bool:
        return any(
            v is not None
            for v in (
                self.energy_per_token,
                self.energy_per_input_token,
                self.energy_per_output_token,
            )
        )


def rates_from_toml(pricing: dict | None, energy: dict | None) -> ModelRates:
    """Build ModelRates from a model TOML's ``[pricing]`` / ``[energy]`` tables.

    Both tables are optional and any field may be absent; unknown keys are
    ignored so the registry stays forward-compatible (providers/README.md).

    Provenance split: a top-level ``[pricing]`` table is OFFICIAL (vendor-published,
    including rates sync-pulled from a provider API); a nested ``[pricing.estimate]``
    (tomllib parses it as ``pricing["estimate"]``) is BEST-EFFORT. Official wins when
    both are present; with no official rates we fall back to the estimate and tag it
    so the usage line can mark it — we never present a guess as confirmed (§ pricing
    provenance; providers/README.md).
    """
    pricing = pricing or {}
    energy = energy or {}

    def num(table: dict, key: str) -> float | None:
        val = table.get(key)
        return float(val) if isinstance(val, (int, float)) else None

    estimate = pricing.get("estimate") if isinstance(pricing.get("estimate"), dict) else None
    official_present = pricing.get("input") is not None or pricing.get("output") is not None
    if official_present:
        rate_table, source = pricing, "official"
    elif estimate is not None:
        rate_table, source = estimate, "estimate"
    else:
        rate_table, source = pricing, None  # no price (e.g. an energy-only model)

    priced = rate_table.get("input") is not None or rate_table.get("output") is not None

    return ModelRates(
        input=num(rate_table, "input"),
        output=num(rate_table, "output"),
        cache_read=num(rate_table, "cache_read"),
        cache_write=num(rate_table, "cache_write"),
        priced_as_of=(str(rate_table["priced_as_of"]) if rate_table.get("priced_as_of") else None),
        pricing_source=(source if priced else None),
        energy_per_token=num(energy, "per_token"),
        energy_per_input_token=num(energy, "per_input_token"),
        energy_per_output_token=num(energy, "per_output_token"),
        energy_source=(str(energy["source"]) if energy.get("source") else None),
    )


# --- usage_metadata helpers --------------------------------------------------

def _split_tokens(usage: dict) -> tuple[int, int, int, int]:
    """(fresh_input, output, cache_read, cache_write) from a usage_metadata dict.

    LangChain's ``input_tokens`` is the TOTAL prompt, with cache_read /
    cache_creation as subsets in ``input_token_details``; subtract them so each
    bucket is priced once. Missing details => 0 (provider didn't report caching).
    """
    total_input = int(usage.get("input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    details = usage.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_write = int(details.get("cache_creation") or 0)
    fresh_input = max(total_input - cache_read - cache_write, 0)
    return fresh_input, output, cache_read, cache_write


# --- pricing strategies ------------------------------------------------------

@dataclass(frozen=True)
class Free:
    """Local / self-hosted providers (ollama, lmstudio): no API charge."""

    def cost(self, usage: dict, bare_model: str, response_metadata: dict | None = None) -> float | None:
        return 0.0


@dataclass(frozen=True)
class ReportedCost:
    """Provider returns the dollar cost in-band (OpenRouter).

    LangChain may surface it on ``usage_metadata`` or on ``response_metadata``
    (``cost`` at top level or under ``usage``); probe all three. If none is
    present we return ``None`` (unpriced) rather than guess — the fallback to a
    RateTable for OpenRouter is a registry choice, not silent $0 (§5).
    """

    def cost(self, usage: dict, bare_model: str, response_metadata: dict | None = None) -> float | None:
        for src in (usage, response_metadata or {}):
            if not isinstance(src, dict):
                continue
            if src.get("cost") is not None:
                return float(src["cost"])
            nested = src.get("usage")
            if isinstance(nested, dict) and nested.get("cost") is not None:
                return float(nested["cost"])
        return None


@dataclass(frozen=True)
class RateTable:
    """Native providers (anthropic/openai/google/deepseek): per-model rate snapshot.

    ``cost`` raises nothing and returns ``None`` when the model has no
    ``[pricing]`` table — the loud-but-non-fatal handling lives in the
    accumulator (warn once, then keep running; never silent $0). Per the M1
    addendum a missing rate must not crash the session.
    """

    rates: dict[str, ModelRates] = field(default_factory=dict)

    def cost(self, usage: dict, bare_model: str, response_metadata: dict | None = None) -> float | None:
        r = self.rates.get(bare_model)
        if r is None or not r.has_price:
            return None
        fresh_in, out, cache_read, cache_write = _split_tokens(usage)
        # Fall back to the fresh-input rate for cached buckets when a split rate
        # is absent, so cached tokens are never dropped from the bill silently.
        in_rate = r.input or 0.0
        out_rate = r.output if r.output is not None else 0.0
        read_rate = r.cache_read if r.cache_read is not None else in_rate
        write_rate = r.cache_write if r.cache_write is not None else in_rate
        total = (
            fresh_in * in_rate
            + out * out_rate
            + cache_read * read_rate
            + cache_write * write_rate
        )
        return total / PER_MTOK


# Tagged union for type hints / isinstance.
Pricing = Free | ReportedCost | RateTable

_STRATEGIES = {"free": Free, "reported": ReportedCost, "rate_table": RateTable}


def pricing_from_strategy(name: str | None, rates: dict[str, ModelRates]) -> Pricing:
    """Map a provider.toml ``pricing = "..."`` string to a strategy instance.

    Unknown / omitted => ``Free`` (the safe default: behaves like the MVP).
    """
    cls = _STRATEGIES.get((name or "free").strip().lower(), Free)
    return RateTable(rates) if cls is RateTable else cls()


# --- energy ------------------------------------------------------------------

def estimate_energy_wh(usage: dict, rates: ModelRates | None) -> float | None:
    """Watt-hours for one call from the per-token estimate, or ``None``.

    Energy is independent of the pricing strategy: a Free (local) model can
    still carry an energy estimate. For locally-hosted models a *measured*
    figure (sampling device power across the model-call window) is the intended
    successor to this estimate — specced, not implemented (see ENERGY_SPEC.md).
    """
    if rates is None or not rates.has_energy:
        return None
    fresh_in, out, cache_read, cache_write = _split_tokens(usage)
    total_input = fresh_in + cache_read + cache_write
    if rates.energy_per_input_token is not None or rates.energy_per_output_token is not None:
        return (
            total_input * (rates.energy_per_input_token or 0.0)
            + out * (rates.energy_per_output_token or 0.0)
        )
    return (total_input + out) * (rates.energy_per_token or 0.0)


def measure_local_energy_wh(*_args, **_kwargs) -> float:
    """Measured local-device energy over the model-call window — NOT IMPLEMENTED.

    Specification (ENERGY_SPEC.md): wrap the model call (``before_model`` /
    ``after_model`` timestamps), sample device power draw across that window, and
    integrate to watt-hours. Backends keyed by ``ModelRates.energy_source``:
      * ``nvidia_smi``  -> ``nvidia-smi --query-gpu=power.draw`` polling
      * ``rapl``        -> Linux Intel RAPL ``/sys/class/powercap/.../energy_uj``
      * ``powermetrics``-> macOS ``powermetrics --samplers cpu_power,gpu_power``
    Until built, local energy uses the per-token estimate above (if declared).
    """
    raise NotImplementedError(
        "local-device energy measurement is specified but not implemented; "
        "see ENERGY_SPEC.md and ModelRates.energy_source"
    )


# --- accumulation ------------------------------------------------------------

class BudgetExceeded(Exception):
    """Raised in after_model when a token/cost ceiling is crossed (caught in run_repl)."""


@dataclass
class UsageAccumulator:
    """Running totals across the calls of a turn (or a whole session).

    ``cost`` sums only *known* costs; ``unpriced_calls`` counts calls a
    RateTable/ReportedCost could not price, so the usage line can flag that the
    dollar figure is a floor, not the truth (M1 addendum: loud, not crashing).
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    energy_wh: float = 0.0
    unpriced_calls: int = 0
    estimated_calls: int = 0  # priced via a best-effort rate: the user-supplied
    #                           DEEPAGENTS_PRICE_ESTIMATE *or* a registry
    #                           [pricing.estimate] table — not a vendor-confirmed rate

    @property
    def total_tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def add(
        self,
        usage: dict,
        pricing: Pricing,
        bare_model: str,
        rates: ModelRates | None = None,
        response_metadata: dict | None = None,
        estimate_per_mtok: float | None = None,
    ) -> None:
        fresh_in, out, cache_read, cache_write = _split_tokens(usage)
        self.input += fresh_in
        self.output += out
        self.cache_read += cache_read
        self.cache_write += cache_write

        price = pricing.cost(usage, bare_model, response_metadata)
        if price is None:
            if estimate_per_mtok is not None:
                # User-supplied fallback $/Mtok over all tokens of the call.
                billable = fresh_in + out + cache_read + cache_write
                self.cost += billable * estimate_per_mtok / PER_MTOK
                self.estimated_calls += 1
            else:
                self.unpriced_calls += 1
        else:
            self.cost += price
            # A real rate produced this price; mark it estimated only when that
            # rate came from a registry [pricing.estimate] table (RateTable only —
            # a ReportedCost in-band dollar figure is the actual bill, never a guess).
            if isinstance(pricing, RateTable) and rates is not None and rates.pricing_source == "estimate":
                self.estimated_calls += 1

        energy = estimate_energy_wh(usage, rates)
        if energy is not None:
            self.energy_wh += energy


def _fmt_usd(amount: float) -> str:
    return f"${amount:.4f}"


def format_line(turn: UsageAccumulator, session: UsageAccumulator, *, electricity_rate: float | None = None) -> str:
    """The ``[harness] usage:`` line: this turn's deltas + the session running total."""

    def block(acc: UsageAccumulator) -> str:
        parts = [
            f"in={acc.input}",
            f"out={acc.output}",
        ]
        if acc.cache_read or acc.cache_write:
            parts.append(f"cache(r={acc.cache_read},w={acc.cache_write})")
        cost = _fmt_usd(acc.cost)
        if acc.unpriced_calls:
            cost += f" (+{acc.unpriced_calls} unpriced)"
        elif acc.estimated_calls:
            cost = "~" + cost + " (est)"
        parts.append(f"cost={cost}")
        if acc.energy_wh:
            parts.append(f"energy={acc.energy_wh:.3f}Wh")
            if electricity_rate is not None:
                parts.append(f"elec={_fmt_usd(acc.energy_wh / 1000 * electricity_rate)}")
        return " ".join(parts)

    return f"[harness] usage: turn[{block(turn)}] session[{block(session)}]"


def format_session_total(session: UsageAccumulator, *, electricity_rate: float | None = None) -> str:
    """The end-of-session total line (printed once when the REPL closes, §1)."""
    parts = [
        f"tokens={session.total_tokens}",
        f"(in={session.input} out={session.output}"
        + (f" cache_r={session.cache_read} cache_w={session.cache_write}" if (session.cache_read or session.cache_write) else "")
        + ")",
    ]
    cost = _fmt_usd(session.cost)
    if session.unpriced_calls:
        cost += f" (+{session.unpriced_calls} unpriced calls — floor only)"
    elif session.estimated_calls:
        cost = "~" + cost + " (estimated)"
    parts.append(f"cost={cost}")
    if session.energy_wh:
        parts.append(f"energy={session.energy_wh:.3f}Wh")
        if electricity_rate is not None:
            parts.append(f"electricity={_fmt_usd(session.energy_wh / 1000 * electricity_rate)}")
    return "[harness] session total: " + " ".join(parts)


# --- middleware --------------------------------------------------------------

class CostTrackerMiddleware(AgentMiddleware):
    """Accumulate usage after every model call; print per-turn + session totals.

    Plugs in exactly like ShellHooksMiddleware — appended to the agent's
    middleware list, ``run_turn`` untouched (§2.5). Built only when there is
    something to report (non-Free pricing, a budget, or energy data); otherwise
    cli.py appends nothing and the harness behaves like the MVP.
    """

    def __init__(
        self,
        pricing: Pricing,
        bare_model: str,
        rates: ModelRates | None = None,
        *,
        max_cost: float | None = None,
        max_tokens: int | None = None,
        estimate_per_mtok: float | None = None,
        electricity_rate: float | None = None,
    ):
        super().__init__()
        self._pricing = pricing
        self._bare_model = bare_model
        self._rates = rates
        self._max_cost = max_cost
        self._max_tokens = max_tokens
        self._estimate = estimate_per_mtok
        self._electricity_rate = electricity_rate
        self.session = UsageAccumulator()
        self.turn = UsageAccumulator()
        self._warned_unpriced = False

    def before_agent(self, state, runtime):
        # Reset the per-turn view; the session total carries across turns.
        self.turn = UsageAccumulator()

    def after_model(self, state, runtime):
        usage, response_metadata = _latest_usage(state)
        if usage is None:
            return
        before_unpriced = self.session.unpriced_calls
        for acc in (self.turn, self.session):
            acc.add(
                usage,
                self._pricing,
                self._bare_model,
                rates=self._rates,
                response_metadata=response_metadata,
                estimate_per_mtok=self._estimate,
            )
        if self.session.unpriced_calls > before_unpriced and not self._warned_unpriced:
            # Loud once, then quiet — the session keeps running, cost just reads
            # as a floor (M1 addendum). Suggest the estimate knob in the warning.
            self._warned_unpriced = True
            print(
                f"[harness] WARNING: no pricing for model '{self._bare_model}'. "
                "Cost shown is a floor (unpriced calls excluded). Set "
                "DEEPAGENTS_PRICE_ESTIMATE=<USD per Mtok> to estimate, or add a "
                "[pricing] / [pricing.estimate] table to its model TOML. "
                "(This warning shows once.)",
                file=sys.stderr,
            )
        self._enforce_budget()

    def after_agent(self, state, runtime):
        print(
            format_line(self.turn, self.session, electricity_rate=self._electricity_rate),
            file=sys.stderr,
        )

    def _enforce_budget(self) -> None:
        if self._max_cost is not None and self.session.cost > self._max_cost:
            raise BudgetExceeded(
                f"cost {_fmt_usd(self.session.cost)} exceeds --max-cost {_fmt_usd(self._max_cost)}"
            )
        if self._max_tokens is not None and self.session.total_tokens > self._max_tokens:
            raise BudgetExceeded(
                f"tokens {self.session.total_tokens} exceeds --max-tokens {self._max_tokens}"
            )


def _latest_usage(state) -> tuple[dict | None, dict | None]:
    """Pull (usage_metadata, response_metadata) off the most recent AIMessage.

    after_model fires once per LLM call, so the last message carries that call's
    usage — counting here sidesteps the "which messages are new this turn"
    problem of a post-invoke walk (§2.1). Returns (None, None) when the last
    message has no usage (e.g. a tool message or a provider that omits it).
    """
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not messages:
        return None, None
    last = messages[-1]
    usage = getattr(last, "usage_metadata", None)
    if usage is None and isinstance(last, dict):
        usage = last.get("usage_metadata")
    response_metadata = getattr(last, "response_metadata", None)
    if response_metadata is None and isinstance(last, dict):
        response_metadata = last.get("response_metadata")
    return (usage if isinstance(usage, dict) else None), response_metadata
