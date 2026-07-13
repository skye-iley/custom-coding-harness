"""Tests for the test-artifact directory mechanism (tests/_artifacts.py + the
`artifact_dir` fixture).

Host-runnable: stdlib only, no keys/network/harness runtime. Exercises both the
default "deleted after run" path (dir under tmp_path) and the "shipped out" path
(dir under DEEPAGENTS_TEST_ARTIFACTS_DIR).
"""

from __future__ import annotations

from pathlib import Path

from _artifacts import ENV_VAR, _sanitize, resolve_artifact_dir


def test_default_dir_is_tmp_path(tmp_path):
    """No env var set -> the artifact dir is tmp_path itself (pytest deletes it,
    so files written there are gone after the run)."""
    got = resolve_artifact_dir("test_foo", tmp_path, env={})
    assert got == tmp_path
    assert got.is_dir()


def test_env_dir_gets_per_test_subdir(tmp_path):
    """Env var set -> the dir is <base>/<sanitized node name>, created for the
    test. This is the path smoke bind-mounts to a host folder."""
    base = tmp_path / "artifacts"
    got = resolve_artifact_dir("test_bar", tmp_path, env={ENV_VAR: str(base)})
    assert got == base / "test_bar"
    assert got.is_dir()
    # It is under the shipped-out base, NOT the ephemeral tmp_path.
    assert base in got.parents


def test_node_name_is_sanitized(tmp_path):
    """A parametrized node id like 'test_x[a/b]' must not create nested or
    unsafe dirs — the whole leaf is one filename-safe segment."""
    base = tmp_path / "out"
    got = resolve_artifact_dir("test_x[a/b:c]", tmp_path, env={ENV_VAR: str(base)})
    assert got.parent == base
    assert got.name == "test_x_a_b_c"
    assert "/" not in got.name and "\\" not in got.name


def test_sanitize_never_empty():
    assert _sanitize("///") == "artifact"
    assert _sanitize("ok.name-1") == "ok.name-1"


def test_fixture_round_trips_a_file(artifact_dir):
    """End-to-end via the fixture: create a temp file, read it back. In the
    default config artifact_dir is tmp_path and this file is cleaned up; under
    -KeepArtifacts it lands in the shipped-out folder."""
    out = Path(artifact_dir) / "hello.txt"
    out.write_text("artifact payload\n", encoding="utf-8")
    assert out.read_text(encoding="utf-8") == "artifact payload\n"


def test_fixture_dir_is_writable_and_isolated(artifact_dir):
    """A second test using the fixture gets its own writable dir and does not
    see the previous test's file (per-test isolation)."""
    d = Path(artifact_dir)
    assert d.is_dir()
    assert not (d / "hello.txt").exists()
    (d / "second.log").write_text("ok", encoding="utf-8")
    assert (d / "second.log").read_text(encoding="utf-8") == "ok"
