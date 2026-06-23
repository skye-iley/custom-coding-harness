#!/usr/bin/env bash
# Run the harness container. Requires project/.env (copy from project/.env.example).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/project/.env"
WORKSPACE="${WORKSPACE:-$ROOT/project/workspace}"
SEED_SOURCE="$ROOT/project/workspace"

# Resource caps (Milestone 1 §3): a Docker host-boundary control so a runaway
# agent can't exhaust the host CPU/RAM or fork-bomb it. NOT a sandbox (the trust
# boundary is still the container; see design_doc_mvp.md §5). Override via env:
#   CPUS=4 MEMORY=8g PIDS_LIMIT=1024 ./run-docker.sh "task"
CPUS="${CPUS:-2}"
MEMORY="${MEMORY:-4g}"
PIDS_LIMIT="${PIDS_LIMIT:-512}"
CAP_FLAGS=(--cpus "$CPUS" --memory "$MEMORY" --pids-limit "$PIDS_LIMIT")

seed_workspace() {
  local target="$1"
  local seed="$2"
  [[ -d "$seed" ]] || return 0
  for file in environment.yml .gitignore; do
    if [[ ! -f "$target/$file" && -f "$seed/$file" ]]; then
      cp "$seed/$file" "$target/$file"
    fi
  done
  if [[ -f "$seed/scripts/run-in-env.sh" && ! -f "$target/scripts/run-in-env.sh" ]]; then
    mkdir -p "$target/scripts"
    cp "$seed/scripts/run-in-env.sh" "$target/scripts/run-in-env.sh"
    chmod +x "$target/scripts/run-in-env.sh"
  fi
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE - copy project/.env.example to project/.env and set API keys." >&2
  exit 1
fi

mkdir -p "$WORKSPACE"
WORKSPACE="$(cd "$WORKSPACE" && pwd)"
seed_workspace "$WORKSPACE" "$SEED_SOURCE"

# Git identity: mount host .gitconfig read-only into the agent user's home (uid 10001 -> /home/agent),
# not /root (container runs USER agent). Never mount ~/.ssh into an autonomous-agent container -
# use a scoped, per-session deploy key or a short-lived token for pushes instead.
GIT_MOUNT=()
if [[ -f "$HOME/.gitconfig" ]]; then
  GIT_MOUNT=(-v "$HOME/.gitconfig:/home/agent/.gitconfig:ro")
fi

# -it gives the REPL prompt loop a TTY. If stdin isn't actually a terminal
# (CI, piped smoke tests), -t fails to allocate and Docker falls back to a
# plain pipe, which the harness already handles via the non-TTY fallback.
TTY_FLAGS="-i"
if [[ -t 0 ]]; then
  TTY_FLAGS="-it"
fi

if [[ $# -gt 0 ]]; then
exec docker run --rm $TTY_FLAGS \
  "${CAP_FLAGS[@]}" \
  --env-file "$ENV_FILE" \
  -e AGENT_WORKSPACE=/project/workspace \
  -v "$WORKSPACE:/project/workspace" \
  ${GIT_MOUNT[@]+"${GIT_MOUNT[@]}"} \
  deepagent-harness \
  python3 main.py "$@"
fi

exec docker run --rm $TTY_FLAGS \
  "${CAP_FLAGS[@]}" \
  --env-file "$ENV_FILE" \
  -e AGENT_WORKSPACE=/project/workspace \
  -v "$WORKSPACE:/project/workspace" \
  ${GIT_MOUNT[@]+"${GIT_MOUNT[@]}"} \
  deepagent-harness
