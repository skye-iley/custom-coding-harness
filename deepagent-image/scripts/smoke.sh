#!/usr/bin/env bash
# Post-build import smoke (requires image already built).
set -euo pipefail
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; print('ok')"
