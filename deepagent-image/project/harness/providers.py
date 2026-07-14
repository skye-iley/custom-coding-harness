"""Provider registry and model selection.

PROVIDERS is the single source of truth: choose_model, validate_credentials,
and resolve_chat_model all derive from it so the maps can't drift. Unlike the
old hard-coded list, PROVIDERS is now LOADED from the on-disk registry at
`<project>/providers/` (see that dir's README.md for the layout). Each
provider is one `<provider>/provider.toml`; its models are
`<provider>/models/<model>.toml`. This keeps per-provider and per-model config
in version-controlled files instead of one Python literal.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# cost.py holds the Pricing types + rate math. The import is one-directional
# (providers -> cost) on purpose: cost.py must never import providers, so the
# two don't form a cycle (docs/milestones/milestone1.md §2.4).
from harness.cost import (
    ModelRates,
    Pricing,
    Free,
    pricing_from_strategy,
    rates_from_toml,
)

# Registry root: <project>/providers/. providers.py lives at
# <project>/harness/providers.py, so parent.parent is <project>. Holds in the
# repo and in the container (harness at /project/harness, registry at
# /project/providers). Override with DEEPAGENTS_PROVIDERS_DIR for tests.
PROVIDERS_DIR = Path(
    os.getenv("DEEPAGENTS_PROVIDERS_DIR")
    or (Path(__file__).resolve().parent.parent / "providers")
)


@dataclass(frozen=True)
class Provider:
    """One provider loaded from <provider>/provider.toml. choose_model,
    validate_credentials, and resolve_chat_model all derive from this registry
    so the maps can't drift."""

    prefix: str              # model spec prefix, e.g. "openai:"
    api_key_env: str         # env var that holds the key / opts the provider in
    default_model: str | None  # auto-select default; None => never auto-selected
    requires_key: bool       # validate_credentials enforces api_key_env
    base_url_env: str | None = None  # set => OpenAI-compatible, routed via ChatOpenAI
    priority: int = 1_000    # auto-selection order; lowest wins
    models: tuple[str, ...] = field(default_factory=tuple)  # known model specs
    # Cost/energy tracker fields (Milestone 1). pricing is the per-provider
    # strategy declared in provider.toml; model_rates maps the bare model id
    # (spec minus prefix) to its TOML [pricing]/[energy] data. Both default to
    # the MVP-equivalent null state (Free, no rates) so the tracker is opt-in.
    pricing: Pricing = field(default_factory=Free)
    model_rates: dict[str, ModelRates] = field(default_factory=dict)

    def rates_for(self, model: str) -> ModelRates | None:
        """ModelRates for a full model spec, keyed by the bare id after prefix."""
        return self.model_rates.get(model[len(self.prefix):])


def _load_provider(provider_dir: Path) -> Provider:
    """Build one Provider from <provider>/provider.toml + its models/ dir."""
    config_path = provider_dir / "provider.toml"
    with config_path.open("rb") as fh:
        cfg = tomllib.load(fh)

    name = provider_dir.name
    prefix = cfg.get("prefix", f"{name}:")

    try:
        api_key_env = cfg["api_key_env"]
        requires_key = bool(cfg["requires_key"])
        priority = int(cfg["priority"])
    except KeyError as exc:
        raise SystemExit(
            f"Provider config {config_path} is missing required field {exc}."
        ) from exc

    # Collect known models from models/*.toml; each file's `name` (default: its
    # stem) is appended to the prefix to form the full spec. Each file's
    # optional [pricing]/[energy] tables become the model's ModelRates, keyed by
    # the bare id so cost.py can price a turn (Milestone 1).
    models: list[str] = []
    model_rates: dict[str, ModelRates] = {}
    models_dir = provider_dir / "models"
    if models_dir.is_dir():
        for model_path in sorted(models_dir.glob("*.toml")):
            with model_path.open("rb") as fh:
                model_cfg = tomllib.load(fh)
            bare = model_cfg.get("name", model_path.stem)
            models.append(prefix + bare)
            # Every model file now carries [pricing]/[energy] sections (commented
            # placeholders when unfilled), so the tables parse as empty dicts.
            # Treat empty == absent: only models with real rate/energy data get a
            # ModelRates entry, keeping behavior identical to flat name-only files.
            pricing_tbl = model_cfg.get("pricing")
            energy_tbl = model_cfg.get("energy")
            if pricing_tbl or energy_tbl:
                model_rates[bare] = rates_from_toml(pricing_tbl, energy_tbl)

    # default_model is a model stem in provider.toml; expand to a full spec.
    default_stem = cfg.get("default_model")
    default_model = prefix + default_stem if default_stem else None
    if default_model and default_model not in models:
        raise SystemExit(
            f"Provider '{name}' default_model '{default_stem}' has no matching "
            f"models/{default_stem}.toml."
        )

    # pricing strategy: provider.toml `pricing = "rate_table"|"reported"|"free"`
    # (default free => behaves like the MVP). RateTable carries the per-model
    # rates so cost.py can look them up.
    pricing = pricing_from_strategy(cfg.get("pricing"), model_rates)

    return Provider(
        prefix=prefix,
        api_key_env=api_key_env,
        default_model=default_model,
        requires_key=requires_key,
        base_url_env=cfg.get("base_url_env"),
        priority=priority,
        models=tuple(models),
        pricing=pricing,
        model_rates=model_rates,
    )


