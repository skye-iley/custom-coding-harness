# Post-build import smoke (requires image already built).
$ErrorActionPreference = "Stop"
docker run --rm deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; print('ok')"
