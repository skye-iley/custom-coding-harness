#!/usr/bin/env bash
# Post-build import smoke (requires image already built).
# Imports the third-party deps AND the harness package, so a broken split or
# import cycle fails here instead of at first real run.
set -euo pipefail
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; from harness.cli import main; print('ok')"
