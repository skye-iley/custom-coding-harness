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
set -euo pipefail

WS="${AGENT_WORKSPACE:-/project/workspace}"

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
  --bind "$WS" "$WS" \
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
