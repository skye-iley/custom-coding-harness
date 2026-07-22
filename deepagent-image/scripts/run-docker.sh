#!/usr/bin/env bash
# Run the harness container. Requires project/.env (copy from project/.env.example).
# Consumes the `deepagent-harness` runtime image built by build.sh
# (`docker build --target runtime`) — no test code, no pytest.
#
# Ephemeral workspace:
#   EPHEMERAL=1 ./run-docker.sh "task"        # revert all workspace changes on close
#                                             #   (in-container /refresh pulls live host edits)
#   SAVE_WORKSPACE=1 ./run-docker.sh "task"   # ephemeral + snapshot to workspace-logs/<ts>/
# In ephemeral mode the real workspace is also mounted read-only at
# /project/workspace-src so the /refresh command + refresh_workspace tool can pull
# live host edits into the throwaway copy mid-run (see harness/refresh.py).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/hostmap.sh
source "$ROOT/scripts/lib/hostmap.sh"   # _should_map_host_user / _detect_is_wsl
ENV_FILE="$ROOT/project/.env"
WORKSPACE="${WORKSPACE:-$ROOT/project/workspace}"
SEED_SOURCE="$ROOT/project/workspace"
NETJAIL_DIR="$ROOT/netjail"

# Resource caps (Milestone 1 §3): a Docker host-boundary control so a runaway
# agent can't exhaust the host CPU/RAM or fork-bomb it. NOT a sandbox (the trust
# boundary is still the container; see docs/milestones/mvp.md §5). Override via env:
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

# Host-uid mapping — fixes bind-mount permissions on native Linux, where mounts
# keep host ownership and the image runs as uid 10001 (agent); a state/workspace
# dir owned by your host uid is then unwritable to the agent (the sqlite
# checkpointer crashes on turn 1). Mapping runs the container as your host uid:gid
# so both host-owned mounts become writable. Not needed on Docker Desktop / WSL2 /
# OrbStack (their VM squashes mount ownership via virtiofs/gRPC-FUSE), where
# mapping is mildly harmful (redirects HOME=/tmp, runs as a uid with no matching
# named user).
#
# macOS caveat: auto-detect never maps on Darwin (host uid ≠ the daemon VM's uid,
# so host-uid passthrough is generally wrong there). That is correct for Docker
# Desktop/OrbStack, which squash ownership. A VM whose mount driver *preserves*
# ownership (some colima/lima configs) can still hit the turn-1 crash; the real
# fix there is a driver that squashes or an in-VM chown, not this host-uid map —
# MAP_HOST_USER=1 maps to the macOS uid, which may not match the VM's.
#
# Precedence (MAP_HOST_USER, explicit wins): 1 → force on; 0 → force off;
# unset → auto-map iff the engine is native Linux (not WSL, not Docker Desktop,
# not macOS). HOST_UID/HOST_GID override the detected id -u/-g. The decision is a
# pure function (scripts/lib/hostmap.sh) so it can be unit-tested; here we only
# gather its inputs (uname / /proc / docker info) and act on the result.
USER_FLAGS=()
HOME_DIR="/home/agent"
_uname_s="$(uname -s 2>/dev/null || echo unknown)"
_is_wsl="$(_detect_is_wsl)"
# docker info distinguishes a native-Linux engine from Docker Desktop, and is
# only consulted on the auto-detect path for a non-WSL Linux host — skip the
# daemon round-trip when MAP_HOST_USER is explicit, or on macOS/WSL where the
# uname/proc checks already decide the outcome.
_docker_os="unknown"
if [[ -z "${MAP_HOST_USER:-}" && "$_uname_s" == "Linux" && "$_is_wsl" != "1" ]]; then
  _docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo unknown)"
fi
if [[ "$(_should_map_host_user "$_uname_s" "$_is_wsl" "$_docker_os" "${MAP_HOST_USER:-}")" == "1" ]]; then
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

