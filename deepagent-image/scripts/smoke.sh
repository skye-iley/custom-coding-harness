#!/usr/bin/env bash
# Post-build smoke. Builds both targets (self-contained — no ordering dependency
# on build.sh), runs a bare-runtime import check against the shippable image,
# then runs the whole suite via pytest discovery on the test image.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Build the runtime (shippable) image and the test image (FROM runtime + pytest +
# tests/). The second build reuses the cached runtime layers.
docker build --target runtime -t deepagent-harness "$ROOT"
docker build --target test -t deepagent-harness-test "$ROOT"

# Bare-runtime import smoke: third-party deps + the harness package (incl. the
# cost tracker, so a providers<->cost import cycle fails here). Runs against the
# plain runtime image — NO test layer — so a runtime import the test layer would
# mask still fails here.
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; from harness.cli import main; from harness.cost import CostTrackerMiddleware; print('runtime import ok')"

# Full suite via pytest discovery on the test image. -v names every test case
# (file::test PASSED/FAILED); -ra recaps non-passing tests at the end. Failures
# print the failing test id, file:line, and asserted values by default.
docker run --rm deepagent-harness-test python3 -m pytest tests/ -v -ra
