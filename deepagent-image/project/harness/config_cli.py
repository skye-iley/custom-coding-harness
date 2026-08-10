"""Milestone 5, C6/C7: `harness config` / `harness config security` -- the
keyless, pre-spinup wizard for knobs fixed at container start (mask mode,
jail/AppArmor, resource caps, NetJail) plus the model/HITL-preset questions
that could also be set via a CLI flag or the in-session `/config`. Writes
through `config.py`'s `save_profile` -- the same writer `/config save` uses,
so there is one writer, not two (milestone5.md §4 C6/C7).

`harness config security` is the tail of the same wizard with the model/HITL
screens skipped, not a separate implementation ("already the same program, a
narrower entry point" -- milestone5.md §4 C7).

Deliberately dependency-light like config.py itself: `harness.providers` is
imported (stdlib + harness.cost only, no langchain -- see its module
docstring) for the model menu and API-key detection, but nothing here imports
`harness.cli` (which pulls the full deepagents/langgraph/langchain stack) or
calls `providers.resolve_chat_model` -- that only runs when an actual agent
run starts, not at wizard-build time.

Deviation from the fuller spec sketch (milestone5_spec.md §9): the
interactive menus here are plain numbered `input()` choices, not an arrow-key
`prompt_toolkit` dialog. That keeps this module free of any prompt_toolkit
dependency and the wizard fully deterministic/host-testable via a stubbed
`input`; an arrow-key upgrade is a pure UI enhancement that can layer on top
later without changing what gets written.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from harness.config import (
    CONFIG_NAME,
    PROFILE_FIELDS,
    PROFILE_NAME,
    Settings,
    SettingsSources,
    load_profile,
    resolve_settings,
    save_profile,
)
from harness.providers import PROVIDERS

# --- pure formatting / parsing (host-testable, no I/O) ------------------------

_DISPLAY_LIVE_FIELDS = ("model", "thread_id", "topic", "max_cost", "max_tokens")
_DISPLAY_PRESPINUP_FIELDS = (
    "headless", "mask_enabled", "mask_mode", "jail",
    "jail_apparmor", "cpus", "memory", "pids_limit", "net_jail",
)


def format_settings_lines(settings: Settings, sources: SettingsSources) -> list[str]:
    """The lines `harness config show` prints: every `Settings` field,
    source-tagged, live fields first then pre-spinup. Pure -- no I/O -- so
    it's testable without touching the filesystem or a terminal."""

    def fmt(name: str, value, source: str) -> str:
        shown = "(unset)" if value in (None, "") else value
        return f"{name:<16} = {str(shown):<28} ({source})"

    lines = [fmt(f, getattr(settings, f), getattr(sources, f)) for f in _DISPLAY_LIVE_FIELDS]
    if settings.hitl is None:
        lines.append(fmt("hitl", "off", sources.hitl))
    else:
        for f in ("autonomy_level", "on_deny", "interruption_policy"):
            lines.append(fmt(f"hitl.{f}", getattr(settings.hitl, f), sources.hitl))
    lines.append("--- pre-spinup (fixed at container start) ---")
    lines.extend(fmt(f, getattr(settings, f), getattr(sources, f)) for f in _DISPLAY_PRESPINUP_FIELDS)
    return lines


# --- numbered-choice prompt primitives ----------------------------------------


def _numbered_choice(header: str, options: list[str], default_index: int = 0) -> str:
    """Print `header` then a numbered `options` list; read a choice via
    `input()`. Blank input picks `default_index`. Loops on invalid input."""
    print(header)
    for i, opt in enumerate(options, 1):
        marker = "*" if i - 1 == default_index else " "
        print(f"  {marker}{i}) {opt}")
    while True:
        raw = input(f"choice [{default_index + 1}]: ").strip()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print(f"enter a number 1-{len(options)}")


def _confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


# --- .agentignore quick-edit (harness config security) -----------------------
#
# Convenience wrappers only -- the file's format/location is unchanged and
# M4-owned (mask.py). These append to the WORKSPACE's .agentignore, not the
# profile (milestone5_spec.md §9).


def _agentignore_path(workspace: Path) -> Path:
    return workspace / ".agentignore"


def agentignore_add_pattern(workspace: Path, pattern: str) -> Path:
    """Append `pattern` as a plain (non-floor) masked path. Returns the file path."""
    path = _agentignore_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{sep}{pattern}\n")
    return path


def netjail_dir() -> Path:
    """The repo's netjail/ dir, resolved relative to this file (a sibling of
    project/, one level up from harness/) -- valid when `harness config` runs
    on the host (the intended usage: pre-spinup, before docker run) against a
    checked-out repo. Not present inside a running container (netjail/ is
    host-only config, never COPYed into the image), so callers must handle a
    missing directory gracefully rather than assume it exists."""
    return Path(__file__).resolve().parent.parent.parent / "netjail"