def _load_providers(registry_dir: Path = PROVIDERS_DIR) -> list[Provider]:
    """Load every provider from the registry, ordered by priority.

    Auto-selection scans the returned list top-to-bottom, so priority = order
    (lowest first). Local providers (ollama, lmstudio) carry requires_key=False.
    """
    if not registry_dir.is_dir():
        raise SystemExit(
            f"Provider registry not found at {registry_dir}. Expected a "
            "providers/ directory (see providers/README.md)."
        )
    providers = [
        _load_provider(child)
        for child in registry_dir.iterdir()
        if child.is_dir() and (child / "provider.toml").is_file()
    ]
    if not providers:
        raise SystemExit(
            f"No providers found under {registry_dir}; expected "
            "<provider>/provider.toml entries."
        )
    providers.sort(key=lambda p: p.priority)
    return providers


PROVIDERS: list[Provider] = _load_providers()


def _provider_for(model: str) -> Provider | None:
    """Registry entry whose prefix matches the model spec (None if unknown)."""
    for provider in PROVIDERS:
        if model.startswith(provider.prefix):
            return provider
    return None


def provider_for(model: str) -> Provider | None:
    """Public registry lookup by model spec (None if no prefix matches).

    Used by the cost tracker wiring in cli.py to read the resolved model's
    pricing strategy + rates. Thin wrapper over the internal _provider_for.
    """
    return _provider_for(model)


def choose_model(explicit_model: str | None) -> str:
    if explicit_model:
        return explicit_model

    env_model = os.getenv("DEEPAGENTS_MODEL")
    if env_model:
        return env_model

    for provider in PROVIDERS:
        if provider.default_model and os.getenv(provider.api_key_env):
            return provider.default_model

    raise SystemExit(
        "No model configured. Set DEEPAGENTS_MODEL plus the matching provider "
        "API key, or set OPENAI_API_KEY / GOOGLE_API_KEY."
    )


def validate_credentials(model: str) -> None:
    # Local providers (ollama, lmstudio) carry requires_key=False, so they are
    # not enforced here. Unknown prefixes pass through to init_chat_model.
    provider = _provider_for(model)
    if provider is None:
        # Passthrough stays intentional, but surface it: a typo'd prefix
        # (e.g. 'claude:' for 'anthropic:') would otherwise skip validation
        # and reappear as a raw init_chat_model traceback. Note, don't fail.
        known = ", ".join(p.prefix for p in PROVIDERS)
        print(
            f"[harness] note: model '{model}' matches no known provider prefix "
            f"({known}); passing through to init_chat_model.",
            file=sys.stderr,
        )
        return
    if provider.requires_key and not os.getenv(provider.api_key_env):
        raise SystemExit(f"Model '{model}' requires {provider.api_key_env}.")


def resolve_chat_model(model: str):
    """Turn a model spec into something create_deep_agent accepts.

    Native init_chat_model providers (openai/anthropic/google_genai/deepseek/
    ollama) pass through unchanged as a string. OpenAI-compatible providers
    (those with a base_url_env: cursor/openrouter/lmstudio) have no native
    prefix, so build a ChatOpenAI client pointed at their base_url. LM Studio
    runs keyless, so the api key falls back to a placeholder when unset.
    """
    provider = _provider_for(model)
    if provider and provider.base_url_env:
        base_url = os.getenv(provider.base_url_env)
        if not base_url:
            raise SystemExit(f"Model '{model}' requires {provider.base_url_env}.")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model[len(provider.prefix):],
            base_url=base_url,
            api_key=os.getenv(provider.api_key_env) or "not-needed",
        )
    return model


def init_summary_model(model: str):
    """Return an *invokable* chat model (has `.invoke`) for the given spec.

    resolve_chat_model returns a bare string for native providers — fine for
    create_deep_agent, which calls init_chat_model itself, but archive.summarize
    needs a real client. OpenAI-compatible providers already resolve to a
    ChatOpenAI object; native providers are initialized here via the same
    init_chat_model path create_deep_agent uses (the "<provider>:<model>" prefix
    doubles as init_chat_model's provider hint)."""
    resolved = resolve_chat_model(model)
    if not isinstance(resolved, str):
        return resolved
    from langchain.chat_models import init_chat_model

    return init_chat_model(resolved)
