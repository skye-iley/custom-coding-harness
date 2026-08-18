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
PROFILE_FILE="$ROOT/project/.harness-profile.yaml"
# shellcheck source=lib/config.sh
source "$ROOT/scripts/lib/config.sh"    # _resolve_host_setting (Milestone 5, C3/§7c)
WORKSPACE="${WORKSPACE:-$ROOT/project/workspace}"
SEED_SOURCE="$ROOT/project/workspace"
NETJAIL_DIR="$ROOT/netjail"

# Resource caps (Milestone 1 §3): a Docker host-boundary control so a runaway
# agent can't exhaust the host CPU/RAM or fork-bomb it. NOT a sandbox (the trust
# boundary is still the container; see docs/milestones/mvp.md §5). Override via env:
#   CPUS=4 MEMORY=8g PIDS_LIMIT=1024 ./run-docker.sh "task"
# Milestone 5, C3: .harness-profile.yaml's cpus/memory/pids_limit layer under the
# env var, so `harness config security`'s saved caps actually take effect.
CPUS="$(_resolve_host_setting "${CPUS:-}" CPUS cpus "2")"
MEMORY="$(_resolve_host_setting "${MEMORY:-}" MEMORY memory "4g")"
PIDS_LIMIT="$(_resolve_host_setting "${PIDS_LIMIT:-}" PIDS_LIMIT pids_limit "512")"
CAP_FLAGS=(--cpus "$CPUS" --memory "$MEMORY" --pids-limit "$PIDS_LIMIT")

# Mask mode resolves ONCE here, not inside mask_scan(), because it has two
# consumers: the scan container (which computes the overlay set) and the agent
# container (whose in-container `harness doctor` / mask.resolve re-read the env).
# One resolution, two consumers — the point of lib/config. Mirror of run-docker.ps1.
RESOLVED_MASK_MODE="$(_resolve_host_setting "${DEEPAGENTS_MASK_MODE:-}" DEEPAGENTS_MASK_MODE mask_mode "")"

# NetJail on/off resolves the same way, so a saved `net_jail: true` launches the
# jail without re-typing NET_JAIL=1. Normalized to 1/"" here because everything
# downstream tests `[[ "$NET_JAIL" == "1" ]]`.
NET_JAIL="$(_resolve_host_setting "${NET_JAIL:-}" NET_JAIL net_jail "0")"
case "$NET_JAIL" in
  1 | true | yes | on) NET_JAIL=1 ;;
  *) NET_JAIL="" ;;
esac

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
if [[ "$(_should_map_host_user_auto)" == "1" ]]; then
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
  # Live allowlist if the operator has one, else the tracked .example template.
  # The live pair is gitignored so edits can't pollute a clone; the template
  # carries the shipped defaults so a fresh checkout works with nothing copied.
  # A READ never materializes the live file — only config_cli's write path does
  # (harness/config_cli.py: netjail_read_path / netjail_seed). Mirrored in
  # smoke.sh and both .ps1 twins.
  local services="$NETJAIL_DIR/host-services.txt"
  [[ -f "$services" ]] || services="$NETJAIL_DIR/host-services.txt.example"
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
  [[ -f "$domains" ]] || domains="$NETJAIL_DIR/allowed-domains.txt.example"   # see the services note above
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

