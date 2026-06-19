#!/usr/bin/env bash
# Verify harness venv imports and conda CLI are present in the image.
set -euo pipefail
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai, sys; print('harness ok', sys.prefix)"
docker run --rm deepagent-harness /opt/conda/bin/conda --version
