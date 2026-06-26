"""Pull the live model list from each provider's API into the providers/ registry.

Generation-time tool, NOT part of the sealed runtime: it needs provider API
keys and network access, which the agent container deliberately lacks. Run it on
a dev machine (keys in project/.env), then commit the refreshed
providers/<provider>/models/*.toml files.

    python3 -m harness sync-models                 # all providers whose key is set
    python3 -m harness sync-models --only openai anthropic
    python3 -m harness sync-models --dry-run       # show, write nothing
    python3 -m harness sync-models --prune         # also delete models the API no longer lists

Design notes:
- Each provider exposes a different list-models endpoint and metadata shape, so
  fetching is split into a pure `parse_*` step (response JSON -> [ModelInfo],
  unit-tested offline) and a thin `_get_json` HTTP step.
- provider.toml is never rewritten — `default_model` is a human choice. Only the
  models/ directory is touched.
- Unknown metadata is written verbatim; the loader ignores unknown keys
  (providers/README.md), so richer fields are non-breaking.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from harness.providers import PROVIDERS, PROVIDERS_DIR, Provider


@dataclass
class ModelInfo:
    """One model as returned by a provider, or parsed back from a model file.

    `name` is the real id sent to the provider; `extra` is flat metadata
    (ints/strs/bools) written verbatim. The three rate tables are kept separate
    so they survive a merge (`merge_preserving`):
      - `pricing`  — official top-level [pricing] (vendor-published / sync-pulled).
      - `estimate` — hand-filled [pricing.estimate] (not vendor-confirmed; ~/(est)).
      - `energy`   — [energy] (Wh per token).
    API fetches set only `pricing`/`extra`; `estimate` and `energy` are
    hand-filled and never returned by an API, so a refresh must preserve them.

    Every model file is rendered to the SAME canonical layout (see
    `render_model_toml`): a missing field is written as a commented placeholder
    (`# field =`) rather than omitted, so a file always shows which fields exist
    to fill."""

    name: str
    extra: dict[str, object] = field(default_factory=dict)
    pricing: dict[str, object] | None = None
    estimate: dict[str, object] | None = None
    energy: dict[str, object] | None = None

    def merge_preserving(self, old: "ModelInfo | None") -> "ModelInfo":
        """Overlay this freshly-fetched info onto an existing on-disk `old`,
        keeping hand-filled data an API never returns. Fetched values win for
        official pricing + metadata the provider reports; the disk copy's
        `estimate` and `energy` tables (and any extra fields the API dropped)
        are retained. Returns a new ModelInfo; `self`/`old` are untouched."""
        if old is None:
            return self
        return ModelInfo(
            name=self.name,
            extra={**old.extra, **self.extra},      # API metadata wins, disk fills gaps
            pricing=self.pricing or old.pricing,    # fetched official rates win
            estimate=old.estimate,                  # hand-filled — always preserved
            energy=old.energy,                       # hand-filled — always preserved
        )


# --- response parsers (pure: parsed-JSON -> [ModelInfo]) ---------------------

def parse_openai(data: dict) -> list[ModelInfo]:
    """OpenAI-compatible /models (openai, deepseek, cursor, lmstudio)."""
    return [ModelInfo(m["id"]) for m in data.get("data", []) if m.get("id")]


def parse_anthropic(data: dict) -> list[ModelInfo]:
    out = []
    for m in data.get("data", []):
        if not m.get("id"):
            continue
        extra = {}
        if m.get("display_name"):
            extra["display_name"] = m["display_name"]
        if m.get("created_at"):
            extra["created_at"] = m["created_at"]
        out.append(ModelInfo(m["id"], extra))
    return out


def parse_google(data: dict) -> list[ModelInfo]:
    """ListModels: keep only models usable for chat (generateContent)."""
    out = []
    for m in data.get("models", []):
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue
        # name is "models/gemini-3.5-flash"; the spec stem is the last segment.
        ident = m.get("name", "").split("/")[-1]
        if not ident:
            continue
        extra: dict[str, object] = {}
        if m.get("inputTokenLimit"):
            extra["context_window"] = int(m["inputTokenLimit"])
        if m.get("outputTokenLimit"):
            extra["output_token_limit"] = int(m["outputTokenLimit"])
        out.append(ModelInfo(ident, extra))
    return out


def _per_mtok(raw: object) -> float | None:
    """OpenRouter prices are USD per token (string); registry rates are per Mtok."""
    try:
        return round(float(raw) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def parse_openrouter(data: dict, *, as_of: str | None = None) -> list[ModelInfo]:
    as_of = as_of or date.today().isoformat()
    out = []
    for m in data.get("data", []):
        if not m.get("id"):
            continue
        extra: dict[str, object] = {}
        if m.get("context_length"):
            extra["context_window"] = int(m["context_length"])
        raw = m.get("pricing") or {}
        # Emit a [pricing] table (per Mtok) so it loads as ModelRates like the
        # native providers; cache_read maps to OpenRouter's input_cache_read
        # when present. OpenRouter's strategy is `reported` (cost in-band), so
        # this table is a fallback/reference, but keeping the shape uniform.
        pricing: dict[str, object] = {}
        if (v := _per_mtok(raw.get("prompt"))) is not None:
            pricing["input"] = v
        if (v := _per_mtok(raw.get("completion"))) is not None:
            pricing["output"] = v
        if (v := _per_mtok(raw.get("input_cache_read"))) is not None:
            pricing["cache_read"] = v
        if (v := _per_mtok(raw.get("input_cache_write"))) is not None:
            pricing["cache_write"] = v
        if pricing:
            pricing["priced_as_of"] = as_of
        out.append(ModelInfo(m["id"], extra, pricing or None))
    return out


def parse_ollama(data: dict) -> list[ModelInfo]:
    out = []
    for m in data.get("models", []):
        ident = m.get("name")
        if not ident:
            continue
        extra: dict[str, object] = {}
        if m.get("size"):
            extra["size_bytes"] = int(m["size"])
        details = m.get("details") or {}
        if details.get("family"):
            extra["family"] = details["family"]
        if details.get("parameter_size"):
            extra["parameter_size"] = details["parameter_size"]
        out.append(ModelInfo(ident, extra))
    return out


PARSERS = {
    "openai": parse_openai,
    "anthropic": parse_anthropic,
    "google": parse_google,
    "openrouter": parse_openrouter,
    "ollama": parse_ollama,
}


# --- per-provider endpoint table --------------------------------------------

@dataclass(frozen=True)
class Endpoint:
    base: str | None       # default API base; None => must come from base_url_env
    path: str              # appended to base
    auth: str              # 'bearer' | 'x-api-key' | 'query' | 'none'
    style: str             # PARSERS key
    needs_key: bool        # skip when True and the api_key_env is unset
    base_url_env: str | None = None  # extra env that overrides base (besides provider's)
    extra_headers: dict[str, str] = field(default_factory=dict)


ENDPOINTS: dict[str, Endpoint] = {
    "openai:": Endpoint("https://api.openai.com/v1", "/models", "bearer", "openai", True),
    "deepseek:": Endpoint("https://api.deepseek.com", "/models", "bearer", "openai", True),
    "anthropic:": Endpoint(
        "https://api.anthropic.com/v1", "/models", "x-api-key", "anthropic", True,
        extra_headers={"anthropic-version": "2023-06-01"},
    ),
    "google_genai:": Endpoint(
        "https://generativelanguage.googleapis.com/v1beta", "/models", "query", "google", True,
    ),
    "openrouter:": Endpoint("https://openrouter.ai/api/v1", "/models", "bearer", "openrouter", False),
    "cursor:": Endpoint(None, "/models", "bearer", "openai", True),
    "lmstudio:": Endpoint(None, "/models", "bearer", "openai", False),
    "ollama:": Endpoint(
        "http://localhost:11434", "/api/tags", "none", "ollama", False,
        base_url_env="OLLAMA_BASE_URL",
    ),
}


def _resolve_base(provider: Provider, ep: Endpoint) -> str | None:
    """Pick the API base: provider's base_url_env wins, then the endpoint's own
    override env, then the hard-coded default. None => caller must skip."""
    for env in (provider.base_url_env, ep.base_url_env):
        if env and os.getenv(env):
            return os.getenv(env).rstrip("/")
    return ep.base


def _headers(provider: Provider, ep: Endpoint, key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", **ep.extra_headers}
    if ep.auth == "bearer" and key:
        headers["Authorization"] = f"Bearer {key}"
    elif ep.auth == "x-api-key" and key:
        headers["x-api-key"] = key
    return headers


def _build_url(base: str, ep: Endpoint, key: str | None) -> str:
    url = base + ep.path
    if ep.auth == "query" and key:
        url += f"?key={key}"
    return url


# Google-style auth passes the key in the query string (?key=...), so a urllib
# HTTPError/URLError str (which embeds the failing URL) would leak it to stderr.
# Scrub any key=<value> before an error string is ever printed.
_KEY_QUERY_RE = re.compile(r"([?&]key=)[^&\s]+")


def _redact(text: str) -> str:
    return _KEY_QUERY_RE.sub(r"\1REDACTED", text)


def _get_json(url: str, headers: dict[str, str], timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted provider URLs)
        return json.loads(resp.read().decode("utf-8"))


def fetch_models(provider: Provider) -> list[ModelInfo]:
    """Hit one provider's list endpoint. Raises on transport/HTTP errors."""
    ep = ENDPOINTS.get(provider.prefix)
    if ep is None:
        raise RuntimeError(f"no list-models endpoint configured for {provider.prefix}")
    key = os.getenv(provider.api_key_env)
    base = _resolve_base(provider, ep)
    if base is None:
        raise RuntimeError(
            f"{provider.prefix} needs a base URL "
            f"({provider.base_url_env or ep.base_url_env}); set it in .env"
        )
    url = _build_url(base, ep, key)
    data = _get_json(url, _headers(provider, ep, key))
    return PARSERS[ep.style](data)


