# Verify harness venv and conda CLI in one container start.
$ErrorActionPreference = "Stop"
docker run --rm deepagent-harness bash -lc @'
python3 -c "import deepagents, langgraph, langchain_openai; import sys; print(\"harness ok\", sys.prefix)"
/opt/conda/bin/conda --version
'@
