"""Tests for harness/providers.py — the model-routing registry.

PROVIDERS is the single source of truth for choose_model / validate_credentials /
resolve_chat_model, loaded from on-disk TOML at import time. These tests build
small throwaway registries under pytest's tmp_path and either parse them with
`_load_providers(dir)` directly or monkeypatch them onto `providers.PROVIDERS` to
drive the routing functions — so each case pins exactly the registry it asserts
against instead of the live committed one, and nothing is written outside tmp.

providers.py imports only harness.cost + tomllib, so this runs on a bare
interpreter via the _bootstrap loader. The one path that needs langchain_openai
(building a ChatOpenAI for an OpenAI-compatible provider) is importorskip-gated.
"""

from __future__ import annotations

import textwrap

import pytest

from _bootstrap import _load

providers = _load("harness.providers")


# --- registry-building helpers --------------------------------------------

def _write_provider(
    root,
    name,
    *,
    api_key_env,
    requires_key=False,
    priority=1,
    default_model=None,
    prefix=None,
    base_url_env=None,
    pricing=None,
    omit_field=None,
    models=(),
):
    """Write a <name>/provider.toml (+ models/*.toml) under `root`.

    `models` is an iterable of (stem, body) — body is raw TOML appended after the
    auto `name = "<stem>"` line (use it to add [pricing]/[energy]). `omit_field`
    drops one otherwise-required field to exercise the missing-key error.
    """
    pdir = root / name
    (pdir / "models").mkdir(parents=True)
    fields = {
        "api_key_env": f'"{api_key_env}"',
        "requires_key": "true" if requires_key else "false",
        "priority": str(priority),
    }
    if prefix is not None:
        fields["prefix"] = f'"{prefix}"'
    if default_model is not None:
        fields["default_model"] = f'"{default_model}"'
    if base_url_env is not None:
        fields["base_url_env"] = f'"{base_url_env}"'
    if pricing is not None:
        fields["pricing"] = f'"{pricing}"'
    fields.pop(omit_field, None)
    (pdir / "provider.toml").write_text(
        "".join(f"{k} = {v}\n" for k, v in fields.items()), encoding="utf-8"
    )
    for stem, body in models:
        (pdir / "models" / f"{stem}.toml").write_text(
            f'name = "{stem}"\n{body}', encoding="utf-8"
        )
    return pdir


# --- _load_provider / _load_providers -------------------------------------

def test_load_provider_defaults_prefix_to_name_colon(tmp_path):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    models=[("m1", "")])
    [p] = providers._load_providers(tmp_path)
    assert p.prefix == "acme:"
    assert p.models == ("acme:m1",)


def test_load_provider_honors_explicit_prefix(tmp_path):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    prefix="acme_x:", models=[("m1", "")])
    [p] = providers._load_providers(tmp_path)
    assert p.prefix == "acme_x:"
    assert p.models == ("acme_x:m1",)


def test_load_provider_missing_required_field_raises(tmp_path):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    omit_field="priority", models=[("m1", "")])
    with pytest.raises(SystemExit) as exc:
        providers._load_providers(tmp_path)
    assert "priority" in str(exc.value)


def test_load_provider_default_model_expands_to_full_spec(tmp_path):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    default_model="m1", models=[("m1", "")])
    [p] = providers._load_providers(tmp_path)
    assert p.default_model == "acme:m1"


def test_load_provider_default_model_without_file_raises(tmp_path):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    default_model="ghost", models=[("m1", "")])
    with pytest.raises(SystemExit) as exc:
        providers._load_providers(tmp_path)
    assert "ghost" in str(exc.value)


def test_load_provider_no_default_is_none(tmp_path):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    models=[("m1", "")])
    [p] = providers._load_providers(tmp_path)
    assert p.default_model is None


def test_models_sorted_and_rates_only_for_priced(tmp_path):
    priced = "[pricing]\ninput = 1.0\noutput = 2.0\n"
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY", pricing="rate_table",
                    models=[("b-plain", ""), ("a-priced", priced)])
    [p] = providers._load_providers(tmp_path)
    assert p.models == ("acme:a-priced", "acme:b-plain")  # sorted by stem
    assert set(p.model_rates) == {"a-priced"}              # only the priced one
    assert p.model_rates["a-priced"].input == 1.0


def test_load_providers_orders_by_priority(tmp_path):
    _write_provider(tmp_path, "low", api_key_env="LOW_API_KEY", priority=5,
                    models=[("m", "")])
    _write_provider(tmp_path, "high", api_key_env="HIGH_API_KEY", priority=1,
                    models=[("m", "")])
    loaded = providers._load_providers(tmp_path)
    assert [p.prefix for p in loaded] == ["high:", "low:"]  # lowest priority first


def test_load_providers_missing_dir_raises(tmp_path):
    with pytest.raises(SystemExit):
        providers._load_providers(tmp_path / "does-not-exist")


