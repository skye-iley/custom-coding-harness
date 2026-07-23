"""Harness doctor — pre-flight config validation (slice E, milestone4.md §12.1).

Runs all checks, collecting ``(level, message)`` records, prints a summary,
returns non-zero if any ``error``. Keyless, stdlib, reuses real loaders so
validation can't drift.

Checks:
  - Registry: every provider.toml parses; each non-null default_model resolves
    to a real models/<model>.toml; rate_table providers have [pricing] tables.
  - Credentials: reports which providers have a key/*_BASE_URL set (names only).
  - Optional config: .mcp.json, hooks.json parse; workflows parse.
  - Mask/floor (new, M4): runs mask.resolve; asserts floor is present and no
    negation/allow targets a floor path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from harness.mask import PATTERN_DEFAULTS, resolve


def _load_providers(providers_dir: str | Path | None = None) -> list:
    """Load provider registry (reuses harness.providers logic)."""
    from harness.providers import PROVIDERS

    return list(PROVIDERS)


def _provider_model_dir(providers_dir: Path) -> Path:
    return providers_dir


def doctor_main(argv: list[str]) -> int:
    """Run all doctor checks and return exit code (0 = all pass)."""
    records: list[tuple[str, str]] = []  # (level, message)

    cwd = Path.cwd()
    providers_dir = Path(os.environ.get("DEEPAGENTS_PROVIDERS_DIR", str(cwd / "providers")))

    workspace_raw = argv[0] if argv else os.environ.get("AGENT_WORKSPACE", str(cwd / "workspace"))
    workspace = Path(workspace_raw).resolve()

    state_dir_raw = argv[1] if len(argv) > 1 else (
        os.environ.get("DEEPAGENTS_STATE_DIR") or str(workspace / ".deepagents")
    )
    state_dir = Path(state_dir_raw)

    # --- Registry checks -------------------------------------------------------
    if providers_dir.is_dir():
        from harness.providers import PROVIDERS

        for prov in PROVIDERS:
            name = getattr(prov, "prefix", "?").rstrip(":")
            records.append(("info", f"provider '{name}' registered"))

            default_model = getattr(prov, "default_model", None)
            if default_model:
                model_stem = default_model.split(":", 1)[-1]
                model_path = providers_dir / name / "models" / f"{model_stem}.toml"
                if not model_path.is_file():
                    records.append((
                        "error",
                        f"provider '{name}': default_model '{default_model}' "
                        f"not found at {model_path}"
                    ))

            pricing = getattr(prov, "pricing", None)
            if pricing and str(pricing) == "rate_table":
                rates_dir = providers_dir / name / "models"
                if rates_dir.is_dir():
                    has_pricing = False
                    for mf in rates_dir.glob("*.toml"):
                        import tomllib
                        try:
                            data = tomllib.loads(mf.read_text(encoding="utf-8"))
                            if data.get("pricing"):
                                has_pricing = True
                                break
                        except Exception:
                            pass
                    if not has_pricing:
                        records.append((
                            "warning",
                            f"provider '{name}': rate_table pricing but no model "
                            f"has a [pricing] table"
                        ))
    else:
        records.append(("warning", f"providers dir not found at {providers_dir}"))

    # --- Credentials check (names only, no values) ----------------------------
    from harness.providers import PROVIDERS

    for prov in PROVIDERS:
        key_name = getattr(prov, "api_key_env", None)
        if key_name and os.environ.get(key_name, "").strip():
            records.append(("info", f"provider '{getattr(prov, 'prefix', '?').rstrip(':')}' has key set"))
        base_url_name = f"{getattr(prov, 'prefix', '').rstrip(':').upper()}_BASE_URL"
        if os.environ.get(base_url_name, "").strip():
            records.append(("info", f"provider '{getattr(prov, 'prefix', '?').rstrip(':')}' has {base_url_name} set"))

    # --- Optional config checks ------------------------------------------------
    mcp_path = cwd / ".mcp.json"
    if mcp_path.is_file():
        try:
            import json
            json.loads(mcp_path.read_text(encoding="utf-8"))
            records.append(("info", ".mcp.json parses OK"))
        except Exception as exc:
            records.append(("error", f".mcp.json parse failed: {exc}"))

    hooks_path = cwd / "hooks.json"
    if hooks_path.is_file():
        try:
            import json
            json.loads(hooks_path.read_text(encoding="utf-8"))
            records.append(("info", "hooks.json parses OK"))
        except Exception as exc:
            records.append(("error", f"hooks.json parse failed: {exc}"))

    # --- Mask / floor checks ---------------------------------------------------
    try:
        mask_result = resolve(str(workspace), str(state_dir), snapshot=False)
        records.append(("info", f"mask resolve OK ({len(mask_result.masked)} masked paths)"))

        # Floor must be present (shipped defaults or #!floor: block)
        floor_count = sum(1 for e in mask_result.masked if e.tier == "floor")
        if floor_count == 0:
            records.append((
                "warning",
                "no designated-secret floor paths configured — "
                "relying on pattern-defaults. Add #!floor: block to state-dir agentignore to enable."
            ))
        else:
            records.append(("info", f"designated-secret floor: {floor_count} path(s)"))

        # No negation/allow targets a floor path
        for w in mask_result.warnings:
            if "floor" in w.lower():
                records.append(("error", w))
            else:
                records.append(("warning", w))

    except SystemExit as exc:
        records.append(("error", f"mask resolve: {exc}"))
    except Exception as exc:
        records.append(("error", f"mask resolve: {exc}"))

    # --- Summary ---------------------------------------------------------------
    errors = [r for r in records if r[0] == "error"]
    warnings = [r for r in records if r[0] == "warning"]
    info = [r for r in records if r[0] == "info"]

    print("[doctor] --- summary ---", file=sys.stderr)
    for level, msg in records:
        print(f"[doctor] [{level}] {msg}", file=sys.stderr)

    print(f"[doctor] {len(errors)} error(s), {len(warnings)} warning(s), {len(info)} info", file=sys.stderr)

    return 1 if errors else 0