def netjail_list_entries(path: Path) -> list[str]:
    """Non-comment, non-blank lines, in file order. `[]` if the file is absent."""
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def netjail_add_entry(path: Path, entry: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{sep}{entry}\n")


def netjail_remove_entry(path: Path, index: int) -> str | None:
    """Remove the `index`-th (0-based) non-comment, non-blank entry, leaving
    comments/blank lines elsewhere untouched. Returns the removed entry's
    text, or `None` if `index` is out of range or the file doesn't exist."""
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    count = -1
    removed = None
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
            if count == index:
                removed = stripped
                continue  # drop this line, keep everything else
        out.append(line)
    if removed is not None:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return removed


def agentignore_add_floor(workspace: Path, pattern: str) -> Path:
    """Append `pattern` inside the `#!floor: ... #!floor-end` block, creating
    the block (at EOF) if the file has none yet. Returns the file path."""
    path = _agentignore_path(workspace)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!floor:\n{pattern}\n#!floor-end\n", encoding="utf-8")
        return path

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_floor = False
    saw_floor = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#!floor:"):
            in_floor = True
            saw_floor = True
            out.append(line)
            continue
        if in_floor and stripped.startswith("#!"):
            out.append(pattern)
            out.append(line)
            inserted = True
            in_floor = False
            continue
        out.append(line)
    if not saw_floor:
        out.append("#!floor:")
        out.append(pattern)
        out.append("#!floor-end")
    elif not inserted:
        # Floor block opened but never closed (malformed file) -- append at EOF,
        # still inside the open block rather than silently dropping the entry.
        out.append(pattern)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


# --- HITL preset write path ----------------------------------------------------
#
# .harness-config.yaml has no writer elsewhere (M3 never needed one -- it was
# always hand-copied from the .example template). This is the wizard's only
# write path to that file; only autonomy_level is touched, so a hand-edited
# review_triggers/on_deny/system_interrupts block survives untouched.


def write_hitl_preset(path: Path, autonomy_level: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"autonomy_level: {autonomy_level}\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    replaced = False
    for line in lines:
        if line.strip().startswith("autonomy_level:"):
            out.append(f"autonomy_level: {autonomy_level}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.insert(0, f"autonomy_level: {autonomy_level}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# --- wizard steps --------------------------------------------------------------


def _wizard_model_step() -> str | None:
    detected = [p for p in PROVIDERS if p.default_model and os.getenv(p.api_key_env)]
    if not detected:
        print("Model: no provider API key detected in the environment -- keeping auto-select.")
        return None
    options = [p.default_model for p in detected] + ["(keep current / auto-select)"]
    keys = ", ".join(p.api_key_env for p in detected)
    choice = _numbered_choice(f"Model -- pick a provider (keys detected: {keys}):", options, default_index=len(options) - 1)
    return None if choice == options[-1] else choice


def _wizard_security_step() -> dict:
    values: dict = {}
    posture = _numbered_choice(
        "Security posture:",
        [
            "default (mask on, jail off)",
            "hardened (mask on, jail on -- requires the seccomp profile; see `harness doctor`)",
            "custom (answer each knob)",
        ],
        default_index=0,
    )
    if posture.startswith("hardened"):
        values["jail"] = True
    elif posture.startswith("custom"):
        values["mask_mode"] = _numbered_choice("Mask mode:", ["deny", "allow"], default_index=0)
        values["jail"] = _numbered_choice("Jail:", ["off", "on"], default_index=0) == "on"
        apparmor = input("AppArmor profile (blank = auto): ").strip()
        if apparmor:
            values["jail_apparmor"] = apparmor
        values["cpus"] = input("CPU limit [2]: ").strip() or "2"
        values["memory"] = input("Memory limit [4g]: ").strip() or "4g"
        values["pids_limit"] = input("PIDs limit [512]: ").strip() or "512"
        values["net_jail"] = _confirm("Enable NetJail (deny-all egress + allowlist)?", default=False)
    # "default" adds nothing -- an empty profile diff for this step, matching
    # "matches current .env" (milestone5_spec.md §9's wizard mockup).
    return values


def _wizard_hitl_step() -> str | None:
    preset = _numbered_choice(
        "HITL preset:",
        [
            "off (no .harness-config.yaml)",
            "guided (approve PR + flagged tool calls)",
            "strict (approve everything)",
        ],
        default_index=0,
    )
    if preset.startswith("off"):
        return None
    return "guided" if preset.startswith("guided") else "strict"


def _wizard_agentignore_step() -> None:
    workspace = Path(os.getenv("AGENT_WORKSPACE") or (Path.cwd() / "workspace"))
    while True:
        action = _numbered_choice(
            ".agentignore quick-edit:",
            ["skip", "add a path to mask", "add a floor entry (can never be unmasked)"],
            default_index=0,
        )
        if action == "skip":
            return
        pattern = input("pattern: ").strip()
        if pattern:
            if action.startswith("add a path"):
                path = agentignore_add_pattern(workspace, pattern)
                print(f"[harness] added {pattern!r} to {path}")
            else:
                path = agentignore_add_floor(workspace, pattern)
                print(f"[harness] added {pattern!r} to the floor block in {path}")
        if not _confirm("Add another?", default=False):
            return


def _wizard_netjail_step() -> None:
    net_dir = netjail_dir()
    if not net_dir.is_dir():
        print(f"[harness] NetJail directory not found at {net_dir} -- skipping (host-side only; "
              "run `harness config security` from a checked-out repo, not inside a container).")
        return
    files = {
        "host-services.txt (host port forwarders)": "host-services.txt",
        "allowed-domains.txt (egress domains)": "allowed-domains.txt",
    }
    while True:
        which = _numbered_choice("NetJail allowlists:", ["done", *files], default_index=0)
        if which == "done":
            return
        fname = files[which]
        path = net_dir / fname
        entries = netjail_list_entries(path)
        print(f"\n{fname}:")
        if not entries:
            print("  (empty)")
        for i, e in enumerate(entries, 1):
            print(f"  {i}) {e}")
        action = _numbered_choice("Action:", ["back", "add", "delete"], default_index=0)
        if action == "back":
            continue
        if action == "add":
            hint = "name port, e.g. ollama 11434" if fname == "host-services.txt" else "domain, e.g. api.github.com"
            entry = input(f"new entry ({hint}): ").strip()
            if entry:
                netjail_add_entry(path, entry)
                print(f"[harness] added {entry!r} to {fname}")
        elif action == "delete":
            if not entries:
                print("[harness] nothing to delete.")
                continue
            raw = input(f"entry number to delete [1-{len(entries)}]: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(entries):
                removed = netjail_remove_entry(path, int(raw) - 1)
                print(f"[harness] removed {removed!r} from {fname}")
            else:
                print(f"[harness] invalid entry number {raw!r}.")


def _run_wizard(*, security_only: bool, auto_save: bool) -> int:
    if not sys.stdin.isatty():
        print(
            "[harness] `harness config` needs an interactive terminal -- use "
            "`harness config set <field> <value>` for a non-interactive one-shot."
        )
        return 1

    values: dict = {}
    if not security_only:
        model = _wizard_model_step()
        if model is not None:
            values["model"] = model
    values.update(_wizard_security_step())

    hitl_choice = None
    if not security_only:
        hitl_choice = _wizard_hitl_step()
    else:
        _wizard_agentignore_step()
        _wizard_netjail_step()

    print()
    print("Summary:")
    for k, v in values.items():
        print(f"  {k}: {v}")
    if hitl_choice is not None:
        print(f"  hitl.autonomy_level: {hitl_choice}")
    elif not security_only:
        print("  hitl: off")
    if not values and hitl_choice is None:
        print("  (no changes)")
        return 0

    if not (auto_save or _confirm(f"Save to {PROFILE_NAME}?", default=True)):
        print("[harness] not saved.")
        return 0

    if values:
        save_profile(Path.cwd() / PROFILE_NAME, values)
    if hitl_choice is not None:
        write_hitl_preset(Path.cwd() / CONFIG_NAME, hitl_choice)
    print("[harness] saved.")
    return 0


# --- one-shot show / set -------------------------------------------------------


def _cmd_show() -> int:
    settings, sources = resolve_settings()
    for line in format_settings_lines(settings, sources):
        print(line)
    return 0


def _cmd_set(field: str, value: str) -> int:
    if field not in PROFILE_FIELDS:
        print(f"[harness] unknown/unsettable field {field!r} (settable: {', '.join(sorted(PROFILE_FIELDS))})")
        return 1
    profile_path = Path.cwd() / PROFILE_NAME
    before = profile_path.read_text(encoding="utf-8") if profile_path.exists() else None
    save_profile(profile_path, {field: value})
    try:
        load_profile(profile_path)  # re-validate the merged file round-trips cleanly
    except SystemExit as exc:
        if before is None:
            profile_path.unlink(missing_ok=True)
        else:
            profile_path.write_text(before, encoding="utf-8")
        print(f"[harness] {exc}")
        return 1
    print(f"[harness] wrote {PROFILE_NAME}: {field}={value}")
    return 0


# --- entry ----------------------------------------------------------------


def config_main(argv: list[str]) -> int:
    """`harness config [show|set <field> <value>|security] [--save]`.

    Bare (no subcommand): full interactive wizard (model + security posture +
    HITL preset), prompting to save. `--save` skips the confirmation.
    `security`: the same wizard with the model/HITL screens skipped, plus the
    `.agentignore` quick-edit -- "already the same program, a narrower entry
    point" (milestone5.md §4 C7).
    """
    if argv and argv[0] == "show":
        return _cmd_show()
    if argv and argv[0] == "set":
        if len(argv) != 3:
            print("[harness] usage: harness config set <field> <value>")
            return 1
        return _cmd_set(argv[1], argv[2])
    if argv and argv[0] == "security":
        return _run_wizard(security_only=True, auto_save="--save" in argv[1:])
    return _run_wizard(security_only=False, auto_save="--save" in argv)