def test_load_providers_empty_dir_raises(tmp_path):
    with pytest.raises(SystemExit) as exc:
        providers._load_providers(tmp_path)
    assert "No providers" in str(exc.value)


# --- provider_for / rates_for ---------------------------------------------

def test_provider_for_matches_prefix(tmp_path, monkeypatch):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY", models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    assert providers.provider_for("acme:m1").prefix == "acme:"
    assert providers.provider_for("unknown:x") is None


def test_rates_for_keys_on_bare_id(tmp_path):
    priced = "[pricing]\ninput = 3.0\n"
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY", pricing="rate_table",
                    models=[("m1", priced)])
    [p] = providers._load_providers(tmp_path)
    assert p.rates_for("acme:m1").input == 3.0
    assert p.rates_for("acme:nope") is None


# --- choose_model ----------------------------------------------------------

def _two_provider_registry(tmp_path):
    # Both keyed: these cases are about the api_key_env gate, and only a keyed
    # provider is gated on it (a keyless one is always available -- see the
    # keyless auto-select cases below).
    _write_provider(tmp_path, "alpha", api_key_env="ALPHA_API_KEY", requires_key=True,
                    priority=1, default_model="a1", models=[("a1", "")])
    _write_provider(tmp_path, "beta", api_key_env="BETA_API_KEY", requires_key=True,
                    priority=2, default_model="b1", models=[("b1", "")])
    return providers._load_providers(tmp_path)


def test_choose_model_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", _two_provider_registry(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_MODEL", "beta:b1")
    monkeypatch.setenv("ALPHA_API_KEY", "k")
    assert providers.choose_model("explicit:x") == "explicit:x"


