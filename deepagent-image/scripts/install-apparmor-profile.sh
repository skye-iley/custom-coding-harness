#!/usr/bin/env bash
# Load the harness's narrowed AppArmor profile into this machine's kernel (M4 slice J).
#
#   sudo ./install-apparmor-profile.sh              # load / replace (enforce mode)
#   sudo ./install-apparmor-profile.sh --uninstall  # remove it
#        ./install-apparmor-profile.sh --status     # is it loaded? which sha?
#
# WHY THIS EXISTS AS A SEPARATE STEP. A seccomp profile is a file whose *path* is
# handed to the daemon at `docker run` — it needs no host state. An AppArmor
# profile is not: it must be compiled into the host kernel by root BEFORE the
# container starts, and `--security-opt apparmor=deepagent-userns` merely
# references an already-loaded profile by name. See apparmor/README.md.
#
# WHERE TO RUN IT: on the machine running **dockerd**, not necessarily the machine
# running the docker CLI. A remote daemon, a Colima/Lima VM, or a WSL distro each
# has its own kernel; loading the profile here does nothing for those.
#
# Linux/AppArmor only, so there is deliberately no .ps1 twin (same exception as
# scripts/jail-check.py). Do NOT add it to check-parity.sh's `pairs` array.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_PATH="$ROOT/apparmor/deepagent-userns"
PROFILE_NAME="deepagent-userns"
APPARMOR_SECURITYFS="/sys/kernel/security/apparmor"

action="load"
case "${1:-}" in
  "") action="load" ;;
  --uninstall | --remove) action="uninstall" ;;
  --status) action="status" ;;
  -h | --help)
    sed -n '2,20p' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  *)
    echo "usage: install-apparmor-profile.sh [--uninstall|--status]" >&2
    exit 2
    ;;
esac

profile_sha() {
  # Deliberately NOT reimplemented in shell. The hash is reported so a stale load
  # is diagnosable by eye (milestone4.1.md §13.2) — the kernel exposes a loaded
  # profile's NAME but never its content — and a diagnostic that disagrees with
  # the generator is worse than none. An awk version of this drifted immediately:
  # AppArmor's `#include <tunables/global>` is the profile's first functional
  # line, not a header comment, and the two implementations disagreed about it.
  # Same rule as the mask matcher (milestone4.md §4 slice A): one implementation,
  # in Python, called from shell.
  local py
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
      PYTHONPATH="$ROOT/project" "$py" -c '
import sys
from pathlib import Path
from harness.apparmor import body_sha256, load_text
print(body_sha256(load_text(Path(sys.argv[1]))))
' "$PROFILE_PATH" 2>/dev/null && return 0
    fi
  done
  echo "(needs python3 to compute)"
}

recorded_sha() {
  sed -n 's/^#[[:space:]]*holder-profile-sha256:[[:space:]]*\([0-9a-f]\{64\}\).*/\1/p' \
    "$PROFILE_PATH" | head -n1
}

# --- environment gates -------------------------------------------------------

if [[ ! -f "$PROFILE_PATH" ]]; then
  echo "[apparmor] missing $PROFILE_PATH — run 'python3 -m harness apparmor-sync'" >&2
  exit 1
fi

have_apparmor=1
[[ -d "$APPARMOR_SECURITYFS" ]] || have_apparmor=0

# --status is purely informational, so it answers even where there is no AppArmor
# to talk to -- the sha it prints is how a stale load is diagnosed (§13.2), and
# that is worth knowing from any host.
if [[ "$action" == "status" ]]; then
  echo "[apparmor] profile file : $PROFILE_PATH"
  echo "[apparmor] recorded sha : $(recorded_sha)"
  echo "[apparmor] on-disk sha  : $(profile_sha)"
  if [[ "$have_apparmor" -eq 0 ]]; then
    echo "[apparmor] host        : no AppArmor ($APPARMOR_SECURITYFS absent) — nothing to load"
    exit 0
  fi
  if [[ -r "$APPARMOR_SECURITYFS/profiles" ]]; then
    if grep -q "^$PROFILE_NAME " "$APPARMOR_SECURITYFS/profiles" 2>/dev/null; then
      echo "[apparmor] loaded      : yes — $(grep "^$PROFILE_NAME " "$APPARMOR_SECURITYFS/profiles" | head -n1)"
    else
      echo "[apparmor] loaded      : NO (run: sudo $0)"
    fi
  else
    echo "[apparmor] loaded      : unknown (need root to read $APPARMOR_SECURITYFS/profiles)"
  fi
  echo "[apparmor] NOTE: the kernel reports a loaded profile's name, never its content."
  echo "           Re-run this installer after every 'apparmor-sync' — a stale load is"
  echo "           otherwise indistinguishable from a current one."
  exit 0
fi

if [[ "$have_apparmor" -eq 0 ]]; then
  # Not a failure the operator has to fix: Docker Desktop / WSL2 / macOS load no
  # AppArmor policy at all, so the jail needs nothing here.
  echo "[apparmor] this host has no AppArmor ($APPARMOR_SECURITYFS absent)."
  echo "           Nothing to load — DEEPAGENTS_JAIL=1 works here without a profile."
  exit 2
fi

if ! command -v apparmor_parser >/dev/null 2>&1; then
  echo "[apparmor] apparmor_parser not found. Install it:" >&2
  echo "           Debian/Ubuntu:  sudo apt-get install apparmor apparmor-utils" >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  # Deliberately does not self-escalate: a tool whose whole purpose is a trust
  # boundary should not silently run things as root on the operator's behalf.
  echo "[apparmor] this must run as root. Re-run:" >&2
  echo "           sudo $0 ${1:-}" >&2
  exit 1
fi

# --- act ---------------------------------------------------------------------

case "$action" in
  load)
    # -r replaces an already-loaded profile, so this is idempotent. -W caches the
    # compiled policy. Enforce mode is the default and is the point: -C (complain)
    # would log violations and allow them, which `harness doctor` reports as an
    # error precisely because it looks like success (invariant 40).
    apparmor_parser -r -W "$PROFILE_PATH"
    echo "[apparmor] loaded '$PROFILE_NAME' (enforce) from $PROFILE_PATH"
    echo "[apparmor] sha: $(profile_sha)"
    echo "[apparmor] run-docker selects it automatically when DEEPAGENTS_JAIL=1."
    ;;
  uninstall)
    apparmor_parser -R "$PROFILE_PATH"
    echo "[apparmor] unloaded '$PROFILE_NAME'."
    echo "[apparmor] DEEPAGENTS_JAIL=1 will now fail closed on this host until it is"
    echo "           reloaded, or DEEPAGENTS_JAIL_APPARMOR=unconfined is set."
    ;;
esac
