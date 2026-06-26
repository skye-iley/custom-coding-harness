# Post-build import smoke (requires image already built).
# Imports the third-party deps AND the harness package, so a broken split or
# import cycle fails here instead of at first real run.
$ErrorActionPreference = "Stop"
# Import check: third-party deps + the harness package (incl. the cost tracker,
# so a providers<->cost import cycle fails here, not at first run).
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; from harness.cli import main; from harness.cost import CostTrackerMiddleware; print('ok')"
if ($LASTEXITCODE -ne 0) { throw "smoke import check failed" }
# Cost-tracker unit tests (pure math; no keys/network). Standalone runners baked
# into the test files, so no pytest dependency is needed.
docker run --rm deepagent-harness python3 tests/test_cost.py
if ($LASTEXITCODE -ne 0) { throw "test_cost.py failed" }
docker run --rm deepagent-harness python3 tests/test_sync_models.py
if ($LASTEXITCODE -ne 0) { throw "test_sync_models.py failed" }
# Workflow-engine unit tests (pure; no keys/network). sh-dependent gate tests
# self-skip if sh is absent, but the image has it so they run here.
docker run --rm deepagent-harness python3 tests/test_workflows.py
if ($LASTEXITCODE -ne 0) { throw "test_workflows.py failed" }
