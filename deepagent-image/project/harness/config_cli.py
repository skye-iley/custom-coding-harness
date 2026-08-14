"""Milestone 5, C6/C7: `harness config` / `harness config security` -- the
keyless, pre-spinup wizard for knobs fixed at container start (mask mode,
jail/AppArmor, resource caps, NetJail) plus the model/HITL-preset questions
that could also be set via a CLI flag or the in-session `/config`. Writes
through `config.py`'s `save_profile` -- the same writer `/config save` uses,
so there is one writer, not two (milestone5.md §4 C6/C7).

`harness config security` is the tail of the same wizard with the model/HITL
screens skipped, not a separate implementation ("already the same program, a
narrower entry point" -- milestone5.md §4 C7).

This module adds **no langchain/deepagents dependency of its own** -- which is
the reason it is a separate module from `cli.py`. `harness.providers` is
imported (stdlib + harness.cost only, no langchain -- see its module
docstring) for the model menu and API-key detection; nothing here imports
`harness.cli` or calls `providers.resolve_chat_model`, which only runs when an
actual agent run starts.

This now holds through the package too, not just for this file. It previously
did not: `harness/__init__.py` did an unconditional `from harness.cli import
main`, so importing `harness.config_cli` still loaded `cli.py` and therefore
langgraph, and `dispatch` lived in `cli.py`, so routing to the wizard imported
the very module the split existed to avoid. Both are fixed (milestone5.md §0.1
F6): `__init__.py` resolves `main` through a lazy `__getattr__`, and the routes
live in the stdlib-only `harness/entry.py`. `tests/test_import_isolation.py`
guards it.

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
    WIZARD_PRESPINUP_SPECS,
    Settings,
    SettingsSources,
    _to_bool,
    format_config_lines,
    load_profile,
    resolve_settings,
    save_profile,
)
from harness.providers import PROVIDERS, provider_available

# --- pure formatting / parsing (host-testable, no I/O) ------------------------

def format_settings_lines(settings: Settings, sources: SettingsSources) -> list[str]:
    """The lines `harness config show` prints: every `Settings` field,
    source-tagged, live fields first then pre-spinup.

    A thin wrapper over the one registry-driven renderer (milestone5.1.md §4
    R3) -- this used to be a second implementation of `cli._config_display_lines`
    at a different width, and the two drifted independently."""
    return format_config_lines(settings, sources)


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


def agentignore_effective_mode(workspace: Path) -> str:
    """The visibility mode a quick-edit to this workspace's `.agentignore` lands in.

    Prefers the file's own `#!mode:` header (M4 owns that grammar, so this reads
    it through `mask._parse_agentignore` rather than reimplementing it), and falls
    back to the resolved `mask_mode` setting when the file has no header. A
    malformed file degrades to the resolved setting instead of taking the wizard
    down with a `SystemExit` -- the mask scan will still reject it loudly at
    launch, which is where that error belongs.
    """
    path = _agentignore_path(workspace)
    if path.is_file():
        try:
            from harness import mask  # stdlib-only, same dependency tier as this module

            parsed = mask._parse_agentignore(path.read_text(encoding="utf-8"), str(workspace))
            header = parsed["directives"].get("mode")
            if header:
                return header
        except Exception:  # noqa: BLE001 - malformed/unreadable file, fall through
            pass
    try:
        settings, _ = resolve_settings()
        return settings.mask_mode
    except SystemExit:
        return "deny"


def netjail_dir() -> Path:
    """The repo's netjail/ dir, resolved relative to this file (a sibling of
    project/, one level up from harness/) -- valid when `harness config` runs
    on the host (the intended usage: pre-spinup, before docker run) against a
    checked-out repo. Not present inside a running container (netjail/ is
    host-only config, never COPYed into the image), so callers must handle a
    missing directory gracefully rather than assume it exists."""
    return Path(__file__).resolve().parent.parent.parent / "netjail"


def netjail_template_path(path: Path) -> Path:
    """The tracked `.example` template beside a live allowlist file."""
    return path.with_name(path.name + ".example")


def netjail_read_path(path: Path) -> Path:
    """The file a *reader* should consult: the live allowlist if it exists, else
    the tracked `.example` template.

    The live files are gitignored so an operator's edits (or a test's) can never
    pollute a clone, but the shipped defaults still have to work on a fresh
    checkout with nothing copied — so a read falls through to the template.
    Reading must not create anything: `smoke` reads these lists and may not
    write into the repo tree. Materializing the live file is `netjail_seed`'s
    job, and only a write path calls it. The launchers do the same resolution in
    shell (`run-docker`/`smoke` NetJail blocks) — keep the three in step."""
    return path if path.is_file() else netjail_template_path(path)


def netjail_seed(path: Path) -> Path:
    """Materialize the gitignored live allowlist from its tracked template.

    Called before a write so an edit lands on the local file rather than the
    committed one, and so the operator inherits the template's comments and
    commented-out examples instead of a bare line in an empty file. A no-op once
    the live file exists; silently leaves `path` absent if no template is there
    either (the caller's append then creates it, same as before)."""
    if not path.is_file():
        template = netjail_template_path(path)
        if template.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def netjail_list_entries(path: Path) -> list[str]:
    """Non-comment, non-blank lines, in file order. `[]` if neither the live file
    nor its `.example` template is present."""
    path = netjail_read_path(path)
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def netjail_add_entry(path: Path, entry: str) -> None:
    netjail_seed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    sep = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{sep}{entry}\n")


def netjail_remove_entry(path: Path, index: int) -> str | None:
    """Remove the `index`-th (0-based) non-comment, non-blank entry, leaving
    comments/blank lines elsewhere untouched. Returns the removed entry's
    text, or `None` if `index` is out of range or the file doesn't exist.

    Reads through `netjail_read_path` and writes to the live file, so deleting
    one of the shipped defaults lands on the local copy and leaves the committed
    template alone — the index the caller passes came from
    `netjail_list_entries`, which may have listed the TEMPLATE's entries, so
    reading the live file only would silently delete nothing.

    A miss stays a pure no-op: nothing is materialized unless a line actually
    came out. Creating the live file on an out-of-range index would be a silent
    one-way door — once it exists it fully replaces the template, so the operator
    would stop inheriting later additions to the shipped defaults."""
    source = netjail_read_path(path)
    if not source.is_file():
        return None
    lines = source.read_text(encoding="utf-8").splitlines()
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
        path.parent.mkdir(parents=True, exist_ok=True)
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


def disable_hitl(path: Path) -> Path | None:
    """Turn HITL off by moving `.harness-config.yaml` aside to
    `<name>.disabled`. Returns the new path, or `None` if there was no file to
    disable.

    Renamed, never deleted: presence-of-file *is* the on/off switch (M3), so
    turning HITL off means the file must not be there -- but it may carry a
    hand-written `review_triggers` block the wizard can't reconstruct, and a
    config wizard has no business destroying that. Moving it aside makes the
    choice reversible with one `mv`.
    """
    if not path.is_file():
        return None
    target = path.with_name(path.name + ".disabled")
    if target.exists():
        target.unlink()
    path.replace(target)
    return target


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


def _expected_cwd() -> Path:
    """`project/` -- the dir holding `providers/`, `.env`, and the two config
    files. `harness/config_cli.py` -> parent.parent."""
    return Path(__file__).resolve().parent.parent


def _refuse_wrong_cwd() -> bool:
    """True (and prints why) when the cwd can't be the one `run-docker` reads.

    Every write path here targets `Path.cwd() / PROFILE_NAME` -- the documented
    contract `cli.main`/`resolve_settings` rely on. Run from the repo root that
    resolves to a `.harness-profile.yaml` `run-docker` never mounts, so the
    operator gets a saved profile that silently does nothing. Detected by the
    `providers/` directory, which only the right cwd has. Refusing beats
    silently re-anchoring the path: the "reads Path.cwd()" contract stays true.
    """
    if (Path.cwd() / "providers").is_dir():
        return False
    print(
        f"[harness] refusing to write from {Path.cwd()}: this is not the harness project "
        f"directory, so the {PROFILE_NAME} written here is not the one run-docker mounts. "
        f"cd {_expected_cwd()} and re-run."
    )
    return True


# --- wizard steps --------------------------------------------------------------


def _wizard_model_step() -> str | None:
    # Same availability rule choose_model uses, so the wizard offers exactly what
    # an unflagged run would actually pick -- keyless local providers included.
    detected = [p for p in PROVIDERS if p.default_model and provider_available(p)]
    if not detected:
        print("Model: no provider API key detected in the environment -- keeping auto-select.")
        return None
    options = [p.default_model for p in detected] + ["(keep current / auto-select)"]
    keys = ", ".join(
        p.api_key_env if p.requires_key else f"{p.prefix.rstrip(':')} (keyless)"
        for p in detected
    )
    choice = _numbered_choice(f"Model -- pick a provider (keys detected: {keys}):", options, default_index=len(options) - 1)
    return None if choice == options[-1] else choice


def _ask_field(spec) -> object:
    """One wizard question, rendered from the field's own spec (milestone5.1.md
    §4 R5). Three shapes, all derived rather than hand-written per knob:

    - ``wizard="confirm"``  -> a y/N question.
    - enum (``choices``) or bool (``_to_bool`` cast) -> a numbered menu; the
      answer goes back through the field's own cast, so ``"on"``/``"off"``
      become real booleans by the same ``_TRUTHY`` rule every other tier uses.
    - anything else -> a text prompt carrying its default in brackets, or
      ``(blank = auto)`` where the default is "unset".

    Returns the field's default on blank input, which for a ``default=None``
    field means "not written" -- the caller drops it.
    """
    if spec.wizard == "confirm":
        return _confirm(spec.label, default=bool(spec.default))
    options = spec.choices or (("off", "on") if spec.cast is _to_bool else None)
    if options is not None:
        default_index = 0
        current = spec.default
        for i, opt in enumerate(options):
            if spec.cast(opt) == current:
                default_index = i
                break
        return spec.cast(_numbered_choice(f"{spec.label}:", list(options), default_index=default_index))
    hint = f"[{spec.default}]" if spec.default is not None else "(blank = auto)"
    raw = input(f"{spec.label} {hint}: ").strip()
    return spec.cast(raw) if raw else spec.default


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
    # The postures stay hand-written on purpose: they are opinions about
    # *combinations* of knobs, which is exactly what a per-field registry can't
    # express and shouldn't try to (milestone5.1.md §4 R5).
    if posture.startswith("default"):
        # Write the values explicitly rather than returning an empty diff: an
        # empty diff leaves a previously-saved `jail: true` in place, so the
        # wizard would report "mask on, jail off" and the next run would come up
        # jailed. Picking a posture must mean the posture you picked.
        values["mask_mode"] = "deny"
        values["jail"] = False
    elif posture.startswith("hardened"):
        values["mask_mode"] = "deny"
        values["jail"] = True
    elif posture.startswith("custom"):
        # Every persisted pre-spinup knob, in registry order. A new one appears
        # here for free -- no edit to this function.
        for spec in WIZARD_PRESPINUP_SPECS:
            answer = _ask_field(spec)
            if answer is not None:
                values[spec.name] = answer
    return values


def _wizard_hitl_step() -> str:
    """Returns "off" | "guided" | "strict" -- "off" is a real answer the caller
    must act on (move any existing .harness-config.yaml aside), not the absence
    of one. `_run_wizard` uses None for "this screen was skipped"."""
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
        return "off"
    return "guided" if preset.startswith("guided") else "strict"


def _wizard_agentignore_step() -> list[str]:
    """`.agentignore` quick-edit loop. Returns the edits actually applied (each
    already written to disk when this returns), so the caller's summary can say
    so -- these writes are NOT covered by the profile save confirmation.

    Mode-aware, because a bare pattern means opposite things in the two modes: in
    deny mode it masks a path, in **allow** mode it IS the allow-list entry, so
    "add a path to mask" would make a secret *visible*. The step names the
    inversion and relabels the option rather than silently rerouting the operator
    to the floor -- promoting an ordinary mask to a permanent, un-negatable floor
    entry behind their back is its own surprise.
    """
    workspace = Path(os.getenv("AGENT_WORKSPACE") or (Path.cwd() / "workspace"))
    allow_mode = agentignore_effective_mode(workspace) == "allow"
    applied: list[str] = []
    if allow_mode:
        print(
            "[harness] note: .agentignore is in allow mode - a plain pattern makes a path "
            "VISIBLE, not masked. To hide something here, add it to the floor block (option 3)."
        )
    plain_option = (
        "add a path to ALLOW (this workspace is in allow mode)"
        if allow_mode
        else "add a path to mask"
    )
    while True:
        action = _numbered_choice(
            ".agentignore quick-edit:",
            ["skip", plain_option, "add a floor entry (can never be unmasked)"],
            default_index=0,
        )
        if action == "skip":
            return applied
        pattern = input("pattern: ").strip()
        if pattern:
            if action == plain_option:
                path = agentignore_add_pattern(workspace, pattern)
                verb = "allow-listed" if allow_mode else "added"
                print(f"[harness] {verb} {pattern!r} in {path}")
                applied.append(f".agentignore: {verb} {pattern!r} ({path})")
            else:
                path = agentignore_add_floor(workspace, pattern)
                print(f"[harness] added {pattern!r} to the floor block in {path}")
                applied.append(f".agentignore: added {pattern!r} to the floor block ({path})")
        if not _confirm("Add another?", default=False):
            return applied


def _wizard_netjail_step() -> list[str]:
    """NetJail allowlist editor. Returns the edits actually applied (written to
    disk as soon as the operator answers), for the same reason
    `_wizard_agentignore_step` does: the profile save confirmation does not cover
    them, so the summary has to name them."""
    net_dir = netjail_dir()
    applied: list[str] = []
    if not net_dir.is_dir():
        print(f"[harness] NetJail directory not found at {net_dir} -- skipping (host-side only; "
              "run `harness config security` from a checked-out repo, not inside a container).")
        return applied
    files = {
        "host-services.txt (host port forwarders)": "host-services.txt",
        "allowed-domains.txt (egress domains)": "allowed-domains.txt",
    }
    while True:
        which = _numbered_choice("NetJail allowlists:", ["done", *files], default_index=0)
        if which == "done":
            return applied
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
                applied.append(f"NetJail: added {entry!r} to {fname}")
        elif action == "delete":
            if not entries:
                print("[harness] nothing to delete.")
                continue
            raw = input(f"entry number to delete [1-{len(entries)}]: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(entries):
                removed = netjail_remove_entry(path, int(raw) - 1)
                print(f"[harness] removed {removed!r} from {fname}")
                applied.append(f"NetJail: removed {removed!r} from {fname}")
            else:
                print(f"[harness] invalid entry number {raw!r}.")


def _run_wizard(*, security_only: bool, auto_save: bool) -> int:
    if not sys.stdin.isatty():
        print(
            "[harness] `harness config` needs an interactive terminal -- use "
            "`harness config set <field> <value>` for a non-interactive one-shot."
        )
        return 1
    if _refuse_wrong_cwd():
        return 1

    values: dict = {}
    if not security_only:
        model = _wizard_model_step()
        if model is not None:
            values["model"] = model
    values.update(_wizard_security_step())

    hitl_choice = None
    # `.agentignore` / NetJail edits write to disk the moment the operator answers
    # -- they are not part of the profile `values` the save confirmation covers.
    # Collected here so the summary and the decline path can say so out loud
    # (restructuring them into a deferred-write transaction would mean two writers
    # per file; naming them is the honest, smaller fix).
    applied: list[str] = []
    if not security_only:
        hitl_choice = _wizard_hitl_step()
    else:
        applied += _wizard_agentignore_step()
        applied += _wizard_netjail_step()

    # `hitl_choice is None` means the screen was skipped (security-only run);
    # "off" is an explicit answer that has to be acted on.
    hitl_path = Path.cwd() / CONFIG_NAME
    print()
    print("Summary:")
    for k, v in values.items():
        print(f"  {k}: {v}")
    if hitl_choice == "off":
        print(f"  hitl: off{' (moves the existing .harness-config.yaml aside)' if hitl_path.is_file() else ''}")
    elif hitl_choice is not None:
        print(f"  hitl.autonomy_level: {hitl_choice}")
    if applied:
        print("Already applied (not part of the profile save):")
        for line in applied:
            print(f"  {line}")
    if not values and hitl_choice is None:
        if not applied:
            print("  (no changes)")
        return 0

    if not (auto_save or _confirm(f"Save to {PROFILE_NAME}?", default=True)):
        if applied:
            print("[harness] profile not saved. (The .agentignore / NetJail edits listed "
                  "above were applied immediately.)")
        else:
            print("[harness] not saved.")
        return 0

    if values:
        save_profile(Path.cwd() / PROFILE_NAME, values)
    if hitl_choice == "off":
        moved = disable_hitl(hitl_path)
        if moved is not None:
            print(f"[harness] HITL off: moved {CONFIG_NAME} to {moved.name} (rename it back to re-enable).")
    elif hitl_choice is not None:
        write_hitl_preset(hitl_path, hitl_choice)
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
    if _refuse_wrong_cwd():
        return 1
    profile_path = Path.cwd() / PROFILE_NAME
    before = profile_path.read_text(encoding="utf-8") if profile_path.exists() else None
    try:
        # `save_profile` rejects an out-of-`choices` value up front (nothing hits
        # disk); `load_profile` then re-validates the merged file round-trips
        # cleanly, which is what catches a bad cast. Both raise SystemExit, so
        # one handler covers both and the rollback below is correct either way.
        save_profile(profile_path, {field: value})
        load_profile(profile_path)
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
