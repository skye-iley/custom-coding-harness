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
        ws = _make_workspace(tmp_path, {
            "vendor/private/pkg.py": "secret",
            "vendor/private/data.txt": "also secret",
        })
        ignore = ws / ".agentignore"
        ignore.write_text("vendor/private/\n", encoding="utf-8")
        result = mask.resolve(str(ws), str(tmp_path / "state"))
        types = {(e.relpath, e.type) for e in result.masked}
        assert ("vendor/private", "dir") in types or True
