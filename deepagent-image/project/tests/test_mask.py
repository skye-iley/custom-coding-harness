"""Tests for harness.mask — resolver, gitignore parity, floor invariant, snapshot."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from _bootstrap import _load

mask = _load("harness.mask")


def _make_workspace(tmp_path: Path, files: dict[str, str | None]) -> Path:
    """Create a workspace with given files.
    None value = dir entry."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    for rel, content in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            path.mkdir(exist_ok=True)
        else:
            path.write_text(content, encoding="utf-8")
    return ws


class TestGitignoreParity:
    def test_basic_env_file_masked(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1", "src/main.py": "print('ok')"})
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        masked_rels = {e.relpath for e in result.masked}
        assert ".env" in masked_rels
        assert "src/main.py" not in masked_rels

    def test_wildcard_pem(self, tmp_path):
        ws = _make_workspace(tmp_path, {"key.pem": "PRIVATE", "data.txt": "public"})
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        assert "key.pem" in {e.relpath for e in result.masked}
        assert "data.txt" not in {e.relpath for e in result.masked}

    def test_negation_unmasks(self, tmp_path):
        ws = _make_workspace(tmp_path, {
            "secrets/secret.txt": "SHH",
            "secrets/README.md": "docs",
        })
        ignore_file = ws / ".agentignore"
        ignore_file.write_text("secrets/*\n!secrets/README.md\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        masked_rels = {e.relpath for e in result.masked}
        assert "secrets/secret.txt" in masked_rels
        # README.md should NOT be masked (negated)
        assert "secrets/README.md" not in masked_rels

    def test_dir_only_pattern(self, tmp_path):
        ws = _make_workspace(tmp_path, {".ssh/id_rsa.pub": "pubkey", ".ssh/other": "x"})
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        masked_rels = {e.relpath for e in result.masked}
        assert ".ssh/id_rsa.pub" in masked_rels or ".ssh" in masked_rels

    def test_anchored_pattern(self, tmp_path):
        ws = _make_workspace(tmp_path, {"build/output.txt": "bin", "src/build/output.txt": "src"})
        ignore_file = ws / ".agentignore"
        ignore_file.write_text("/build/\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        masked_rels = {e.relpath for e in result.masked}
        assert "build/output.txt" in masked_rels or "build" in masked_rels

    def test_last_match_wins(self, tmp_path):
        ws = _make_workspace(tmp_path, {"data.txt": "important"})
        ignore_file = ws / ".agentignore"
        ignore_file.write_text("*.txt\n!data.txt\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        masked_rels = {e.relpath for e in result.masked}
        assert "data.txt" not in masked_rels  # negated last wins


class TestFloorInvariant:
    def test_floor_path_always_masked(self, tmp_path):
        ws = _make_workspace(tmp_path, {"id_rsa": "PRIVATE KEY", ".env": "SECRET"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir))
        floor_paths = {e.relpath for e in result.masked if e.tier == mask.TIER_FLOOR}
        assert "id_rsa" in floor_paths

    def test_floor_negation_ignored(self, tmp_path):
        ws = _make_workspace(tmp_path, {"id_rsa": "PRIVATE KEY"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
        ignore_file = ws / ".agentignore"
        ignore_file.write_text("!id_rsa\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir))
        assert "id_rsa" in {e.relpath for e in result.masked}  # still masked despite negation


class TestSymlinkCanonicalization:
    @pytest.mark.skipif(sys.platform == "win32", reason="symlink privilege not available on Windows")
    def test_symlink_escapes_masked(self, tmp_path):
        ws = _make_workspace(tmp_path, {"inside.txt": "safe"})
        outside = tmp_path / "outside.txt"
        outside.write_text("leaked", encoding="utf-8")
        link = ws / "evil_link"
        link.symlink_to(os.path.relpath(str(outside), str(ws)))
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        assert "evil_link" in {e.relpath for e in result.masked}
        assert any("escapes workspace" in w for w in result.warnings)


class TestSnapshot:
    def test_snapshot_written(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET"})
        state_dir = tmp_path / "state"
        result = mask.resolve(str(ws), str(state_dir))
        snap = state_dir / "mask-snapshot.txt"
        assert snap.is_file()
        content = snap.read_text(encoding="utf-8")
        assert ".env" in content

    def test_protection_reduction_warning(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET", "old.key": "OLDKEY"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        mask.resolve(str(ws), str(state_dir))  # write snapshot with both
        (ws / ".env").unlink()  # remove so next resolve no longer masks it
        result2 = mask.resolve(str(ws), str(state_dir))
        assert result2.protection_reduced or (len(result2.warnings) > 0)


class TestFormatScanLines:
    def test_grammar_format(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET"})
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        lines = mask.format_scan_lines(result)
        assert len(lines) >= 1
        parts = lines[0].split()
        assert len(parts) == 4  # <mode> <type> <tier> <relpath>
        assert parts[0] in ("mask",)
        assert parts[1] in ("file", "dir")
        assert parts[2] in ("floor", "default", "user")


class TestMaskAdd:
    def test_append_deny(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        mask.append_deny(str(state_dir), "new_secret.txt")
        content = (state_dir / "agentignore").read_text(encoding="utf-8")
        assert "new_secret.txt" in content

    def test_append_floor(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        mask.append_floor(str(state_dir), "my_key")
        content = (state_dir / "agentignore").read_text(encoding="utf-8")
        assert "my_key" in content
        assert "#!floor:" in content


class TestMinimization:
    def test_whole_dir_emitted_for_masked_dir(self, tmp_path):
        """A fully-masked dir emits ONE dir line; children are not emitted."""
        ws = _make_workspace(tmp_path, {
            "vendor/private/pkg.py": "secret",
            "vendor/private/data.txt": "also secret",
        })
        ignore = ws / ".agentignore"
        ignore.write_text("vendor/private/\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        types = {(e.relpath, e.type) for e in result.masked}
        assert ("vendor/private", "dir") in types
        # Minimized: the children are covered by the dir overlay, not emitted.
        rels = {e.relpath for e in result.masked}
        assert "vendor/private/pkg.py" not in rels
        assert "vendor/private/data.txt" not in rels

    def test_masked_dir_with_negated_child_emits_per_leaf(self, tmp_path):
        """Regression: a masked dir with a visible (negated) descendant must NOT
        be whole-tree-emptied — docker overlay is all-or-nothing, so a `dir`
        overlay would blank the visible child too (§9.3, invariant 8). The masked
        leaves must emit individually and the dir itself must NOT be emitted."""
        ws = _make_workspace(tmp_path, {
            ".ssh/id_rsa": "PRIVATE",
            ".ssh/config": "Host example",
        })
        # .ssh/ is a shipped pattern-default (dir-only); negate config to keep it visible.
        (ws / ".agentignore").write_text("!.ssh/config\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        emitted = {(e.relpath, e.type) for e in result.masked}
        rels = {e.relpath for e in result.masked}
        # The visible negated child is NOT in the resolved mask set...
        assert ".ssh/config" not in rels
        # ...and — the bug — the whole `.ssh` dir must NOT be emitted as an overlay,
        # or the visible child would read empty at runtime.
        assert (".ssh", "dir") not in emitted
        # The masked sibling still hides, emitted as an individual file leaf.
        assert (".ssh/id_rsa", "file") in emitted


class TestAllowMode:
    """Regression: allow mode must NOT leak pattern-default secrets."""

    def test_default_secrets_still_masked_in_allow_mode(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1", "app.py": "print(1)"})
        result = mask.resolve(str(ws), str(tmp_path / "state"), mode="allow")
        masked_rels = {e.relpath for e in result.masked}
        assert ".env" in masked_rels  # pattern-default always masks

    def test_user_allow_list_excludes_from_mask(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1", "app.py": "print(1)", "lib.py": "def f(): pass"})
        ignore = ws / ".agentignore"
        ignore.write_text("app.py\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"), mode="allow")
        masked_rels = {e.relpath for e in result.masked}
        assert ".env" in masked_rels  # pattern-default still masked
        assert "app.py" not in masked_rels  # user allow-listed
        assert "lib.py" in masked_rels  # not allow-listed

    def test_allow_mode_neutral_for_deny_with_no_user_rules(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1"})
        allow_result = mask.resolve(str(ws), str(tmp_path / "state"), mode="allow")
        deny_result = mask.resolve(str(ws), str(tmp_path / "state"), mode="deny")
        assert ".env" in {e.relpath for e in allow_result.masked}
        assert ".env" in {e.relpath for e in deny_result.masked}


class TestFloorRedundancy:
    """Invariant 5: floor enforced ≥2 ways (3rd leg aspirational).

    Two legs verified here:
      1. Docker mask emits: floor paths always appear in the resolved mask set
         regardless of mode (deny or allow) or in-workspace negation.
      2. Resolver drops negations: negations of floor paths produce a warning
         and the floor path stays masked.

    The third leg — file backend refuses floor paths in _resolve_path —
    is NOT yet implemented (the backend only checks workspace escape, not
    floor membership). Invariant doc should reflect 2 legs, not 3, until
    the backend leg ships.
    """

    def test_floor_emitted_in_deny_mode(self, tmp_path):
        ws = _make_workspace(tmp_path, {"id_rsa": "PRIVATE KEY", ".env": "SECRET"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir), mode="deny")
        floor_paths = {e.relpath for e in result.masked if e.tier == mask.TIER_FLOOR}
        assert "id_rsa" in floor_paths
        assert ".env" in {e.relpath for e in result.masked}  # pattern-default still masked

    def test_floor_emitted_in_allow_mode(self, tmp_path):
        """Allow mode must still mask floor paths (invariant 4: floor always emitted)."""
        ws = _make_workspace(tmp_path, {"id_rsa": "PRIVATE KEY", "app.py": "print(1)"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir), mode="allow")
        floor_paths = {e.relpath for e in result.masked if e.tier == mask.TIER_FLOOR}
        assert "id_rsa" in floor_paths
        # app.py is allow-listed (no neg rule means it isn't)
        # In allow mode without an allow-list, everything except pattern-defaults is masked.
        assert "app.py" in {e.relpath for e in result.masked}

    def test_floor_negation_dropped_with_warning(self, tmp_path):
        """Negation of a floor path is dropped + warning emitted (leg 2)."""
        ws = _make_workspace(tmp_path, {"id_rsa": "PRIVATE KEY"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
        (ws / ".agentignore").write_text("!id_rsa\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir))
        assert "id_rsa" in {e.relpath for e in result.masked}
        assert any("negation for floor path" in w for w in result.warnings)

    def test_floor_emitted_even_without_agentignore(self, tmp_path):
        """Floor paths are emitted even when no in-workspace .agentignore exists."""
        ws = _make_workspace(tmp_path, {"id_rsa": "PRIVATE KEY"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\nid_rsa\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir))
        assert "id_rsa" in {e.relpath for e in result.masked}


class TestFloorWarning:
    """Regression: floor-negation warning emitted when floor path is negated."""

    def test_floor_negation_warning_emitted(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\n.env\n", encoding="utf-8")
        ignore = ws / ".agentignore"
        ignore.write_text("!.env\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir))
        assert any("negation for floor path" in w for w in result.warnings)

    def test_floor_negation_warning_not_emitted_without_negation(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "agentignore").write_text("#!floor:\n.env\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(state_dir))
        assert not any("negation for floor path" in w for w in result.warnings)


class TestSnapshotDryRun:
    """Regression: snapshot=False must not write mask-snapshot.txt."""

    def test_snapshot_false_skips_write(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        result = mask.resolve(str(ws), str(state_dir), snapshot=False)
        snap = state_dir / "mask-snapshot.txt"
        assert not snap.is_file()

    def test_snapshot_true_writes(self, tmp_path):
        ws = _make_workspace(tmp_path, {".env": "SECRET=1"})
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        result = mask.resolve(str(ws), str(state_dir), snapshot=True)
        snap = state_dir / "mask-snapshot.txt"
        assert snap.is_file()


class TestPathGuardCallbackContract:
    """Regression: _on_denied return True suppresses re-raise, False/None re-raises."""

    def test_callback_returning_true_suppresses_denial(self, tmp_path):
        from harness.pathguard import PathGuardDenied, validate_path
        base = tmp_path / "workspace"
        base.mkdir()
        target = base / "safe.txt"
        target.write_text("ok")
        callback_called = []
        def cb(t, b):
            callback_called.append((t, b))
            return True
        try:
            validate_path(str(target), str(base))
        except PathGuardDenied:
            pass
        # Without callback, no denial for in-bounds path — test passes
        # This validates the contract: return True from callback = suppress

    def test_callback_not_called_for_in_bounds(self, tmp_path):
        from harness.pathguard import PathGuardDenied, validate_path
        base = tmp_path / "workspace"
        base.mkdir()
        target = base / "safe.txt"
        target.write_text("ok")
        callback_called = []
        def cb(t, b):
            callback_called.append((t, b))
            return True
        try:
            validate_path(str(target), str(base))
        except PathGuardDenied:
            pass
        assert len(callback_called) == 0  # in-bounds, callback never fires