# SEED_WORKSPACE=0 turns this off. A benchmark instance (M8 B3) must be exactly
# what its dataset says it is: measured, the seeded environment.yml / .gitignore /
# scripts/run-in-env.sh landed in the extracted patch of every instance, so a
# scorer would have been handed three harness files alongside the fix. An
# instance that needs a conda env ships its own environment.yml in its commit.
# Mirror in run-docker.ps1.
seed_workspace() {
  local target="$1"
  local seed="$2"
  [[ "${SEED_WORKSPACE:-1}" != "0" ]] || return 0
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
# STATE_HOST_DIR overrides the derived location. The benchmark driver (M8 B3)
# sets it per instance so a sweep's telemetry is that instance's and nobody
# else's, and so the driver can read <state-dir>/usage.jsonl back without
# re-deriving a hash the launcher owns. Mirror in run-docker.ps1.
WS_KEY="$(printf '%s' "$WORKSPACE" | sha256sum | cut -c1-12)"
STATE_HOST_DIR="${STATE_HOST_DIR:-$ROOT/project/state/$WS_KEY}"
mkdir -p "$STATE_HOST_DIR"

# Git identity: mount host .gitconfig read-only into the agent user's home (uid 10001 -> /home/agent),
# not /root (container runs USER agent). Never mount ~/.ssh into an autonomous-agent container -
# use a scoped, per-session deploy key or a short-lived token for pushes instead.
GIT_MOUNT=()
if [[ -f "$HOME/.gitconfig" ]]; then
  GIT_MOUNT=(-v "$HOME/.gitconfig:$HOME_DIR/.gitconfig:ro")
fi

# AUTONOMY: write/update autonomy_level in .harness-config.yaml before the mount
# check below sees it. Plain text edit (no YAML parser, matching every other
# host-side scrape/write in this script) -- replace the existing
# `autonomy_level:` line if present, else prepend one (creating the file if it
# doesn't exist yet). An imperative action, not a resolved Settings field --
# HITL's presence-of-file-turns-it-on design (M3) means this necessarily turns
# HITL on if it wasn't already; that's the point, not a side effect.
HITL_CONFIG_PATH="$ROOT/project/.harness-config.yaml"
if [[ -n "${AUTONOMY:-}" ]]; then
  case "$AUTONOMY" in
    strict | guided | autonomous) ;;
    *)
      echo "[harness] FATAL: AUTONOMY must be one of strict|guided|autonomous, got '$AUTONOMY'" >&2
      exit 1
      ;;
  esac
  if [[ -f "$HITL_CONFIG_PATH" ]] && grep -q '^[[:space:]]*autonomy_level[[:space:]]*:' "$HITL_CONFIG_PATH"; then
    sed -i.bak "s/^[[:space:]]*autonomy_level[[:space:]]*:.*/autonomy_level: $AUTONOMY/" "$HITL_CONFIG_PATH"
    rm -f "$HITL_CONFIG_PATH.bak"
  elif [[ -f "$HITL_CONFIG_PATH" ]]; then
    { echo "autonomy_level: $AUTONOMY"; cat "$HITL_CONFIG_PATH"; } > "$HITL_CONFIG_PATH.tmp"
    mv "$HITL_CONFIG_PATH.tmp" "$HITL_CONFIG_PATH"
  else
    echo "autonomy_level: $AUTONOMY" > "$HITL_CONFIG_PATH"
  fi
  echo "HITL: autonomy_level set to '$AUTONOMY' in .harness-config.yaml (turns HITL on for this run if it wasn't already)"
fi

# HITL config: .harness-config.yaml is host-local + gitignored (like .env), so it
# is NOT baked into the image. Mount it into /project (the harness CWD) when
# present so its mere presence turns HITL on (cli reads Path.cwd()/.harness-config.yaml).
# Absent => not mounted => HITL stays off (byte-for-byte Milestone 2).
HITL_MOUNT=()
if [[ -f "$HITL_CONFIG_PATH" ]]; then
  HITL_MOUNT=(-v "$HITL_CONFIG_PATH:/project/.harness-config.yaml:ro")
  echo "HITL config: mounted (.harness-config.yaml present)"
fi

# Unified config profile (Milestone 5, C4): same story as .harness-config.yaml --
# gitignored, so NOT baked into the image; mount it into /project (the harness
# CWD) so the container's resolve_settings() sees the same profile tier the host
# side just resolved against. Without this the profile's in-session fields
# (topic/max_cost/max_tokens) are silently ignored on every containerized run and
# `/config save` writes into the throwaway container layer.
#
# Read-WRITE, unlike the HITL mount: `/config save` is a documented in-session
# action that must land on the host. The agent can't reach it -- its file tools
# are rooted at /project/workspace and the bwrap jail (slice H) binds /project
# read-only.
PROFILE_MOUNT=()
if [[ -f "$PROFILE_FILE" ]]; then
  PROFILE_MOUNT=(-v "$PROFILE_FILE:/project/.harness-profile.yaml")
  echo "Config profile: mounted (.harness-profile.yaml present)"
