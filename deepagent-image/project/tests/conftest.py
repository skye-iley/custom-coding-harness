"""Shared pytest fixtures for the harness test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Committed fixture registry mirroring the real provider layout
# (<provider>/provider.toml + models/<model>.toml). Resolved from this conftest's
# location so it works regardless of pytest's invocation cwd.
_FIXTURE_REGISTRY = Path(__file__).resolve().parent / "fixtures" / "providers"


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