# --- TOML writing ------------------------------------------------------------

def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


# Canonical field order per section. Every model file renders these in this
# order; a field with no value is written as a `# field =` placeholder (not
# omitted) so the file is a self-documenting template. Extra fields a provider
# reports beyond these are appended (sorted) as real values.
CANONICAL_META = ("display_name", "context_window")
CANONICAL_PRICING = ("input", "output", "cache_read", "cache_write", "priced_as_of")
# estimate tables additionally carry a `source` note (provenance of the guess).
CANONICAL_ESTIMATE = CANONICAL_PRICING + ("source",)
CANONICAL_ENERGY = ("per_input_token", "per_output_token", "source")


def _field_line(key: str, value: object) -> str:
    """`key = value` when a value is recorded, else a commented `# key =` placeholder."""
    if value is None:
        return f"# {key} ="
    return f"{key} = {_toml_scalar(value)}"


def _section(lines: list[str], canonical: tuple[str, ...], data: dict[str, object]) -> None:
    """Emit `canonical` keys in order (commented if absent), then any leftover
    real fields sorted. Mutates `data` (pops the canonical keys)."""
    rest = dict(data)
    for key in canonical:
        lines.append(_field_line(key, rest.pop(key, None)))
    for key in sorted(rest):
        lines.append(_field_line(key, rest[key]))