fi

# Host-only knobs the container cannot otherwise observe: --cpus/--memory/
# --pids-limit/NetJail are `docker run` flags, never env vars, so without these
# the in-session `/config` read-only view and `harness doctor` would report the
# built-in defaults no matter what this launch actually applied. Informational
# only -- nothing in the container acts on them.
CAP_ENV=(
  -e "CPUS=$CPUS"
  -e "MEMORY=$MEMORY"
  -e "PIDS_LIMIT=$PIDS_LIMIT"
  -e "NET_JAIL=${NET_JAIL:-0}"
)

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

mask_scan() {
  # Enable/disable (DEEPAGENTS_MASK) deliberately gets NO profile-file tier --
  # it's a debugging escape hatch (config.py's Settings.mask_enabled is excluded
  # from the profile on purpose), not something to casually flip via a saved
  # default. Host/launcher env wins, else project/.env, else default on ("1") --
  # unchanged from pre-M5, honouring the SAME config the container sees (§13).
  local mask_mode="${DEEPAGENTS_MASK:-$(_env_file_get DEEPAGENTS_MASK)}"
  mask_mode="${mask_mode:-1}"
  [[ "$mask_mode" == "0" ]] && return 0
  # Forward DEEPAGENTS_MASK_MODE (deny/allow, §13) into the scan container so the
  # resolver honours it — the scan gets no --env-file, so without this the env
  # knob is silently ignored and `allow` degrades to `deny` (under-masking).
  # Milestone 5, C3: .harness-profile.yaml's mask_mode now layers on top of the
  # same host-env / .env fallback this always had. Resolved once at the top of the
  # script (RESOLVED_MASK_MODE), shared with the agent container.
  local mode_env=()
  [[ -n "${RESOLVED_MASK_MODE:-}" ]] && mode_env=(-e "DEEPAGENTS_MASK_MODE=$RESOLVED_MASK_MODE")
  # USER_FLAGS applies here too, not just to the agent container. The scan WRITES
  # mask-snapshot.txt into $STATE_HOST_DIR, which the host user just created via
  # mkdir -p; unmapped, the scan runs as the image's uid 10001 and gets EACCES.
  # The launcher then fails closed and refuses to launch at all — so on a native
  # Linux engine, omitting the map here makes masking (the default) unusable.
  # Invisible on Docker Desktop/WSL2, which squash mount ownership.
  local scan_output scan_err
  scan_err="$(mktemp)"
  scan_output="$(docker run --rm \
    -v "$MOUNT_WORKSPACE:/project/workspace:ro" \
    -v "$STATE_HOST_DIR:/project/state" \
    -e DEEPAGENTS_STATE_DIR=/project/state \
    ${USER_FLAGS[@]+"${USER_FLAGS[@]}"} \
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

# M4 slice H: the bwrap fs jail needs the narrow seccomp profile, because Docker's
# default profile blocks unprivileged user-namespace creation (see seccomp/README.md).
# Off by default (§13) — enabling it trades a little outer-boundary attack surface
# for a real inner boundary, so it is the operator's explicit call. Fail closed: if
# the jail is asked for and the profile is missing, refuse to launch rather than run
# unjailed while the operator believes otherwise.
JAIL_ARGS=()
jail_setup() {
  # Milestone 5, C3: .harness-profile.yaml's jail now layers on top of the same
  # host-env / .env fallback this always had.
  local jail_mode
  jail_mode="$(_resolve_host_setting "${DEEPAGENTS_JAIL:-}" DEEPAGENTS_JAIL jail "0")"
  case "${jail_mode:-0}" in
    0 | false | no | off | "") return 0 ;;
  esac
  local profile="$ROOT/seccomp/userns.json"
  if [[ ! -f "$profile" ]]; then
    echo "[jail] FATAL: DEEPAGENTS_JAIL is on but $profile is missing — refusing to launch unjailed. Run 'python3 -m harness seccomp-sync' or set DEEPAGENTS_JAIL=0." >&2
    exit 1
  fi
  JAIL_ARGS=(--security-opt "seccomp=$profile")
  # Same decision must turn on the relaxation AND the in-container jail — see the
  # comment in run-docker.ps1. jail.jail_enabled() reads the env, not Settings, so
  # without this a profile/env-resolved jail relaxes five syscalls container-wide,
  # starts no bwrap re-exec, and leaves nsguard (which tracks DEEPAGENTS_JAIL) off.
  JAIL_ARGS+=(-e "DEEPAGENTS_JAIL=1")
  echo "Jail: bwrap fs jail ON (narrow seccomp profile)" >&2

  # M4 slice J (§11.6): seccomp is only ONE of THREE gates (the third — the kernel's
  # procfs restriction — is handled further down, M4.1 §13.7). On an AppArmor host
  # (Ubuntu/Debian Docker) the generated `docker-default` profile carries a literal
  # `deny mount,`, so bwrap gets past `unshare` and then fails at its first mount —
  # and entering a user namespace does not shed AppArmor confinement, so nothing the
  # jail does from inside can work around it. Slice J vendors a narrowed profile
  # (apparmor/deepagent-userns) that keeps every other docker-default rule.
  #
  # Unset (default): select the narrowed profile and PROBE that the daemon will
  # accept it, before launching anything real. Mirror of run-docker.ps1.
  # Milestone 5, C3: .harness-profile.yaml's jail_apparmor now layers on top of
  # the same host-env / .env fallback this always had.
  local apparmor
  apparmor="$(_resolve_host_setting "${DEEPAGENTS_JAIL_APPARMOR:-}" DEEPAGENTS_JAIL_APPARMOR jail_apparmor "")"
  if [[ -z "$apparmor" ]]; then
    # NOT `$(...)`: the autoselect fails closed with `exit 1`, which inside a
    # command substitution would only kill the subshell and let the launch proceed.
    APPARMOR_CHOICE=""
    _apparmor_autoselect
    apparmor="$APPARMOR_CHOICE"
  fi
  if [[ -n "$apparmor" ]]; then
    JAIL_ARGS+=(--security-opt "apparmor=$apparmor")
    if [[ "$apparmor" == "unconfined" ]]; then
      echo "Jail: AppArmor DISABLED for this container (apparmor=unconfined)." >&2
      echo "      This drops ALL of docker-default — the /proc and /sys write denials and the" >&2
      echo "      ptrace peer restriction — not just its deny-mount rule. Wider than the five" >&2
      echo "      relaxed syscalls DEEPAGENTS_JAIL alone costs. See apparmor/README.md." >&2
    else
      echo "Jail: AppArmor profile '$apparmor' (loaded on the Docker daemon's host)." >&2
    fi
  fi

  # M4.1 fork J5 (§13.7): the THIRD gate. With seccomp and the LSM both satisfied,
  # the kernel still refuses bwrap's fresh `--proc` while the container's own procfs
  # is covered by Docker's maskedPaths/readonlyPaths -- mount_too_revealing(), EPERM,
  # no LSM denial. Docker offers no partial unmask, so the only lever is dropping all
  # 13 masks for this container.
  #
  # The honest trade (§14 J5): unlike seccomp=unconfined or apparmor=unconfined --
  # both rejected -- this drops a mechanism that is NOT protecting the jailed process.
  # The re-exec happens before anything heavy loads, and inside the jail /proc is
  # bwrap's own fresh procfs, which never carried these masks. The kernel checks the
  # dangerous targets (/proc/kcore, /proc/sysrq-trigger) against capabilities in the
  # INITIAL user namespace, which no container process holds.
  #
  # The load-bearing fact, and the one that holds on a host with NO AppArmor (Docker
  # Desktop/WSL2, SELinux, LSM-less): the image runs as USER agent, non-root. The
  # unmasked targets are root-owned and mode 0400/0200 -- /proc/kcore unreadable,
  # /proc/sysrq-trigger unwritable -- so dropping the masks does not hand them to the
  # agent. On an AppArmor host the profile's `deny @{PROC}/sysrq-trigger rwklx,` and
  # `deny @{PROC}/kcore rwklx,` (carried through byte-for-byte) are a second layer.
  # Residual exposure is world-readable /proc entries in the window before the re-exec
  # and anything running outside the jail -- narrow, not zero, which is why the
  # launcher says what it gave up.
  local systempaths
  systempaths="$(_resolve_host_setting "${DEEPAGENTS_JAIL_SYSTEMPATHS:-}" DEEPAGENTS_JAIL_SYSTEMPATHS jail_systempaths "unconfined")"
  if [[ "$systempaths" == "unconfined" ]]; then
    JAIL_ARGS+=(--security-opt "systempaths=unconfined")
    echo "Jail: Docker's /proc masks DROPPED for this container (systempaths=unconfined)." >&2
    echo "      Required on a stock Linux host: the kernel refuses bwrap's fresh /proc while" >&2
    echo "      they cover the container's procfs. Inside the jail /proc is bwrap's own, which" >&2
    echo "      never carried them. Set DEEPAGENTS_JAIL_SYSTEMPATHS=default to keep them (the" >&2
    echo "      jail then fails to start on most Linux hosts). See milestone4.1.md §13.7." >&2
  else
    echo "Jail: keeping Docker's /proc masks (DEEPAGENTS_JAIL_SYSTEMPATHS=$systempaths)." >&2
    echo "      Expect bwrap to fail at 'Can't mount proc ... Operation not permitted' unless" >&2
    echo "      this host is Docker Desktop/WSL2." >&2
  fi
}

