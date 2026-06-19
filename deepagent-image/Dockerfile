FROM ubuntu:24.04

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /usr/local/bin/

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_NO_CACHE=1
ENV AGENT_WORKSPACE=/project/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
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
COPY project/ .
COPY scripts/conda-init-workspace.sh /usr/local/bin/conda-init-workspace
COPY project/workspace/ /project/workspace-seed/
RUN chmod +x /usr/local/bin/conda-init-workspace \
    && chmod +x /project/workspace/scripts/run-in-env.sh \
    && chmod +x /project/workspace-seed/scripts/run-in-env.sh \
    && mkdir -p /project/workspace \
    && chown -R agent:agent /project /opt/venv /opt/conda

# Explicit marker so main.py knows it runs inside the harness image
# (resolve_workspace uses this instead of sniffing the filesystem).
ENV DEEPAGENTS_IN_CONTAINER=1

USER agent

CMD ["python3", "main.py"]
