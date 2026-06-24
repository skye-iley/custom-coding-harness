#!/usr/bin/env bash
# Post-build import smoke (requires image already built).
# Imports the third-party deps AND the harness package, so a broken split or
# import cycle fails here instead of at first real run.
set -euo pipefail
# Import check: third-party deps + the harness package (incl. the cost tracker,
# so a providers<->cost import cycle fails here, not at first run).
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; from harness.cli import main; from harness.cost import CostTrackerMiddleware; print('ok')"
# Cost-tracker unit tests (pure math; no keys/network). These run the standalone
# runners baked into the test files, so no pytest dependency is needed.
docker run --rm deepagent-harness python3 tests/test_cost.py
docker run --rm deepagent-harness python3 tests/test_sync_models.py
