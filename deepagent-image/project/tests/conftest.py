"""Shared pytest fixtures for the harness test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _artifacts import resolve_artifact_dir

# Committed fixture registry mirroring the real provider layout
# (<provider>/provider.toml + models/<model>.toml). Resolved from this conftest's
# location so it works regardless of pytest's invocation cwd.
_FIXTURE_REGISTRY = Path(__file__).resolve().parent / "fixtures" / "providers"

# project/ — the dir bind-mounted to /project in the container. The artifact
# guard below watches it so a test run never leaves files in the repo or the
# mounted workspace.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_KEEP_ENV = "DEEPAGENTS_KEEP_TEST_ARTIFACTS"


def _tracked_paths(root: Path) -> set[Path]:
    """Every path under `root`, ignoring Python bytecode caches (pytest/python
    create those and they're not test artifacts)."""
    return {
        p
        for p in root.rglob("*")
        if "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


@pytest.fixture(scope="session", autouse=True)
def _clean_repo_artifacts():
    """Backstop cleanup of anything a test writes under project/.

    The plan is for every filesystem-touching test to write to pytest's
    `tmp_path` (OS temp, auto-removed, never under the bind-mounted workspace).
    This fixture is the safety net for code paths that force a write under the
    process CWD anyway (e.g. a checkpoint DB or a stray workspace dir): it diffs
    the project/ tree across the whole session and removes any path that wasn't
    there at the start, so the repo and the host-mounted workspace are left
    byte-for-byte as they were.

    Set DEEPAGENTS_KEEP_TEST_ARTIFACTS=1 to keep the leftovers for debugging
    (the "unless flagged" escape hatch) — nothing is deleted then.
    """
    before = _tracked_paths(_PROJECT_ROOT)
    yield
    if os.getenv(_KEEP_ENV):
        return
    after = _tracked_paths(_PROJECT_ROOT)
    # Deepest paths first so a dir's contents are gone before we rmdir it; dirs
    # are only removed when empty, so a pre-existing dir that gained a tracked
    # child survives.
    for path in sorted(after - before, key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            pass


@pytest.fixture
def workspace_sandbox(tmp_path, monkeypatch):
    """A throwaway workspace under pytest's tmp_path, with CWD pointed at it.

    Several harness reads are CWD-relative (AGENTS.md / .mcp.json / hooks.json
    via Path.cwd(), the checkpoint DB under the workspace). Running the test from
    an empty tmp dir keeps those reads from hitting the real project/ files and
    keeps every write inside tmp_path, which pytest deletes after the test — so
    nothing the test creates ever reaches the repo or the mounted workspace.
    Yields the workspace Path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    return workspace


@pytest.fixture
def artifact_dir(request, tmp_path):
    """A writable dir for a test to drop files it wants to inspect after the run.

    Default: pytest's `tmp_path` — deleted when the session ends. Under smoke's
    `-KeepArtifacts` / `KEEP_ARTIFACTS=1` (which sets
    `DEEPAGENTS_TEST_ARTIFACTS_DIR` and bind-mounts it to a host folder), the dir
    is a per-test subdir of that path, so the files are shipped out and survive
    the disposable container. See tests/_artifacts.py."""
    return resolve_artifact_dir(request.node.name, tmp_path)


# --- live-model tier -------------------------------------------------------
#
# The third test tier: cases that send a real prompt to a real model and assert
# on the real reply. Off unless DEEPAGENTS_LIVE_MODEL=1, so the host and image
# tiers stay hermetic (no keys, no network) and CI is unaffected. Ollama being
# the default provider is what makes this practical -- a local model has no
# free-tier quota to burn. See "Test suite layout & conventions" in
# deepagent-image/CLAUDE.md.

_LIVE_ENV = "DEEPAGENTS_LIVE_MODEL"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_model: sends a real prompt to a real model; runs only when "
        f"{_LIVE_ENV}=1 (see tests/conftest.py).",
    )


def pytest_collection_modifyitems(config, items):
    if os.getenv(_LIVE_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"live-model tier off; set {_LIVE_ENV}=1 to run")
    for item in items:
        if "live_model" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_model():
    """A real chat model, resolved the same way a real run resolves one.

    Goes through `providers.choose_model` + `validate_credentials` +
    `resolve_chat_model` rather than constructing a client directly, so the test
    exercises the harness's own routing and a routing regression shows up here
    too. Skips (never fails) when the runtime stack is absent or the model is
    unreachable -- an offline laptop should not turn the suite red, but note that
    means a silent skip is possible: check for the `live_model` cases in the
    output when you mean to be testing against a real model.
    """
    pytest.importorskip("langchain")
    providers = _load_providers_module()
    spec = providers.choose_model(None)
    providers.validate_credentials(spec)
    try:
        model = providers.resolve_chat_model(spec)
        # Cheapest possible round trip: proves the daemon/endpoint answers before
        # a test blames the harness for what is really a model that isn't running.
        if isinstance(model, str):
            from langchain.chat_models import init_chat_model

            model = init_chat_model(model)
        model.invoke("ping")
    except Exception as exc:  # noqa: BLE001 - any transport/config failure = skip
        pytest.skip(f"live model {spec!r} unreachable: {type(exc).__name__}: {exc}")
    return model


def _load_providers_module():
    """Import harness.providers the same way the test modules do."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bootstrap import _load

    return _load("harness.providers")


@pytest.fixture
def provider_registry():
    """Redirect DEEPAGENTS_PROVIDERS_DIR at the committed fixture registry for the
    test's duration, then restore it. Set BEFORE the test imports
    harness.providers (the registry loads at import time), so consumers must take
    this fixture and only then `_load("harness.providers")`."""
    previous = os.environ.get("DEEPAGENTS_PROVIDERS_DIR")
    os.environ["DEEPAGENTS_PROVIDERS_DIR"] = str(_FIXTURE_REGISTRY)
    try:
        yield _FIXTURE_REGISTRY
    finally:
        if previous is None:
            os.environ.pop("DEEPAGENTS_PROVIDERS_DIR", None)
        else:
            os.environ["DEEPAGENTS_PROVIDERS_DIR"] = previous