def render_model_toml(info: ModelInfo) -> str:
    """Serialize a ModelInfo to the canonical TOML layout shared by every model
    file: `name`, a metadata block, a [pricing] (or [pricing.estimate]) table, and
    an [energy] table. Fields with no value are emitted as commented placeholders
    so the format is identical across files and self-documents what can be filled."""
    lines = [f"name = {_toml_scalar(info.name)}", ""]

    lines.append("# Model metadata (commented = not recorded).")
    _section(lines, CANONICAL_META, dict(info.extra))

    lines.append("")
    lines.append("# USD per million tokens. Top-level [pricing] = official (vendor-published")
    lines.append("# / sync-pulled). [pricing.estimate] = hand-filled, not vendor-confirmed")
    lines.append("# (shown ~/(est)). See providers/README.md.")
    lines.append("[pricing]")
    _section(lines, CANONICAL_PRICING, dict(info.pricing or {}))

    # The estimate sub-table is hand-filled, so it is only emitted when it
    # carries data (a refresh preserves it via merge_preserving). Official
    # [pricing] above always appears as the promote-to target.
    if info.estimate:
        lines.append("")
        lines.append("[pricing.estimate]")
        _section(lines, CANONICAL_ESTIMATE, dict(info.estimate))

    lines.append("")
    lines.append("# Watt-hours per token (optional estimate; tracked even for free models).")
    lines.append("[energy]")
    _section(lines, CANONICAL_ENERGY, dict(info.energy or {}))

    return "\n".join(lines) + "\n"


def parse_model_toml(text: str, stem: str = "") -> ModelInfo:
    """Inverse of render: parse a model file back into a ModelInfo, splitting the
    official [pricing] from the hand-filled [pricing.estimate]. Used to merge an
    on-disk file with a fresh fetch (`ModelInfo.merge_preserving`) so estimates
    and energy survive a sync. Legacy flat `price_prompt`/`price_completion`
    strings (older sync output) are folded into the official [pricing]."""
    data = tomllib.loads(text)
    name = data.get("name", stem)
    pricing_tbl = data.get("pricing") or {}
    estimate = pricing_tbl.get("estimate") or None
    official = {k: v for k, v in pricing_tbl.items() if k != "estimate"} or None
    energy = data.get("energy") or None
    extra = {k: v for k, v in data.items() if k not in ("name", "pricing", "energy")}

    pp = extra.pop("price_prompt", None)
    pc = extra.pop("price_completion", None)
    if pp is not None or pc is not None:
        official = dict(official or {})
        if (v := _per_mtok(pp)) is not None:
            official["input"] = v
        if (v := _per_mtok(pc)) is not None:
            official["output"] = v
        official.setdefault("priced_as_of", date.today().isoformat())

    return ModelInfo(name, extra, official or None, estimate, energy)


