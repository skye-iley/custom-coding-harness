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
  - State-dir isolation (new, M4): in-container, the harness state dir must
    resolve *outside* the workspace, or the agent's own file tools can read and
    rewrite the stores that are supposed to be beyond its reach.
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


def state_dir_inside_workspace(state_dir: str | Path, workspace: str | Path) -> bool:
    """True when the harness state dir resolves inside the workspace tree.

    ``realpath`` + ``commonpath`` (not ``startswith``) for the same reason
    ``pathguard`` uses them: a sibling like ``<ws>-state`` must not read as
    inside ``<ws>``, and a symlinked state dir must be judged by its target.
    ``commonpath`` raises on inputs with no shared root (different Windows
    drives) — that is definitively *not* inside, so it degrades to False."""
    try:
        state = os.path.realpath(str(state_dir))
        ws = os.path.realpath(str(workspace))
        return os.path.commonpath([state, ws]) == ws
    except (ValueError, OSError):
        return False


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

    # --- State-dir isolation ---------------------------------------------------
    # `archive.state_dir` falls back to `<workspace>/.deepagents` when
    # DEEPAGENTS_STATE_DIR is unset. In-container that fallback puts
    # checkpoints.sqlite / past.sqlite / denials.jsonl back inside the workspace
    # bind-mount, in-bounds for the path guard and writable by the agent's own
    # file tools — including the denial log recording its escape attempts. Both
    # launchers set the var, so this asserts a launcher invariant nothing else
    # checks (M4 invariants 20 / 17a; the "boundary cannot silently regress" role
    # doctor already plays for the floor).
    if state_dir_inside_workspace(state_dir, workspace):
        if os.environ.get("DEEPAGENTS_IN_CONTAINER") == "1":
            records.append((
                "error",
                f"state dir {state_dir} is INSIDE the workspace {workspace} — "
                "checkpoints.sqlite / past.sqlite / denials.jsonl are reachable by the "
                "agent's own file tools. Set DEEPAGENTS_STATE_DIR to a path outside the "
                "workspace mount (run-docker uses /project/state)."
            ))
        else:
            records.append((
                "info",
                f"state dir {state_dir} is inside the workspace — the documented bare-host "
                "layout (no container boundary to protect). In-container this is an error."
            ))
    else:
        records.append(("info", f"state dir {state_dir} is outside the workspace"))

    # --- Jail / seccomp (M4 slice H) -------------------------------------------
    # Only checked when the operator opted in: the jail is off by default (§13),
    # and doctor must not fail every run for a feature nobody enabled.
    from harness import jail as jail_mod
    from harness import seccomp as seccomp_mod

    if not jail_mod.jail_enabled():
        records.append(("info", "fs jail off (DEEPAGENTS_JAIL unset) — slice H checks skipped"))
    else:
        profile_path = seccomp_mod.profile_path()
        if not profile_path.is_file():
            records.append((
                "error",
                f"fs jail is on but the seccomp profile is missing at {profile_path} — "
                "run 'python3 -m harness seccomp-sync'"
            ))
        else:
            try:
                problems = seccomp_mod.verify_profile(seccomp_mod.load_profile(profile_path))
            except (OSError, ValueError) as exc:
                records.append(("error", f"seccomp profile unreadable: {exc}"))
            else:
                for problem in problems:
                    records.append(("error", f"seccomp profile: {problem}"))
                if not problems:
                    records.append((
                        "info",
                        "seccomp profile is Docker's default plus exactly "
                        f"{list(seccomp_mod.RELAXED_SYSCALLS)}",
                    ))

        # LSM gate (M4 slice J, §11.6). seccomp and AppArmor are independent, and a
        # correct seccomp profile says nothing about the second one: docker-default
        # denies `mount` outright, which is not something the jail can work around
        # from inside the namespace. Surfaced here so it is a pre-flight finding
        # naming the real cause, not a bwrap error the operator has to decode.
        confinement = jail_mod.apparmor_confinement()
        apparmor_opt = (os.environ.get("DEEPAGENTS_JAIL_APPARMOR") or "").strip()
        if confinement:
            records.append((
                "error",
                f"fs jail is on but this container is confined by AppArmor profile "
                f"'{confinement}', which denies the mounts bwrap needs (seccomp is not "
                "the problem). Relaunch with DEEPAGENTS_JAIL_APPARMOR=unconfined — which "
                "drops the whole profile, not just its deny-mount rule — or load the "
                "narrowed profile (milestone4.md §11.6, slice J).",
            ))
        elif apparmor_opt == "unconfined":
            # Not an error (the operator asked for it) but never silent: this is a
            # wider trade than the five relaxed syscalls DEEPAGENTS_JAIL alone costs.
            records.append((
                "warning",
                "fs jail: AppArmor is disabled for this container "
                "(DEEPAGENTS_JAIL_APPARMOR=unconfined). That drops all of docker-default "
                "— the /proc and /sys write denials and the ptrace peer restriction — "
                "not only its deny-mount rule.",
            ))
        else:
            records.append(("info", "fs jail: no AppArmor confinement in force"))

        # The real gate: bwrap being installed says nothing about whether seccomp
        # will actually let it unshare. Only meaningful in-container.
        if os.environ.get("DEEPAGENTS_IN_CONTAINER") == "1":
            jail_problems = jail_mod.preflight()
            for problem in jail_problems:
                records.append(("error", f"fs jail: {problem}"))
            if not jail_problems:
                records.append(("info", "fs jail: bwrap can create a user namespace here"))
        else:
            records.append((
                "info",
                "fs jail: userns probe skipped off-container (run it in the image)",
            ))

    # --- Summary ---------------------------------------------------------------
    errors = [r for r in records if r[0] == "error"]
    warnings = [r for r in records if r[0] == "warning"]
    info = [r for r in records if r[0] == "info"]

    print("[doctor] --- summary ---", file=sys.stderr)
    for level, msg in records:
        print(f"[doctor] [{level}] {msg}", file=sys.stderr)

    print(f"[doctor] {len(errors)} error(s), {len(warnings)} warning(s), {len(info)} info", file=sys.stderr)

    return 1 if errors else 0
