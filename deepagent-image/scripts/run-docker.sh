#!/usr/bin/env bash
# Run the harness container. Requires project/.env (copy from project/.env.example).
# Consumes the `deepagent-harness` runtime image built by build.sh
# (`docker build --target runtime`) — no test code, no pytest.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/project/.env"
WORKSPACE="${WORKSPACE:-$ROOT/project/workspace}"
SEED_SOURCE="$ROOT/project/workspace"
NETJAIL_DIR="$ROOT/netjail"

# Resource caps (Milestone 1 §3): a Docker host-boundary control so a runaway
# agent can't exhaust the host CPU/RAM or fork-bomb it. NOT a sandbox (the trust
# boundary is still the container; see design_doc_mvp.md §5). Override via env:
#   CPUS=4 MEMORY=8g PIDS_LIMIT=1024 ./run-docker.sh "task"
CPUS="${CPUS:-2}"
MEMORY="${MEMORY:-4g}"
PIDS_LIMIT="${PIDS_LIMIT:-512}"
CAP_FLAGS=(--cpus "$CPUS" --memory "$MEMORY" --pids-limit "$PIDS_LIMIT")

# Host reachability: make `host.docker.internal` resolve to the host on native
# Linux (Docker Desktop/WSL2 provide it already; re-declaring host-gateway is a
# harmless no-op there). Lets a container reach host-side services — notably a
# host Ollama daemon: set OLLAMA_HOST=http://host.docker.internal:11434 in
# project/.env (inside the container `localhost` is the container, not the host).
# NOTE: superseded by NET_JAIL below, which instead reaches the host only through
# a locked-down forwarder on an --internal network (see netjail/README.md).
HOST_GW=(--add-host=host.docker.internal:host-gateway)

# Optional host-uid mapping — fixes bind-mount permissions on native Linux, where
# mounts keep host ownership and the image runs as uid 10001 (agent); a workspace
# owned by your host uid is then unwritable to the agent. Opt in with
# MAP_HOST_USER=1 (auto-detects id -u/-g) or set HOST_UID/HOST_GID explicitly, to
# run the container as your uid:gid. Not needed on Docker Desktop/WSL2 (its VM
# squashes ownership). When mapped, HOME is redirected to the world-writable /tmp
# because /home/agent is owned by 10001 and unwritable to the remapped user.
USER_FLAGS=()
HOME_DIR="/home/agent"
if [[ "${MAP_HOST_USER:-}" == "1" ]]; then
  HOST_UID="${HOST_UID:-$(id -u)}"
  HOST_GID="${HOST_GID:-$(id -g)}"
fi
if [[ -n "${HOST_UID:-}" ]]; then
  USER_FLAGS=(--user "${HOST_UID}:${HOST_GID:-$HOST_UID}" -e HOME=/tmp)
  HOME_DIR="/tmp"
fi

# ---------------------------------------------------------------------------
# NetJail (opt-in: NET_JAIL=1) — deny-all-egress network jail with an explicit
# allowlist. The agent runs on an --internal docker network (no route to host or
# internet). Two kinds of controlled hole are punched, both driven by plain-text
# config so adding a permission is a one-line edit (see netjail/README.md):
#   - netjail/host-services.txt : per-line socat forwarder to a HOST port.
#   - netjail/allowed-domains.txt: per-line domain the egress proxy will reach.
# Everything not listed is unreachable by construction. Default OFF: without
# NET_JAIL the script behaves exactly as before (host-gateway on the bridge).
JAIL_NET="${NETJAIL_JAIL_NET:-deepagent-jail}"
EGRESS_NET="${NETJAIL_EGRESS_NET:-deepagent-egress}"
SOCAT_IMAGE="${NETJAIL_SOCAT_IMAGE:-alpine/socat:latest}"
PROXY_IMAGE="${NETJAIL_PROXY_IMAGE:-kalaksi/tinyproxy:latest}"
SIDECARS=()          # container names to tear down
FILTER_TMP=""        # generated proxy allowlist file to clean up
NET_ARGS=()          # docker network flags for the agent container
PROXY_ENV=()         # -e ... proxy/OLLAMA env for the agent container

