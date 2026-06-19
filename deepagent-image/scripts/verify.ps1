# Verify harness venv imports and conda CLI are present in the image.
$ErrorActionPreference = "Stop"

docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai, sys; print('harness ok', sys.prefix)"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

docker run --rm deepagent-harness /opt/conda/bin/conda --version
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
