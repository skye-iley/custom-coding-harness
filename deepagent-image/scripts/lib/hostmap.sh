# shellcheck shell=bash
# Host-uid mapping decision (pure) — sourced by run-docker.sh and unit tests.
#
# Native Linux bind mounts preserve real host ownership, so a state/workspace dir
# created by the host user (uid 1000) is unwritable to the image's `agent` user
# (uid 10001) → the sqlite checkpointer crashes on turn 1. Mapping the container
# to the host uid:gid fixes it. WSL2 / Docker Desktop / OrbStack squash mount
# ownership in their VM, so mapping there is unnecessary (and mildly harmful).
#
# macOS (Darwin) is deliberately NOT auto-mapped: the daemon always runs in a
# Linux VM whose uid namespace differs from the macOS host, so host-uid
# passthrough is generally wrong. Docker Desktop/OrbStack squash ownership so no
# map is needed anyway; a colima/lima mount driver that *preserves* ownership can
# still hit the crash, but the fix there is a squashing driver / in-VM chown, not
# this map. See IMMEDIATE_TODO.md for the full write-up.

# _should_map_host_user <uname_s> <is_wsl:0|1> <docker_os> <map_host_user_env>
# Echoes "1" (map to host uid) or "0" (don't). Precedence (explicit wins):
#   env == "1" → force on          env == "0" → force off
#   env unset  → auto: map iff native-Linux engine (Linux, not WSL, not Docker Desktop)
_should_map_host_user() {
  local uname_s="$1" is_wsl="$2" docker_os="$3" env="$4"
  case "$env" in
    1) echo 1; return ;;
    0) echo 0; return ;;
  esac
  # Auto-detect: only a native-Linux engine needs mapping.
  [[ "$uname_s" == "Linux" ]] || { echo 0; return; }
  [[ "$is_wsl" != "1" ]]      || { echo 0; return; }
  case "$docker_os" in
    *"Docker Desktop"*) echo 0; return ;;
  esac
  echo 1
}

# _detect_is_wsl — echoes "1" if the host kernel looks like WSL, else "0".
_detect_is_wsl() {
  if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null \
     || grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; then
    echo 1
  else
    echo 0
  fi
}

# _should_map_host_user_auto — Auto-detect whether to map, collecting all inputs.
_should_map_host_user_auto() {
  local uname_s="$(uname -s 2>/dev/null || echo unknown)"
  local is_wsl="$(_detect_is_wsl)"
  local docker_os="unknown"

  # Only probe docker on native Linux (skip the daemon call otherwise)
  if [[ "$uname_s" == "Linux" && "$is_wsl" != "1" && -z "${MAP_HOST_USER:-}" ]]; then
    docker_os="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo unknown)"
  fi

  _should_map_host_user "$uname_s" "$is_wsl" "$docker_os" "${MAP_HOST_USER:-}"
}
