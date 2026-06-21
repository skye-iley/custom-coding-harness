"""Provider registry and model selection.

PROVIDERS is the single source of truth: choose_model, validate_credentials,
and resolve_chat_model all derive from it so the maps can't drift.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# NOTE: eventually set up a config file to define the ordering and default models given the API keys
DEFAULT_OPENAI_MODEL = "openai:gpt-5.5"
DEFAULT_GOOGLE_MODEL = "google_genai:gemini-3.5-flash"
DEFAULT_CLAUDE_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_CURSOR_MODEL = "cursor:composer-2.5"
DEFAULT_DEEPSEEK_MODEL = "deepseek:deepseek-v4-flash"
# left as "None" intentionally until I have them set up.
DEFAULT_OLLAMA_MODEL = None
DEFAULT_LMSTUDIO_MODEL = None
DEFAULT_OPENROUTER_MODEL = None


@dataclass(frozen=True)
class Provider:
    """One provider declared once. choose_model, validate_credentials, and
    resolve_chat_model all derive from this registry so the maps can't drift."""

    prefix: str              # model spec prefix, e.g. "openai:"
    api_key_env: str         # env var that holds the key / opts the provider in
    default_model: str | None  # auto-select default; None => never auto-selected
    requires_key: bool       # validate_credentials enforces api_key_env
    base_url_env: str | None = None  # set => OpenAI-compatible, routed via ChatOpenAI


# Auto-selection scans this list top-to-bottom, so order = priority when several
# provider keys are set. Local providers (ollama, lmstudio) need no real key.
PROVIDERS: list[Provider] = [
    Provider("google_genai:", "GOOGLE_API_KEY", DEFAULT_GOOGLE_MODEL, requires_key=True),
    Provider("anthropic:", "ANTHROPIC_API_KEY", DEFAULT_CLAUDE_MODEL, requires_key=True),
    Provider("openai:", "OPENAI_API_KEY", DEFAULT_OPENAI_MODEL, requires_key=True),
    Provider("cursor:", "CURSOR_API_KEY", DEFAULT_CURSOR_MODEL, requires_key=True, base_url_env="CURSOR_BASE_URL"),
    Provider("ollama:", "OLLAMA_API_KEY", DEFAULT_OLLAMA_MODEL, requires_key=False),
    Provider("lmstudio:", "LMSTUDIO_API_KEY", DEFAULT_LMSTUDIO_MODEL, requires_key=False, base_url_env="LMSTUDIO_BASE_URL"),
    Provider("deepseek:", "DEEPSEEK_API_KEY", DEFAULT_DEEPSEEK_MODEL, requires_key=True),
    Provider("openrouter:", "OPENROUTER_API_KEY", DEFAULT_OPENROUTER_MODEL, requires_key=True, base_url_env="OPENROUTER_BASE_URL"),
]


def _provider_for(model: str) -> Provider | None:
    """Registry entry whose prefix matches the model spec (None if unknown)."""
    for provider in PROVIDERS:
        if model.startswith(provider.prefix):
            return provider
    return None


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