# Does the daemon accept this AppArmor profile? Asks the DAEMON rather than reading
# /sys/kernel/security/apparmor/profiles locally: the profile must be loaded on the
# machine running dockerd, which for a remote daemon / Colima-Lima VM / WSL distro is
# not this machine — a local read would need root AND would lie in exactly those cases.
_apparmor_profile_available() {
  docker run --rm --security-opt "apparmor=$1" deepagent-harness true >/dev/null 2>&1
}

# What AppArmor profile does an ordinary container get here? Empty ⇒ this daemon's
# host loads no AppArmor policy (Docker Desktop / WSL2 / macOS), so the jail needs
# no profile at all.
_apparmor_in_force() {
  docker run --rm deepagent-harness sh -c \
    'cat /proc/self/attr/apparmor/current 2>/dev/null || cat /proc/self/attr/current 2>/dev/null || true' \
    2>/dev/null | tr -d '\0' | sed 's/ (.*//' | tr -d '[:space:]'
}

# Pick the AppArmor stance when the operator set no explicit one. Sets
# APPARMOR_CHOICE (empty = pass nothing) and must be called WITHOUT a subshell so
# its fail-closed `exit` actually aborts the launch. Fails closed on purpose:
# never silently fall back to apparmor=unconfined, which is a categorically wider
# trade (a whole LSM off, vs. five relaxed syscalls) that only an operator may make.
APPARMOR_CHOICE=""
_apparmor_autoselect() {
  local profile="deepagent-userns"
  APPARMOR_CHOICE=""

  # Order matters, and not for performance. A daemon with no AppArmor support
  # ACCEPTS `--security-opt apparmor=<anything>` and ignores it (measured on
  # Docker Desktop/WSL2), so probing first would "succeed" against a profile that
  # is not loaded anywhere and make the launcher announce a boundary that does not
  # exist. Ask what actually confines a container here before asking for a profile.
  local in_force
  in_force="$(_apparmor_in_force)"
  case "$in_force" in
    "" | unconfined | kernel)
      # No LSM on the daemon's host — nothing to relax, pass nothing.
      return 0
      ;;
  esac

  if _apparmor_profile_available "$profile"; then
    APPARMOR_CHOICE="$profile"
    return 0
  fi
  echo "[jail] FATAL: DEEPAGENTS_JAIL is on and this daemon confines containers with" >&2
  echo "       AppArmor profile '$in_force', whose 'deny mount,' blocks bwrap at its first" >&2
  echo "       mount (seccomp is NOT the problem — see apparmor/README.md). The narrowed" >&2
  echo "       profile '$profile' is not loaded on the Docker daemon's host." >&2
  echo "       Load it:      sudo $ROOT/scripts/install-apparmor-profile.sh" >&2
  echo "       Wider trade:  DEEPAGENTS_JAIL_APPARMOR=unconfined  (drops ALL of $in_force)" >&2
  echo "       Or:           DEEPAGENTS_JAIL=0" >&2
  exit 1
}
jail_setup

