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

RUN useradd --create-home --uid 10001 agent

# Miniforge for workspace-local conda envs (not on harness PATH). Installed +
# chowned here, ABOVE the requirements.txt COPY, because /opt/conda does not
# depend on the harness deps at all. Keeping the big conda tree upstream of the
# source-dependent layers means its recursive chown (tens of thousands of files —
# the dominant cost of the old combined sweep) stays cached across requirements.txt
# edits, re-running only when miniforge itself or the base image changes.
RUN curl -fsSL https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
        -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda config --system --set auto_activate_base false \
    && /opt/conda/bin/conda clean -a -y
RUN chown -R agent:agent /opt/conda

ENV CONDA_ROOT=/opt/conda
ENV MAMBA_ROOT_PREFIX=/opt/conda

WORKDIR /tmp/build
COPY project/requirements.txt .
# venv install chowns its OWN output inline (per the guardrail below): the conda
# sweep above already ran and no longer trails the whole build, so this layer must
# fix up ownership of what it writes. /opt/venv is small (a single Python env), so
# re-running this on a requirements.txt change is cheap — unlike the conda tree.
# GUARDRAIL: any future RUN that writes to /opt/venv or /opt/conda as root before
# USER agent must chown its own output — there is no trailing catch-all sweep.
# (The test stage installs as USER agent, so its files are already agent-owned.)
RUN uv venv /opt/venv --python python3 \
    && uv pip install --index-url https://pypi.org/simple -r requirements.txt \
    && chown -R agent:agent /opt/venv
RUN python3 -c "import deepagents, langgraph, langgraph.checkpoint.sqlite, langchain_openai, langchain_deepseek, langchain_ollama"

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
COPY project/workflows/ ./workflows/
COPY project/workspace/ ./workspace/
# Host-side artifact (the launchers pass it to `docker run --security-opt`), but
# copied in so `harness doctor` can verify the profile from inside the image too.
COPY seccomp/ ./seccomp/
# M4 slice J: the AppArmor profile is never applied from inside the container (it
# is loaded into the HOST kernel by root), but `harness doctor` verifies the
# vendored artifact is still narrow, so the file has to be present in-image.
COPY apparmor/ ./apparmor/
COPY scripts/conda-init-workspace.sh /usr/local/bin/conda-init-workspace
COPY scripts/sandbox-exec.sh /usr/local/bin/sandbox-exec
COPY project/workspace/ /project/workspace-seed/
RUN chmod +x /usr/local/bin/conda-init-workspace \
    && chmod +x /usr/local/bin/sandbox-exec \
    && chmod +x /project/workspace/scripts/run-in-env.sh \
    && chmod +x /project/workspace-seed/scripts/run-in-env.sh \
    && mkdir -p /project/workspace \
    && mkdir -p /project/state \
    && chown -R agent:agent /project

# Explicit marker so main.py knows it runs inside the harness image
# (resolve_workspace uses this instead of sniffing the filesystem).
ENV DEEPAGENTS_IN_CONTAINER=1

USER agent

CMD ["python3", "main.py"]

# bench stage: runtime + pytest only, no tests/. Not the shippable image -- that
# stays pytest-free by design (see the runtime stage's own header comment). This
# is a SEPARATE tag (`docker build --target bench`, conventionally tagged
# deepagent-harness-bench) that the benchmark driver selects via DEEPAGENTS_IMAGE
# so a benchmark instance's shell can actually run `pytest tests/` -- see
# milestone8_selftest_findings.md §1/§4: the runtime image ships no pytest, the
# workspace conda env never exists for a SEED_WORKSPACE=0 instance with no
# environment.yml, and AGENTS.md's manual-activation fallback doesn't work
# either, so a gold-set instance had ZERO working route to a test runner. Pytest
# lands in /opt/venv (the harness venv, already on PATH first) rather than a
# workspace conda env: a benchmark instance is plain-stdlib code with no
# environment.yml, so there is no workspace env to put it in, and the two-stack
# rule (harness deps vs. workspace deps) is about a WORKSPACE's own dependencies,
# not the test-running tool the driver supplies regardless of what the instance
# ships. Distinct from the `test` stage below, which carries tests/ +
# requirements-dev.txt and exists to run THIS repo's own suite, not an agent's.
FROM runtime AS bench

WORKDIR /tmp/build
RUN uv pip install --index-url https://pypi.org/simple pytest

WORKDIR /project

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
