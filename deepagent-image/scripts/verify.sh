#!/usr/bin/env bash
# Verify harness venv imports and conda CLI are present in the image.
# Targets the `deepagent-harness` runtime image built by build.sh
# (`docker build --target runtime`).
set -euo pipefail
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai, sys; print('harness ok', sys.prefix)"
docker run --rm deepagent-harness /opt/conda/bin/conda --version