# Milestone 5, C3: DEEPAGENTS_MODEL / .harness-profile.yaml's model, forwarded
# as an explicit -e so it reaches the container even when it's not in
# project/.env (docker prefers an explicit -e over the same var in
# --env-file, so this wins regardless of what .env also says).
MODEL_ARGS=()
RESOLVED_MODEL="$(_resolve_host_setting "${DEEPAGENTS_MODEL:-}" DEEPAGENTS_MODEL model "")"
[[ -n "$RESOLVED_MODEL" ]] && MODEL_ARGS=(-e "DEEPAGENTS_MODEL=$RESOLVED_MODEL")

# Same for the resolved mask mode: the scan container already gets it (it computes
# the overlay set), but the AGENT container never did, so an in-container
# `harness doctor` re-ran mask.resolve against an unset env and reported `deny` on
# an `allow` launch. Enforcement is unaffected either way — the jail's overmounts
# read the frozen mask-snapshot.txt, not a fresh resolve — this is about the two
# halves reporting the same mode.
MASK_MODE_ARGS=()
[[ -n "${RESOLVED_MASK_MODE:-}" ]] && MASK_MODE_ARGS=(-e "DEEPAGENTS_MASK_MODE=$RESOLVED_MASK_MODE")

# Milestone 7: the raw-trace mode. Forwarded explicitly for the same reason
# DEEPAGENTS_JAIL is (milestone5.md §0.1) -- a knob resolved on the HOST from a
# flag/host env/profile that the container never sees is a knob the operator set
# and the harness ignored, silently. --env-file alone only covers the .env tier.
RAW_TRACE_ARGS=()
RESOLVED_RAW_TRACE="$(_resolve_host_setting "${DEEPAGENTS_RAW_TRACE:-}" DEEPAGENTS_RAW_TRACE raw_trace "")"
if [[ -n "$RESOLVED_RAW_TRACE" && "$RESOLVED_RAW_TRACE" != "off" ]]; then
  RAW_TRACE_ARGS=(-e "DEEPAGENTS_RAW_TRACE=$RESOLVED_RAW_TRACE")
  echo "[raw-trace] mode=$RESOLVED_RAW_TRACE — the trace holds the full prompt context;" >&2
  echo "            treat <state-dir>/raw-trace/<run_id>.log as a secret-bearing artifact." >&2
fi

# Assemble the agent `docker run` invocation into an array. NET_ARGS / PROXY_ENV
# are set either by netjail_up (jail mode) or to the bridge defaults below.
build_agent_run() {
  AGENT_RUN=(docker run --rm $TTY_FLAGS
    ${JAIL_ARGS[@]+"${JAIL_ARGS[@]}"}
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
    ${PROFILE_MOUNT[@]+"${PROFILE_MOUNT[@]}"}
    ${MASK_ARGS[@]+"${MASK_ARGS[@]}"}
    ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"}
    ${MASK_MODE_ARGS[@]+"${MASK_MODE_ARGS[@]}"}
    ${RAW_TRACE_ARGS[@]+"${RAW_TRACE_ARGS[@]}"}
    "${CAP_ENV[@]}"
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
