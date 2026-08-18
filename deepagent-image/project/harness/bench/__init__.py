"""Benchmark ladder, tier 1 (Milestone 8).

Deliberately empty of imports. `harness.bench` must stay keyless in the strong
sense — no API key, no network, no model, and **no runtime stack** — so
`tests/test_import_isolation.py` can pin it alongside `entry` / `doctor` /
`telemetry`. A convenience re-export here would defeat that the moment one of
the submodules grew a dependency, which is the same defect M5 §0.1 F6 removed
from `harness/__init__.py`.

Import the submodule you want:

    from harness.bench.patch import extract_patch
"""

from __future__ import annotations
