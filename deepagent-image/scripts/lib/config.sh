#!/usr/bin/env bash
# Shared host-side pre-spinup config resolution (Milestone 5, C3/§7c): host env >
# project/.env ($ENV_FILE) > .harness-profile.yaml ($PROFILE_FILE) > default.
#
# Bash has no separate CLI-flag mechanism from env vars -- `VAR=x ./run-docker.sh`
# already IS the flag -- so this collapses to a two-file fallback under whatever
# the caller's env var already resolved to. Mirror of lib/config.ps1's
# Resolve-HostSetting, which layers a real -Flag tier on top of the same two files.
#
# Callers must set $ENV_FILE and $PROFILE_FILE before sourcing this file.

# Last matching KEY=VALUE line from a dotenv-style file, quotes stripped.
_env_file_get() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^[[:space:]]*${key}=//p" "$ENV_FILE" | tail -1 \
    | sed 's/[[:space:]]*$//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
}

# Last matching `key: value` line from the flat-scalar .harness-profile.yaml.
# A comment-only value ("key:   # note", used throughout the checked-in example
# for every unset key) is unset, not the literal string "# note" -- matches the
# fix in harness/config.py's load_profile (a value that IS a comment isn't one).
_profile_file_get() {
  local key="$1"
  [[ -f "$PROFILE_FILE" ]] || return 0
  sed -n "s/^[[:space:]]*${key}[[:space:]]*:[[:space:]]*//p" "$PROFILE_FILE" | tail -1 \
    | sed 's/[[:space:]]*#.*$//; s/[[:space:]]*$//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
}

# $1 = the value the caller's own env var already resolved to (may be empty),
# $2 = that same var's name (for the .env fallback lookup), $3 = its
# .harness-profile.yaml key, $4 = default. Precedence: env ($1/$2) > .env >
# profile > default.
_resolve_host_setting() {
  local current="$1" env_var="$2" profile_key="$3" default="$4"
  if [[ -n "$current" ]]; then printf '%s' "$current"; return; fi
  local v
  v="$(_env_file_get "$env_var")"
  if [[ -n "$v" ]]; then printf '%s' "$v"; return; fi
  v="$(_profile_file_get "$profile_key")"
  if [[ -n "$v" ]]; then printf '%s' "$v"; return; fi
  printf '%s' "$default"
}