def test_choose_model_env_over_autoselect(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", _two_provider_registry(tmp_path))
    monkeypatch.setenv("DEEPAGENTS_MODEL", "beta:b1")
    monkeypatch.setenv("ALPHA_API_KEY", "k")
    assert providers.choose_model(None) == "beta:b1"


def test_choose_model_autoselect_by_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", _two_provider_registry(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
    # Both keys present -> lowest-priority provider (alpha) wins.
    monkeypatch.setenv("ALPHA_API_KEY", "k")
    monkeypatch.setenv("BETA_API_KEY", "k")
    assert providers.choose_model(None) == "alpha:a1"


def test_choose_model_skips_provider_without_key(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", _two_provider_registry(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
    monkeypatch.delenv("ALPHA_API_KEY", raising=False)  # alpha unavailable
    monkeypatch.setenv("BETA_API_KEY", "k")
    assert providers.choose_model(None) == "beta:b1"


def test_choose_model_none_available_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", _two_provider_registry(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
    monkeypatch.delenv("ALPHA_API_KEY", raising=False)
    monkeypatch.delenv("BETA_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        providers.choose_model(None)


# --- keyless auto-selection (ollama as the shipped default) -----------------
#
# Regression guard: auto-selection used to gate on `os.getenv(api_key_env)` for
# every provider, so a keyless one (requires_key = false) could never be picked
# no matter what -- there is no credential to detect. That is why ollama had to
# stay `default_model`-less. The gate now consults requires_key first.

def test_provider_available_keyless_needs_no_key(tmp_path, monkeypatch):
    _write_provider(tmp_path, "local", api_key_env="LOCAL_API_KEY",
                    requires_key=False, models=[("m1", "")])
    [p] = providers._load_providers(tmp_path)
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    assert providers.provider_available(p) is True


def test_provider_available_keyed_needs_key(tmp_path, monkeypatch):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY",
                    requires_key=True, models=[("m1", "")])
    [p] = providers._load_providers(tmp_path)
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    assert providers.provider_available(p) is False
    monkeypatch.setenv("ACME_API_KEY", "k")
    assert providers.provider_available(p) is True


def test_choose_model_autoselects_keyless_provider_without_key(tmp_path, monkeypatch):
    _write_provider(tmp_path, "local", api_key_env="LOCAL_API_KEY", requires_key=False,
                    priority=0, default_model="m1", models=[("m1", "")])
    _write_provider(tmp_path, "cloud", api_key_env="CLOUD_API_KEY", requires_key=True,
                    priority=1, default_model="c1", models=[("c1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    monkeypatch.setenv("CLOUD_API_KEY", "k")  # cloud is available too...
    # ...but the keyless local provider is lower priority, so it wins.
    assert providers.choose_model(None) == "local:m1"


def test_choose_model_keyless_still_skipped_without_default_model(tmp_path, monkeypatch):
    # requires_key=false alone is not enough: no default_model => never auto-picked
    # (lmstudio/openrouter stay in that state).
    _write_provider(tmp_path, "local", api_key_env="LOCAL_API_KEY", requires_key=False,
                    priority=0, models=[("m1", "")])
    _write_provider(tmp_path, "cloud", api_key_env="CLOUD_API_KEY", requires_key=True,
                    priority=1, default_model="c1", models=[("c1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
    monkeypatch.setenv("CLOUD_API_KEY", "k")
    assert providers.choose_model(None) == "cloud:c1"


def test_shipped_registry_autoselects_ollama(monkeypatch):
    """The shipped providers/ dir must resolve to a local model with no keys set."""
    monkeypatch.delenv("DEEPAGENTS_MODEL", raising=False)
    for provider in providers.PROVIDERS:
        monkeypatch.delenv(provider.api_key_env, raising=False)
    assert providers.choose_model(None) == "ollama:gemma4"


# --- validate_credentials --------------------------------------------------

def test_validate_unknown_prefix_passes_through(tmp_path, monkeypatch, capsys):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY", requires_key=True,
                    models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    providers.validate_credentials("typo:model")  # must not raise
    assert "matches no known provider prefix" in capsys.readouterr().err


def test_validate_requires_key_without_key_raises(tmp_path, monkeypatch):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY", requires_key=True,
                    models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        providers.validate_credentials("acme:m1")
    assert "ACME_API_KEY" in str(exc.value)


def test_validate_requires_key_with_key_ok(tmp_path, monkeypatch):
    _write_provider(tmp_path, "acme", api_key_env="ACME_API_KEY", requires_key=True,
                    models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.setenv("ACME_API_KEY", "k")
    providers.validate_credentials("acme:m1")  # no raise


def test_validate_keyless_provider_ok(tmp_path, monkeypatch):
    _write_provider(tmp_path, "local", api_key_env="LOCAL_API_KEY", requires_key=False,
                    models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    providers.validate_credentials("local:m1")  # requires_key=False -> no raise


# --- resolve_chat_model ----------------------------------------------------

def test_resolve_native_provider_passthrough(tmp_path, monkeypatch):
    _write_provider(tmp_path, "openai", api_key_env="OPENAI_API_KEY",
                    models=[("gpt", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    assert providers.resolve_chat_model("openai:gpt") == "openai:gpt"


def test_resolve_unknown_prefix_passthrough(tmp_path, monkeypatch):
    _write_provider(tmp_path, "openai", api_key_env="OPENAI_API_KEY",
                    models=[("gpt", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    assert providers.resolve_chat_model("mystery:m") == "mystery:m"


def test_resolve_openai_compatible_missing_base_url_raises(tmp_path, monkeypatch):
    _write_provider(tmp_path, "compat", api_key_env="COMPAT_API_KEY",
                    base_url_env="COMPAT_BASE_URL", models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.delenv("COMPAT_BASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        providers.resolve_chat_model("compat:m1")
    assert "COMPAT_BASE_URL" in str(exc.value)


def test_resolve_openai_compatible_builds_client(tmp_path, monkeypatch):
    pytest.importorskip("langchain_openai")  # only present in the runtime/test image
    _write_provider(tmp_path, "compat", api_key_env="COMPAT_API_KEY",
                    base_url_env="COMPAT_BASE_URL", models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.setenv("COMPAT_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.delenv("COMPAT_API_KEY", raising=False)  # keyless -> placeholder key
    client = providers.resolve_chat_model("compat:m1")
    assert client.model_name == "m1"  # prefix stripped for the wire call


# --- init_summary_model: always invokable (archive.summarize needs .invoke) --

def test_init_summary_model_native_calls_init_chat_model(tmp_path, monkeypatch):
    # Native providers pass through resolve_chat_model as a bare string; the
    # summary model must instead be a real client, so init_chat_model is invoked
    # with that spec (the same "<provider>:<model>" create_deep_agent uses).
    lcm = pytest.importorskip("langchain.chat_models")
    _write_provider(tmp_path, "openai", api_key_env="OPENAI_API_KEY",
                    models=[("gpt", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    seen = {}
    sentinel = object()

    def fake_init(spec, *a, **k):
        seen["spec"] = spec
        return sentinel

    monkeypatch.setattr(lcm, "init_chat_model", fake_init)
    assert providers.init_summary_model("openai:gpt") is sentinel
    assert seen["spec"] == "openai:gpt"


def test_init_summary_model_openai_compatible_returns_client(tmp_path, monkeypatch):
    # OpenAI-compatible providers already resolve to an invokable client — no
    # init_chat_model round-trip needed.
    pytest.importorskip("langchain_openai")
    _write_provider(tmp_path, "compat", api_key_env="COMPAT_API_KEY",
                    base_url_env="COMPAT_BASE_URL", models=[("m1", "")])
    monkeypatch.setattr(providers, "PROVIDERS", providers._load_providers(tmp_path))
    monkeypatch.setenv("COMPAT_BASE_URL", "http://localhost:1234/v1")
    client = providers.init_summary_model("compat:m1")
    assert hasattr(client, "invoke") and not isinstance(client, str)