netjail_down() {
  [[ ${#SIDECARS[@]} -gt 0 ]] && docker rm -f "${SIDECARS[@]}" >/dev/null 2>&1 || true
  [[ -n "$FILTER_TMP" && -f "$FILTER_TMP" ]] && rm -f "$FILTER_TMP" || true
}

netjail_up() {
  command -v docker >/dev/null || { echo "NET_JAIL: docker not found" >&2; exit 1; }
  trap netjail_down EXIT INT TERM

  # Networks (idempotent). jail = --internal (no gateway → no host/internet route).
  docker network inspect "$JAIL_NET"   >/dev/null 2>&1 || docker network create --internal "$JAIL_NET" >/dev/null
  docker network inspect "$EGRESS_NET" >/dev/null 2>&1 || docker network create "$EGRESS_NET" >/dev/null

  NET_ARGS=(--network "$JAIL_NET")
  PROXY_ENV=()
  local no_proxy="localhost,127.0.0.1"

  # Host-service forwarders. Each is attached to the egress net (has the host
  # route) plus the jail net (agent-visible), and relays exactly one TCP port.
  local services="$NETJAIL_DIR/host-services.txt"
  if [[ -f "$services" ]]; then
    local name port
    while read -r name port _; do
      [[ -z "${name:-}" || "$name" == \#* ]] && continue
      local cname="deepagent-fwd-$name"
      docker rm -f "$cname" >/dev/null 2>&1 || true
      docker run -d --rm --name "$cname" \
        --network "$EGRESS_NET" \
        --add-host=host.docker.internal:host-gateway \
        --cap-drop=ALL --security-opt=no-new-privileges \
        "$SOCAT_IMAGE" \
        "TCP-LISTEN:$port,fork,reuseaddr" "TCP:host.docker.internal:$port" >/dev/null
      docker network connect "$JAIL_NET" "$cname" >/dev/null
      SIDECARS+=("$cname")
      no_proxy="$no_proxy,$cname"
      # Convenience: point the agent's OLLAMA_HOST at the ollama forwarder.
      [[ "$name" == "ollama" ]] && PROXY_ENV+=(-e "OLLAMA_HOST=http://$cname:$port")
    done < "$services"
  fi

  # Egress proxy: domain-allowlisted HTTP(S) forward proxy for git/pip/npm. Only
  # started when the allowlist has at least one real entry.
  local domains="$NETJAIL_DIR/allowed-domains.txt"
  if [[ -f "$domains" ]] && grep -qvE '^[[:space:]]*(#|$)' "$domains"; then
    # Generate the tinyproxy Filter file: anchor each plain domain so it matches
    # the domain and its subdomains (and only those), not arbitrary substrings.
    FILTER_TMP="$(mktemp)"
    local d
    while read -r d _; do
      [[ -z "${d:-}" || "$d" == \#* ]] && continue
      printf '(^|\\.)%s$\n' "$(printf '%s' "$d" | sed 's/[.[\*^$]/\\&/g')" >> "$FILTER_TMP"
    done < "$domains"

    local pname="deepagent-proxy"
    docker rm -f "$pname" >/dev/null 2>&1 || true
    docker run -d --rm --name "$pname" \
      --network "$EGRESS_NET" \
      --cap-drop=ALL --security-opt=no-new-privileges \
      -v "$NETJAIL_DIR/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:ro" \
      -v "$FILTER_TMP:/etc/tinyproxy/filter:ro" \
      "$PROXY_IMAGE" >/dev/null
    docker network connect "$JAIL_NET" "$pname" >/dev/null
    SIDECARS+=("$pname")
    # Fail CLOSED: assert the proxy actually loaded our allowlist config. If the
    # conf mount silently failed to land, a stock proxy image falls back to its
    # default (filtering disabled) and would allow ALL egress — refuse to run the
    # agent in that state rather than hand it an open proxy.
    sleep 2
    if ! docker exec "$pname" grep -q '^FilterDefaultDeny Yes' /etc/tinyproxy/tinyproxy.conf 2>/dev/null; then
      echo "NET_JAIL: egress proxy did not load the allowlist config (would fail open) — aborting." >&2
      docker logs "$pname" 2>&1 | tail -20 >&2 || true
      exit 1
    fi
    local purl="http://$pname:8888"
    PROXY_ENV+=(-e "HTTP_PROXY=$purl"  -e "HTTPS_PROXY=$purl" \
               -e "http_proxy=$purl"  -e "https_proxy=$purl" \
               -e "NO_PROXY=$no_proxy" -e "no_proxy=$no_proxy")
  fi
}

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

# Harness state (checkpoints.sqlite + past.sqlite + session.env) lives OUTSIDE the
# workspace mount, at /project/state, so the agent's file/shell tools (rooted at
# /project/workspace) can't read the past archive or corrupt the live DBs. Backed
# by a host dir under the harness repo, keyed per-workspace so distinct repos keep
# separate archives (mirrors the old per-workspace <workspace>/.deepagents split).
# The Python side reads DEEPAGENTS_STATE_DIR via archive.state_dir. Mirror in run-docker.ps1.
WS_KEY="$(printf '%s' "$WORKSPACE" | sha256sum | cut -c1-12)"
STATE_HOST_DIR="$ROOT/project/state/$WS_KEY"
mkdir -p "$STATE_HOST_DIR"

# Git identity: mount host .gitconfig read-only into the agent user's home (uid 10001 -> /home/agent),
# not /root (container runs USER agent). Never mount ~/.ssh into an autonomous-agent container -
# use a scoped, per-session deploy key or a short-lived token for pushes instead.
GIT_MOUNT=()
if [[ -f "$HOME/.gitconfig" ]]; then
  GIT_MOUNT=(-v "$HOME/.gitconfig:$HOME_DIR/.gitconfig:ro")
fi

# -it gives the REPL prompt loop a TTY. If stdin isn't actually a terminal
# (CI, piped smoke tests), -t fails to allocate and Docker falls back to a
# plain pipe, which the harness already handles via the non-TTY fallback.
TTY_FLAGS="-i"
if [[ -t 0 ]]; then
  TTY_FLAGS="-it"
fi

# Assemble the agent `docker run` invocation into an array. NET_ARGS / PROXY_ENV
# are set either by netjail_up (jail mode) or to the bridge defaults below.
build_agent_run() {
  AGENT_RUN=(docker run --rm $TTY_FLAGS
    "${CAP_FLAGS[@]}"
    "${NET_ARGS[@]}"
    ${USER_FLAGS[@]+"${USER_FLAGS[@]}"}
    --env-file "$ENV_FILE"
    ${PROXY_ENV[@]+"${PROXY_ENV[@]}"}
    -e AGENT_WORKSPACE=/project/workspace
    -e DEEPAGENTS_STATE_DIR=/project/state
    -v "$WORKSPACE:/project/workspace"
    -v "$STATE_HOST_DIR:/project/state"
    ${GIT_MOUNT[@]+"${GIT_MOUNT[@]}"}
    deepagent-harness)
  if [[ $# -gt 0 ]]; then
    AGENT_RUN+=(python3 main.py "$@")
  fi
}

if [[ "${NET_JAIL:-}" == "1" ]]; then
  # Jail mode: bring up sidecars, then run WITHOUT exec so the EXIT trap can tear
  # them down. The agent gets no host route; egress only via the allowlist.
  netjail_up
  build_agent_run "$@"
  "${AGENT_RUN[@]}"
  exit $?
fi

# Default (no jail): host-gateway on the bridge, no proxy.
NET_ARGS=("${HOST_GW[@]}")
PROXY_ENV=()
build_agent_run "$@"
exec "${AGENT_RUN[@]}"
