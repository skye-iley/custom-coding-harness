# shellcheck shell=bash
# Host-uid mapping decision (pure) — sourced by run-docker.sh and unit tests.
#
# Native Linux bind mounts preserve real host ownership, so a state/workspace dir
# created by the host user (uid 1000) is unwritable to the image's `agent` user
# (uid 10001) → the sqlite checkpointer crashes on turn 1. Mapping the container
# to the host uid:gid fixes it. WSL2 / Docker Desktop / macOS squash mount
# ownership in their VM, so mapping there is unnecessary (and mildly harmful).
# See IMMEDIATE_TODO.md for the full write-up.

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
