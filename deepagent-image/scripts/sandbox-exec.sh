#!/usr/bin/env bash
# Two-phase bubblewrap sandbox for agent-triggered commands.
#
#   sandbox-exec install -- <cmd...>   network ALLOWED  (pip/conda/npm dependency resolution)
#   sandbox-exec exec    -- <cmd...>   network DENIED   (run tests / agent-authored code)
#
# The workspace ($AGENT_WORKSPACE, default /project/workspace) is the only writable bind;
# system paths are mounted read-only. The execution phase removes the network namespace so
# agent code cannot exfiltrate; the install phase keeps it so dependencies can be fetched.
#
# Note: bwrap nested in Docker needs unprivileged user namespaces (see design_doc.md §2).
# If the host disallows them this exits non-zero rather than running unsandboxed.
#
# AGENT_BIND_SCOPE (M9, milestone9.md §2/§6): optional "relpath:mode,relpath:mode"
# list, mirroring harness/profile.py's AgentProfile on the Python side of the other
# bwrap seam. Unset -> the single hardcoded workspace bind below, unchanged (no
# selection surface feeds this var yet -- see milestone9.md §6). A malformed entry
# is a hard failure: it must never silently fall back to binding the whole workspace,
# which would be a widening disguised as a parse error (invariant 10).
set -euo pipefail

WS="${AGENT_WORKSPACE:-/project/workspace}"

ws_bind_args=()
if [[ -n "${AGENT_BIND_SCOPE:-}" ]]; then
  IFS=',' read -ra _scope_entries <<< "$AGENT_BIND_SCOPE"
  for entry in "${_scope_entries[@]}"; do
    relpath="${entry%%:*}"
    mode="${entry##*:}"
    if [[ -z "$relpath" || -z "$mode" || "$entry" != "$relpath:$mode" ]]; then
      echo "sandbox-exec: malformed AGENT_BIND_SCOPE entry: ${entry@Q}" >&2
      exit 2
    fi
    case "$mode" in
      rw) flag="--bind" ;;
      ro) flag="--ro-bind" ;;
      *)
        echo "sandbox-exec: AGENT_BIND_SCOPE mode must be rw or ro, got: ${mode@Q}" >&2
        exit 2
        ;;
    esac
    target="$WS/$relpath"
    ws_bind_args+=("$flag" "$target" "$target")
  done
else
  ws_bind_args=(--bind "$WS" "$WS")
fi

phase="${1:-}"
shift || true
if [[ "${1:-}" == "--" ]]; then
  shift
fi

case "$phase" in
  install) net_args=() ;;               # keep the network namespace (omit --unshare-net)
  exec)    net_args=(--unshare-net) ;;  # no NIC, no loopback
  *)
    echo "usage: sandbox-exec {install|exec} -- <command...>" >&2
    exit 2
    ;;
esac

if [[ $# -eq 0 ]]; then
  echo "sandbox-exec: no command given" >&2
  exit 2
fi

if ! command -v bwrap >/dev/null 2>&1; then
  echo "sandbox-exec: bubblewrap (bwrap) not installed in image" >&2
  exit 127
fi

exec bwrap \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind /sbin /sbin \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /etc /etc \
  --ro-bind /opt /opt \
  "${ws_bind_args[@]}" \
  --dir /tmp \
  --proc /proc \
  --dev /dev \
  --unshare-user \
  --unshare-ipc \
  --unshare-pid \
  --unshare-uts \
  "${net_args[@]}" \
  --chdir "$WS" \
  "$@"
