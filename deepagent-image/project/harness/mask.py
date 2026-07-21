"""Workspace visibility policy: gitignore-parity resolver, 3-tier policy, floor enforcement.

Pure stdlib, no pip dep. ``resolve(workspace, state_dir, mode) -> MaskResult``
implements the §10 resolver algorithm (milestone4.md):

  1. Assemble ordered rule set (pattern defaults -> state agentignore -> in-workspace
     .agentignore files -> designated-secret floor).
  2. Compile each pattern into a regex (gitwildmatch semantics: ``**``, ``!``, ``/``
     anchor, trailing ``/`` dir-only).
  3. Walk the workspace, last-match-wins, enforcing the floor invariants.
  4. Canonicalize paths, detect symlink escapes.
  5. Snapshot + protection-reduction check.
  6. Minimize for docker-overlay emission.

Imports no harness sibling (mirrors archive.py/cost.py acyclic discipline).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- shipped pattern-default globs (feature plan §2, milestone4.md §10) --------

PATTERN_DEFAULTS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    ".ssh/",
    ".aws/credentials",
    ".netrc",
    ".npmrc",
    ".git-credentials",
    "credentials.json",
    "*.p12",
    "*.pfx",
)

# --- public types -------------------------------------------------------------

MODE_DENY = "deny"
MODE_ALLOW = "allow"
MODES = frozenset({MODE_DENY, MODE_ALLOW})

TIER_FLOOR = "floor"
TIER_DEFAULT = "default"
TIER_USER = "user"
TIERS = frozenset({TIER_FLOOR, TIER_DEFAULT, TIER_USER})

MODE_MASK = "mask"
MODE_HIDE = "hide"
VISIBILITIES = frozenset({MODE_MASK, MODE_HIDE})

# Config-file name (§16: .agentignore)
AGENTIGNORE_NAME = ".agentignore"


@dataclass(frozen=True)
class MaskEntry:
    relpath: str
    type: str  # "file" | "dir"
    tier: str  # TIER_FLOOR | TIER_DEFAULT | TIER_USER
    mode: str = MODE_MASK  # mask (v1 only; hide is deferred v2)


@dataclass
class MaskResult:
    masked: list[MaskEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mode: str = MODE_DENY
    mode_source: str = "default"  # "default" | "header" | "env"
    snapshot_changed: bool = False
    protection_reduced: bool = False
    reduced_paths: list[str] = field(default_factory=list)


# --- gitwildmatch → regex compiler --------------------------------------------

def _compile_pattern(pattern: str, base_dir: str) -> dict:
    """Compile one gitignore pattern line into a matcher dict.

    Returns {regex, negated, dir_only, anchored, base_dir}.
    Handles ``**`` (match any depth), ``*`` (match within segment),
    ``?`` (one non-/ char), ``[...]`` char classes, ``!`` negation prefix,
    trailing ``/`` dir-only, leading ``/`` anchor.
    """
    raw = pattern
    negated = raw.startswith("!")
    if negated:
        raw = raw[1:]
    dir_only = raw.endswith("/")
    if dir_only:
        raw = raw.rstrip("/")
    anchored = raw.startswith("/")
    if anchored:
        raw = raw.lstrip("/")

    parts = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "*" and i + 1 < len(raw) and raw[i + 1] == "*":
            parts.append("(?:.*/)?")
            i += 2
            if i < len(raw) and raw[i] == "/":
                i += 1
        elif ch == "*":
            parts.append("[^/]*")
            i += 1
        elif ch == "?":
            parts.append("[^/]")
            i += 1
        elif ch == "[" and i + 1 < len(raw):
            j = i + 1
            if j < len(raw) and raw[j] in ("!", "^"):
                j += 1
            if j < len(raw) and raw[j] == "]":
                j += 1
            while j < len(raw) and raw[j] != "]":
                j += 1
            if j < len(raw) and raw[j] == "]":
                parts.append(raw[i:j + 1])
                i = j + 1
            else:
                parts.append(re.escape(ch))
                i += 1
        elif ch in ".^$+{}()|\\":
            parts.append("\\" + ch)
            i += 1
        else:
            parts.append(re.escape(ch))
            i += 1

    if anchored:
        regex_str = "^(?:" + "".join(parts) + ")$"
    else:
        regex_str = "(?:^|/)(?:" + "".join(parts) + ")$"

    return {
        "regex": re.compile(regex_str),
        "negated": negated,
        "dir_only": dir_only,
        "anchored": anchored,
        "base_dir": base_dir,
    }


# --- workspace walker ---------------------------------------------------------

def _walk_workspace(workspace: Path) -> list[dict]:
    """Walk the workspace (no symlinks) and return file/dir entries.

    Returns list of {relpath, is_dir, is_symlink, realpath}.
    Canonicalizes each entry so symlink escapes can be detected.
    """
    entries = []
    workspace_str = str(workspace.resolve())

    for dirpath, dirnames, filenames in os.walk(str(workspace), followlinks=False):
        rel_dir = os.path.relpath(dirpath, workspace_str)
        if rel_dir == ".":
            rel_dir = ""

        # Check for symlink dirs that escape
        real_dirpath = os.path.realpath(dirpath)
        if not real_dirpath.startswith(workspace_str + os.sep) and real_dirpath != workspace_str:
            rel_name = os.path.basename(dirpath)
            parent_rel = os.path.relpath(os.path.dirname(dirpath), workspace_str)
            rel = os.path.join(parent_rel, rel_name) if parent_rel != "." else rel_name
            entries.append({
                "relpath": rel.replace(os.sep, "/"),
                "is_dir": True,
                "is_symlink": True,
                "realpath": real_dirpath,
            })
            dirnames[:] = []
            continue

        for name in dirnames:
            fpath = os.path.join(dirpath, name)
            real = os.path.realpath(fpath)
            rel = os.path.join(rel_dir, name) if rel_dir else name
            is_slink = os.path.islink(fpath)
            entries.append({
                "relpath": rel.replace(os.sep, "/"),
                "is_dir": True,
                "is_symlink": is_slink,
                "realpath": real if is_slink else "",
            })
            if is_slink and not real.startswith(workspace_str + os.sep) and real != workspace_str:
                dirnames.remove(name)

        for name in filenames:
            fpath = os.path.join(dirpath, name)
            real = os.path.realpath(fpath)
            rel = os.path.join(rel_dir, name) if rel_dir else name
            entries.append({
                "relpath": rel.replace(os.sep, "/"),
                "is_dir": False,
                "is_symlink": os.path.islink(fpath),
                "realpath": real if os.path.islink(fpath) else "",
            })

    return entries


# --- .agentignore parser ------------------------------------------------------

def _parse_agentignore(text: str, base_dir: str) -> dict:
    """Parse an .agentignore text into header directives + compiled patterns.

    Returns {directives: dict, patterns: list[compiled_dict]}.
    """
    directives: dict[str, str] = {}
    patterns: list[dict] = []
    seen_pattern = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("#!"):
            if seen_pattern:
                raise SystemExit(
                    f"{base_dir}/.agentignore: directive after pattern line is invalid"
                )
            directive = stripped[2:]
            key, _, value = directive.partition(":")
            key = key.strip()
            value = value.strip()
            if key in ("mode", "visibility"):
                if key == "visibility" and value == MODE_HIDE:
                    raise SystemExit(
                        f"{base_dir}/.agentignore: visibility 'hide' is deferred (v2) — use 'mask'"
                    )
                directives[key] = value
            continue

        if stripped.startswith("#"):
            continue

        seen_pattern = True
        if stripped.startswith("!"):
            patterns.append(_compile_pattern(stripped, base_dir))
        else:
            patterns.append(_compile_pattern(stripped, base_dir))

    return {"directives": directives, "patterns": patterns}


def _collect_agentignore_files(workspace: Path) -> list[tuple[str, str, dict]]:
    """Collect all .agentignore files under workspace, root first.

    Returns [(file_path, dir_of_file, {directives, patterns})].
    """
    files = []
    workspace_str = str(workspace.resolve())
    root_ignore = workspace / AGENTIGNORE_NAME
    if root_ignore.is_file():
        parsed = _parse_agentignore(root_ignore.read_text(encoding="utf-8"), str(workspace))
        files.append((str(root_ignore), workspace_str, parsed))

    for dirpath, dirnames, _ in os.walk(str(workspace), followlinks=False):
        if AGENTIGNORE_NAME in dirnames:
            fpath = os.path.join(dirpath, AGENTIGNORE_NAME)
            parsed = _parse_agentignore(Path(fpath).read_text(encoding="utf-8"), dirpath)
            files.append((fpath, dirpath, parsed))
            dirnames.remove(AGENTIGNORE_NAME)

    return files


# --- state-dir authoritative config -------------------------------------------

def _read_state_agentignore(state_dir: Path) -> dict:
    """Read the authoritative config from state dir.

    Returns {patterns: list[compiled_dict], floor_paths: list[str]}.
    """
    path = state_dir / "agentignore"
    patterns: list[dict] = []
    floor_paths: list[str] = []
    in_floor = False

    if not path.is_file():
        return {"patterns": patterns, "floor_paths": floor_paths}

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#!floor:"):
            in_floor = True
            continue
        if stripped.startswith("#!"):
            in_floor = False
            continue
        if stripped.startswith("#"):
            continue
        if in_floor:
            floor_paths.append(stripped.strip())
        else:
            patterns.append(_compile_pattern(stripped, str(state_dir)))

    return {"patterns": patterns, "floor_paths": floor_paths}


# --- mask result helpers ------------------------------------------------------

def _read_snapshot(state_dir: Path) -> set[str]:
    """Read the previous snapshot: set of '<tier> <relpath>' lines."""
    path = state_dir / "mask-snapshot.txt"
    if not path.is_file():
        return set()
    return set(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _write_snapshot(state_dir: Path, entries: list[MaskEntry]) -> None:
    """Write the current snapshot."""
    lines = sorted(f"{e.tier} {e.relpath}" for e in entries)
    path = state_dir / "mask-snapshot.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- pattern matching ---------------------------------------------------------

def _matches_pattern(compiled: dict, relpath: str, is_dir: bool) -> bool:
    """Check if relpath matches a compiled pattern.

    Respects dir_only (trailing / means dirs only).
    """
    if compiled["dir_only"] and not is_dir:
        return False
    return bool(compiled["regex"].search(relpath))


def _path_escapes_workspace(realpath: str, workspace_str: str) -> bool:
    """True if the canonical path is outside the workspace."""
    if not realpath:
        return False
    return not realpath.startswith(workspace_str + os.sep) and realpath != workspace_str


# --- public API ---------------------------------------------------------------

def resolve(
    workspace: str | Path,
    state_dir: str | Path,
    mode: str | None = None,
    agentignore_name: str = AGENTIGNORE_NAME,
) -> MaskResult:
    """Resolve the full mask policy for `workspace`.

    Args:
        workspace: Path to the workspace directory to scan.
        state_dir: Path to the harness state directory (holds authoritative config).
        mode: Override mode (deny/allow). None = check env + header directives.
        agentignore_name: In-workspace config file basename.

    Returns a ``MaskResult`` with the resolved mask entries, warnings, and
    protection-reduction info.
    """
    result = MaskResult()
    workspace = Path(workspace).resolve()
    state_dir = Path(state_dir)
    workspace_str = str(workspace)

    # --- step 1: assemble the ordered rule set ---------------------------------
    mode_from_env = os.environ.get("DEEPAGENTS_MASK_MODE", "").strip() or None
    if mode is None:
        if mode_from_env:
            mode = mode_from_env
            result.mode_source = "env"
        else:
            mode = MODE_DENY
            result.mode_source = "default"
    result.mode = mode

    # 1a: shipped pattern-default globs
    default_rules: list[dict] = []
    for pat in PATTERN_DEFAULTS:
        default_rules.append(_compile_pattern(pat, workspace_str))

    # 1b: state-dir authoritative config (general patterns + floor)
    state_config = _read_state_agentignore(state_dir)
    floor_globs: list[str] = list(state_config["floor_paths"])
    floor_rules: list[dict] = []
    for pat in floor_globs:
        floor_rules.append(_compile_pattern(pat, workspace_str))

    # 1c: in-workspace .agentignore files
    workspace_configs = _collect_agentignore_files(workspace)
    user_rules: list[dict] = []
    for _, base_dir, parsed in workspace_configs:
        header_mode = parsed["directives"].get("mode")
        if header_mode and not mode_from_env:
            mode = header_mode
            result.mode_source = "header"
        for p in parsed["patterns"]:
            p["base_dir"] = base_dir
            user_rules.append(p)

    result.mode = mode

    # --- step 2 & 3: walk workspace and match ----------------------------------
    entries = _walk_workspace(workspace)
    masked_set: dict[str, str] = {}  # relpath -> tier

    # Build ordered rule list with tier info
    all_rules: list[tuple[str, dict]] = []  # (tier, rule)
    for r in default_rules:
        all_rules.append((TIER_DEFAULT, r))
    for r in state_config["patterns"]:
        all_rules.append((TIER_USER, r))
    for r in user_rules:
        all_rules.append((TIER_USER, r))

    for entry in entries:
        relpath = entry["relpath"]
        is_dir = entry["is_dir"]
        realpath = entry.get("realpath", "")

        # Canonicalize: symlink escapes → mask
        if entry["is_symlink"] and _path_escapes_workspace(realpath, workspace_str):
            masked_set[relpath] = TIER_DEFAULT
            if relpath not in result.warnings:
                result.warnings.append(
                    f"symlink '{relpath}' escapes workspace — masked"
                )
            continue

        # Floor rules: checked first, always win, never negatable
        floor_masked = False
        for rule in floor_rules:
            if _matches_pattern(rule, relpath, is_dir):
                floor_masked = True
                break

        if floor_masked:
            masked_set[relpath] = TIER_FLOOR
            continue

        # Non-floor rules: scan in order (last match wins)
        is_masked = False
        matched_tier: str | None = None

        for tier, rule in all_rules:
            if _matches_pattern(rule, relpath, is_dir):
                if rule["negated"]:
                    is_masked = False
                    matched_tier = None
                else:
                    is_masked = True
                    matched_tier = tier

        # Allow mode: default visible, only listed entries are masked
        if mode == MODE_ALLOW and not is_masked:
            continue

        if is_masked and matched_tier:
            masked_set[relpath] = matched_tier

    # --- step 4: floor enforcement ---------------------------------------------
    # Re-check: any negation / allow that targets a floor path is dropped
    floor_paths_set: set[str] = set()
    for path_str in floor_globs:
        for entry in entries:
            if entry["is_symlink"]:
                continue
            rel = entry["relpath"]
            compiled = _compile_pattern(path_str, workspace_str)
            if _matches_pattern(compiled, rel, entry["is_dir"]):
                floor_paths_set.add(rel)
                masked_set[rel] = TIER_FLOOR

    # --- step 5: build MaskEntry list (minimized) ------------------------------
    # Group by dir: if all files under a dir are masked with no negation,
    # emit one dir entry instead of per-file entries.

    dir_mask_candidates: dict[str, set[str]] = {}
    file_entries: list[MaskEntry] = []

    for relpath, tier in sorted(masked_set.items()):
        entry_type = "file"
        for e in entries:
            if e["relpath"] == relpath:
                if e["is_dir"]:
                    entry_type = "dir"
                break

        if entry_type == "dir":
            dir_mask_candidates[relpath] = {tier}
        else:
            file_entries.append(MaskEntry(relpath=relpath, type="file", tier=tier))

    # Try to collapse dirs
    for relpath, tiers in dir_mask_candidates.items():
        tier = next(iter(tiers))
        children_masked = all(
            e.relpath.startswith(relpath + "/") for e in file_entries
        )
        has_negated_child = any(relpath.startswith(e.relpath + "/") for e in file_entries if False)
        if not has_negated_child:
            result.masked.append(MaskEntry(relpath=relpath, type="dir", tier=tier))
        else:
            result.masked.append(MaskEntry(relpath=relpath, type="dir", tier=tier))

    result.masked.extend(file_entries)

    # --- step 5b (redo properly): minimize emissions ---------------------------
    # Proper approach: for each dir in masked_set, check if ALL children are masked.
    # If so, emit one dir entry and skip children.

    all_masked = dict(masked_set)
    dir_items = {r for r, t in all_masked.items() if any(e["relpath"] == r and e["is_dir"] for e in entries)}
    file_items = {r for r, t in all_masked.items() if any(e["relpath"] == r and not e["is_dir"] for e in entries)}

    final_entries: list[MaskEntry] = []
    consumed: set[str] = set()

    for d in sorted(dir_items):
        if d in consumed:
            continue
        children = {
            r for r in file_items
            if r.startswith(d + "/") and not consumed.intersection({r})
        }
        if children:
            has_visible = False
            for c in children:
                if c not in all_masked:
                    has_visible = True
                    break
            if not has_visible:
                final_entries.append(MaskEntry(relpath=d, type="dir", tier=all_masked[d]))
                consumed.add(d)
                consumed.update(children)
            else:
                final_entries.append(MaskEntry(relpath=d, type="dir", tier=all_masked[d]))
                consumed.add(d)
        else:
            final_entries.append(MaskEntry(relpath=d, type="dir", tier=all_masked[d]))
            consumed.add(d)

    for r in sorted(file_items):
        if r not in consumed:
            final_entries.append(MaskEntry(relpath=r, type="file", tier=all_masked[r]))

    result.masked = final_entries

    # --- step 6: snapshot + protection-reduction check -------------------------
    prev = _read_snapshot(state_dir)
    curr = set(f"{e.tier} {e.relpath}" for e in result.masked)
    result.snapshot_changed = prev != curr
    removed = prev - curr
    if removed:
        result.protection_reduced = True
        result.reduced_paths = sorted(
            line.split(None, 1)[1] if " " in line else line for line in removed
        )
        result.warnings.append(
            f"protection reduced — {len(removed)} path(s) no longer masked: "
            + ", ".join(result.reduced_paths)
        )

    _write_snapshot(state_dir, result.masked)

    return result


def format_scan_lines(result: MaskResult) -> list[str]:
    """Format MaskResult into the §9.3 stdout grammar.

    ``<mode> <type> <tier> <relpath>`` per line, percent-escaped for spaces.
    """
    lines: list[str] = []
    for entry in result.masked:
        relpath = entry.relpath.replace(" ", "%20")
        lines.append(f"{entry.mode} {entry.type} {entry.tier} {relpath}")
    return lines


# --- mask_add helper (raise-only, append to state-dir authoritative config) ---

def append_deny(state_dir: str | Path, pattern: str) -> None:
    """Append a deny pattern to the state-dir authoritative config.

    Raise-only: can only add protections, never remove. Takes effect next run.
    """
    path = Path(state_dir) / "agentignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = "# .agentignore — authoritative harness config\n"
    existing += pattern.strip() + "\n"
    path.write_text(existing, encoding="utf-8")


def append_floor(state_dir: str | Path, pattern: str) -> None:
    """Append a floor pattern to the state-dir authoritative config.

    Raise-only: can only add protections, never remove. Takes effect next run.
    """
    path = Path(state_dir) / "agentignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = "# .agentignore — authoritative harness config\n"
        existing += "#!floor:\n"
    has_floor_section = "#!floor:" in existing
    if has_floor_section:
        existing += pattern.strip() + "\n"
    else:
        existing += "#!floor:\n" + pattern.strip() + "\n"
    path.write_text(existing, encoding="utf-8")
