"""Test-artifact directory resolution.

A test that needs to write a file to inspect after the run takes the
`artifact_dir` fixture (conftest.py). Where that dir lives — and therefore
whether the file survives the run — is decided here from one env var:

- `DEEPAGENTS_TEST_ARTIFACTS_DIR` unset (default): the dir is pytest's per-test
  `tmp_path`, which pytest deletes when the session ends. Files are **deleted
  after the run**.
- set (smoke's `-KeepArtifacts` / `KEEP_ARTIFACTS=1`): the dir is
  `<that path>/<sanitized test node name>`. smoke bind-mounts that path to a host
  folder (`test-artifacts/<timestamp>/`), so the files are **shipped out** and
  survive the disposable container.

Kept as a plain function (not just fixture body) so it is unit-testable with a
fake env — see test_artifacts.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_VAR = "DEEPAGENTS_TEST_ARTIFACTS_DIR"

# Collapse anything that isn't filename-safe into '_' so a pytest node id like
# "test_foo[case-1]" becomes a tidy directory name across Windows and POSIX.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(node_name: str) -> str:
    return _UNSAFE.sub("_", node_name).strip("_") or "artifact"


def resolve_artifact_dir(
    node_name: str,
    tmp_path: Path,
    env: dict | None = None,
) -> Path:
    """Return a writable dir for `node_name`'s artifacts, creating it.

    Uses `DEEPAGENTS_TEST_ARTIFACTS_DIR` from `env` (defaults to `os.environ`)
    when set, else `tmp_path` (auto-removed by pytest).
    """
    env = os.environ if env is None else env
    base = env.get(ENV_VAR)
    if base:
        target = Path(base) / _sanitize(node_name)
    else:
        target = Path(tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    return target