# Copy $1 -> $2, excluding the heavy, rebuildable workspace conda env (.conda).
# dotglob so dotfiles (.git, .gitignore) are copied; nullglob so an empty dir
# doesn't leave a literal '*'.
copy_workspace() {
  local src="$1" dst="$2" entry base
  mkdir -p "$dst"
  shopt -s dotglob nullglob
  for entry in "$src"/*; do
    base="$(basename "$entry")"
    [[ "$base" == ".conda" ]] && continue
    cp -a "$entry" "$dst/"
  done
  shopt -u dotglob nullglob
}

# Ephemeral teardown: optionally snapshot the throwaway copy, then discard it so
# every workspace change reverts. Idempotent (clears EPHEMERAL_DIR after running).
ephemeral_cleanup() {
  [[ -n "${EPHEMERAL:-}" && -n "${EPHEMERAL_DIR:-}" ]] || return 0
  if [[ -n "${SAVE_WORKSPACE:-}" ]]; then
    local logdir="$ROOT/workspace-logs/$STAMP"
    copy_workspace "$EPHEMERAL_DIR" "$logdir"
    echo "Workspace snapshot saved under $logdir"
  fi
  [[ -d "$EPHEMERAL_DIR" ]] && rm -rf "$EPHEMERAL_DIR"
  EPHEMERAL_DIR=""
  echo "Ephemeral: workspace changes discarded."
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

# Ephemeral workspace: mount a throwaway copy so changes revert on close. The state
# dir below is keyed to the REAL workspace path and stays persistent. SAVE_WORKSPACE
# implies EPHEMERAL and snapshots the copy before it is discarded (ephemeral_cleanup).
EPHEMERAL="${EPHEMERAL:-}"
SAVE_WORKSPACE="${SAVE_WORKSPACE:-}"
[[ -n "$SAVE_WORKSPACE" ]] && EPHEMERAL=1
STAMP="$(date +%Y%m%d-%H%M%S)"
EPHEMERAL_DIR=""
MOUNT_WORKSPACE="$WORKSPACE"
# In ephemeral mode we ALSO bind-mount the real workspace read-only at
# /project/workspace-src, so the in-container /refresh command + refresh_workspace
# tool can pull live host edits into the throwaway copy mid-run (still reverted on
# close). Empty on a normal run, so nothing extra is mounted.
SRC_MOUNT=()
if [[ -n "$EPHEMERAL" ]]; then
  EPHEMERAL_DIR="$ROOT/.ephemeral/$STAMP"
  copy_workspace "$WORKSPACE" "$EPHEMERAL_DIR"
  MOUNT_WORKSPACE="$EPHEMERAL_DIR"
  SRC_MOUNT=(-v "$WORKSPACE:/project/workspace-src:ro"
             -e DEEPAGENTS_WORKSPACE_SRC=/project/workspace-src)
  echo "Ephemeral: on - changes revert on close."
  echo "  Live copy (run tests here): $EPHEMERAL_DIR"
  echo "  /refresh pulls live host edits from $WORKSPACE into the copy."
fi
seed_workspace "$MOUNT_WORKSPACE" "$SEED_SOURCE"

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

# HITL config: .harness-config.yaml is host-local + gitignored (like .env), so it
# is NOT baked into the image. Mount it into /project (the harness CWD) when
# present so its mere presence turns HITL on (cli reads Path.cwd()/.harness-config.yaml).
# Absent => not mounted => HITL stays off (byte-for-byte Milestone 2).
HITL_MOUNT=()
if [[ -f "$ROOT/project/.harness-config.yaml" ]]; then
  HITL_MOUNT=(-v "$ROOT/project/.harness-config.yaml:/project/.harness-config.yaml:ro")
  echo "HITL config: mounted (.harness-config.yaml present)"
fi

# -it gives the REPL prompt loop a TTY. If stdin isn't actually a terminal
# (CI, piped smoke tests), -t fails to allocate and Docker falls back to a
# plain pipe, which the harness already handles via the non-TTY fallback.
TTY_FLAGS="-i"
if [[ -t 0 ]]; then
  TTY_FLAGS="-it"
fi

# M4 mask pre-flight: when DEEPAGENTS_MASK != 0, run a throwaway scan container
# to resolve the mask set, then emit empty overlay mounts for each masked path.
MASK_ARGS=()
EMPTY_FILE=""
EMPTY_DIR=""
# Read a var from project/.env so launcher-side decisions honour the SAME config
# the container sees (DEEPAGENTS_MASK is a container env per §13; the launcher
# gates the host-side scan/overlay on it too). Host/launcher env still wins when
# set — this is only the fallback. Last matching KEY=VALUE line, quotes stripped.
_env_file_get() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^[[:space:]]*${key}=//p" "$ENV_FILE" | tail -1 \
    | sed 's/[[:space:]]*$//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
}

mask_scan() {
  # Launcher env wins; else fall back to project/.env; else default on ("1").
  local mask_mode="${DEEPAGENTS_MASK:-$(_env_file_get DEEPAGENTS_MASK)}"
  mask_mode="${mask_mode:-1}"
  [[ "$mask_mode" == "0" ]] && return 0
  # Forward DEEPAGENTS_MASK_MODE (deny/allow, §13) into the scan container so the
  # resolver honours it — the scan gets no --env-file, so without this the env
  # knob is silently ignored and `allow` degrades to `deny` (under-masking).
  local scan_mode="${DEEPAGENTS_MASK_MODE:-$(_env_file_get DEEPAGENTS_MASK_MODE)}"
  local mode_env=()
  [[ -n "$scan_mode" ]] && mode_env=(-e "DEEPAGENTS_MASK_MODE=$scan_mode")
  local scan_output scan_err
  scan_err="$(mktemp)"
  scan_output="$(docker run --rm \
    -v "$MOUNT_WORKSPACE:/project/workspace:ro" \
    -v "$STATE_HOST_DIR:/project/state" \
    -e DEEPAGENTS_STATE_DIR=/project/state \
    ${mode_env[@]+"${mode_env[@]}"} \
    deepagent-harness python3 -m harness mask-scan 2>"$scan_err")" \
    || { echo "[mask] FATAL: mask-scan failed — refusing to launch unmasked. Fix the scan or set DEEPAGENTS_MASK=0 to disable masking." >&2; [[ -s "$scan_err" ]] && cat "$scan_err" >&2; rm -f "$scan_err"; exit 1; }
  # Surface mask-scan diagnostics (protection-reduction, symlink-escape warnings).
  [[ -s "$scan_err" ]] && cat "$scan_err" >&2
  rm -f "$scan_err"
  [[ -z "$scan_output" ]] && return 0
  EMPTY_FILE="$(mktemp)"
  EMPTY_DIR="$(mktemp -d)"
  local mode type tier relpath source
  while IFS=' ' read -r mode type tier relpath rest; do
    relpath="$(printf '%s' "$relpath" | sed 's/%20/ /g')"
    if [[ "$type" == "dir" ]]; then
      source="$EMPTY_DIR"
    else
      source="$EMPTY_FILE"
    fi
    MASK_ARGS+=(-v "$source:/project/workspace/$relpath:ro")
  done <<< "$scan_output"
  echo "Mask: $((${#MASK_ARGS[@]}/2)) path(s) masked" >&2
}

mask_cleanup() {
  [[ -n "$EMPTY_FILE" && -f "$EMPTY_FILE" ]] && rm -f "$EMPTY_FILE" 2>/dev/null || true
  [[ -n "$EMPTY_DIR" && -d "$EMPTY_DIR" ]] && rm -rf "$EMPTY_DIR" 2>/dev/null || true
}

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
    -v "$MOUNT_WORKSPACE:/project/workspace"
    -v "$STATE_HOST_DIR:/project/state"
    ${SRC_MOUNT[@]+"${SRC_MOUNT[@]}"}
    ${GIT_MOUNT[@]+"${GIT_MOUNT[@]}"}
    ${HITL_MOUNT[@]+"${HITL_MOUNT[@]}"}
    ${MASK_ARGS[@]+"${MASK_ARGS[@]}"}
    deepagent-harness)
  if [[ $# -gt 0 ]]; then
    AGENT_RUN+=(python3 main.py "$@")
  fi
}

mask_scan

# Clean up mask temp files on exit (added to any existing trap).
_exit_cleanup() {
  mask_cleanup
  ephemeral_cleanup
}

if [[ "${NET_JAIL:-}" == "1" ]]; then
  # Jail mode: bring up sidecars, then run WITHOUT exec so the EXIT trap can tear
  # them down. The agent gets no host route; egress only via the allowlist.
  netjail_up
  build_agent_run "$@"
  "${AGENT_RUN[@]}"
  rc=$?
  _exit_cleanup   # netjail's own EXIT trap handles the sidecars
  exit $rc
fi

# Default (no jail): host-gateway on the bridge, no proxy.
NET_ARGS=("${HOST_GW[@]}")
PROXY_ENV=()
build_agent_run "$@"
if [[ -n "$EPHEMERAL" ]]; then
  # Can't exec: the copy must be reverted (and optionally saved) after the run.
  trap "_exit_cleanup" EXIT INT TERM
  "${AGENT_RUN[@]}"
  exit $?
fi
# Can't exec: the mask overlay sources (empty temp file/dir) must outlive the
# container run, then be cleaned up after it exits. exec would replace the shell
# so the EXIT trap never fires; and cleaning up before exec deletes the overlay
# sources out from under `docker run` (docker then fails mounting a dir onto a
# masked file). So run non-exec and let the trap clean up post-run.
trap "mask_cleanup" EXIT INT TERM
"${AGENT_RUN[@]}"
exit $?