def model_filename(model_id: str) -> str:
    """Filesystem-safe stem for a model id. The real id is kept in the file's
    `name` field, so this only needs to be unique and valid (ids carry '/'/':')."""
    safe = model_id
    for ch in '/\\:*?"<>|':
        safe = safe.replace(ch, "_")
    return safe + ".toml"


def _provider_models_dir(provider: Provider) -> Path:
    # Dir name == prefix without the trailing ':' (registry convention). Holds
    # unless provider.toml overrode `prefix` to differ from its dir name.
    return PROVIDERS_DIR / provider.prefix.rstrip(":") / "models"


# --- orchestration -----------------------------------------------------------

def sync_provider(
    provider: Provider, *, dry_run: bool, prune: bool, log=print,
) -> tuple[int, int]:
    """Refresh one provider's models/ dir. Returns (written, pruned)."""
    models = fetch_models(provider)
    models_dir = _provider_models_dir(provider)
    if not dry_run:
        models_dir.mkdir(parents=True, exist_ok=True)

    wanted: set[str] = set()
    written = 0
    for info in models:
        fname = model_filename(info.name)
        wanted.add(fname)
        target = models_dir / fname
        # Merge onto the existing file so hand-filled [pricing.estimate] / [energy]
        # (which no API returns) survive the refresh; fetched official rates +
        # provider metadata still win. See ModelInfo.merge_preserving.
        old_bytes = target.read_bytes() if target.exists() else None
        old_info = parse_model_toml(old_bytes.decode("utf-8"), target.stem) if old_bytes else None
        body = render_model_toml(info.merge_preserving(old_info))
        # Compare and write as bytes so the on-disk newline is always LF
        # (render joins with "\n"): write_text() would translate to CRLF on
        # Windows, and read_text() would mask CRLF drift on a re-read. Bytes
        # make the LF decision explicit and rewrite any stale CRLF file.
        new_bytes = body.encode("utf-8")
        if old_bytes != new_bytes:
            if not dry_run:
                target.write_bytes(new_bytes)
            written += 1
    log(f"  {provider.prefix} {len(models)} models ({written} new/changed)")

    pruned = 0
    if prune and models_dir.is_dir():
        for existing in models_dir.glob("*.toml"):
            if existing.name not in wanted:
                log(f"    prune {existing.name}")
                if not dry_run:
                    existing.unlink()
                pruned += 1
    return written, pruned


def _selected(only: list[str] | None) -> list[Provider]:
    if not only:
        return list(PROVIDERS)
    want = {o.rstrip(":") for o in only}
    return [p for p in PROVIDERS if p.prefix.rstrip(":") in want]


def sync_models_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness sync-models",
        description="Pull live model lists from provider APIs into providers/.",
    )
    parser.add_argument("--only", nargs="+", metavar="PROVIDER",
                        help="Limit to these providers (e.g. openai anthropic).")
    parser.add_argument("--dry-run", action="store_true", help="Show changes, write nothing.")
    parser.add_argument("--prune", action="store_true",
                        help="Delete model files the API no longer lists.")
    args = parser.parse_args(argv)

    # Load .env so keys/base URLs are available exactly like the runtime sees them.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ModuleNotFoundError:
        pass

    providers = _selected(args.only)
    if not providers:
        print(f"No providers match --only {args.only}.", file=sys.stderr)
        return 2

    total_written = total_pruned = total_err = 0
    for provider in providers:
        ep = ENDPOINTS.get(provider.prefix)
        if ep is None:
            print(f"  {provider.prefix} skip: no endpoint configured", file=sys.stderr)
            continue
        if ep.needs_key and not os.getenv(provider.api_key_env):
            print(f"  {provider.prefix} skip: {provider.api_key_env} unset", file=sys.stderr)
            continue
        try:
            w, p = sync_provider(provider, dry_run=args.dry_run, prune=args.prune)
            total_written += w
            total_pruned += p
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, RuntimeError,
                json.JSONDecodeError, KeyError) as exc:
            total_err += 1
            print(f"  {provider.prefix} ERROR: {_redact(str(exc))}", file=sys.stderr)

    verb = "would write" if args.dry_run else "wrote"
    print(f"\n{verb} {total_written} model file(s); "
          f"pruned {total_pruned}; {total_err} provider error(s).")
    return 1 if total_err else 0
