# runtime stage: the shippable harness image — NO test code, NO pytest. A bare
# `docker build` defaults to the LAST stage (`test`, FROM runtime), so the
# scripts pass `--target runtime` explicitly to get this one (see scripts/ + the
# plan's R3). Do not rely on a no-`--target` build to produce the shippable image.
FROM ubuntu:24.04 AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /usr/local/bin/

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_NO_CACHE=1
ENV AGENT_WORKSPACE=/project/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bubblewrap \
        build-essential \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /tmp/build
COPY project/requirements.txt .
RUN uv venv /opt/venv --python python3 \
    && uv pip install --index-url https://pypi.org/simple -r requirements.txt
RUN python3 -c "import deepagents, langgraph, langgraph.checkpoint.sqlite, langchain_openai, langchain_deepseek, langchain_ollama"

# Miniforge for workspace-local conda envs (not on harness PATH).
RUN curl -fsSL https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
        -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --system --set auto_activate_base false \
    && /opt/conda/bin/conda clean -a -y

ENV CONDA_ROOT=/opt/conda
ENV MAMBA_ROOT_PREFIX=/opt/conda

RUN useradd --create-home --uid 10001 agent

WORKDIR /project
# Enumerated COPY of the project/ top level *minus* tests/ — keeps test code out
# of every runtime layer (R4), not just the final filesystem. Mirror the actual
# project/ top level when it changes. (suggestions/, __pycache__/, .env are
# already excluded by .dockerignore; the test stage adds tests/ back.) AGENTS.md
# is load-bearing: harness/agent.py reads it from CWD and appends it to the system
# prompt, so a missing copy silently changes agent behavior.
COPY project/AGENTS.md ./AGENTS.md
COPY project/hooks.json ./hooks.json
COPY project/main.py ./main.py
COPY project/requirements.txt ./requirements.txt
COPY project/agents/ ./agents/
COPY project/harness/ ./harness/
COPY project/memories/ ./memories/
COPY project/providers/ ./providers/
COPY project/skills/ ./skills/
COPY project/workspace/ ./workspace/
COPY scripts/conda-init-workspace.sh /usr/local/bin/conda-init-workspace
COPY scripts/sandbox-exec.sh /usr/local/bin/sandbox-exec
COPY project/workspace/ /project/workspace-seed/
RUN chmod +x /usr/local/bin/conda-init-workspace \
    && chmod +x /usr/local/bin/sandbox-exec \
    && chmod +x /project/workspace/scripts/run-in-env.sh \
    && chmod +x /project/workspace-seed/scripts/run-in-env.sh \
    && mkdir -p /project/workspace \
    && chown -R agent:agent /project /opt/venv /opt/conda

# Explicit marker so main.py knows it runs inside the harness image
# (resolve_workspace uses this instead of sniffing the filesystem).
ENV DEEPAGENTS_IN_CONTAINER=1

USER agent

CMD ["python3", "main.py"]

# test stage: runtime + pytest + tests/. Built with `docker build --target test`
# (smoke). FROM runtime means the suite exercises the real runtime layer, so the
# import-cycle / package-split checks still hold. pytest lives only here, never in
# the shipped image (R2, R4). /opt/venv is chown'd to agent in runtime, so the
# inherited USER agent installs without a USER root window.
FROM runtime AS test

WORKDIR /tmp/build
COPY project/requirements-dev.txt .
RUN uv pip install --index-url https://pypi.org/simple -r requirements-dev.txt

WORKDIR /project
COPY --chown=agent:agent project/tests/ ./tests/

CMD ["python3", "-m", "pytest", "tests/"]
