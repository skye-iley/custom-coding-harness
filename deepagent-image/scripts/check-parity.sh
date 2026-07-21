#!/usr/bin/env bash
# Check .ps1 ↔ .sh script pairs for drift.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

pairs=(
  "build.ps1 build.sh"
  "run-docker.ps1 run-docker.sh"
  "smoke.ps1 smoke.sh"
  "verify.ps1 verify.sh"
  "sync-models.ps1 sync-models.sh"
)

for pair in "${pairs[@]}"; do
  read -r ps1 sh <<< "$pair"
  ps1_path="$ROOT/scripts/$ps1"
  sh_path="$ROOT/scripts/$sh"
  if [[ ! -f "$ps1_path" ]]; then
    echo "MISSING: $ps1_path"
    FAILED=1
    continue
  fi
  if [[ ! -f "$sh_path" ]]; then
    echo "MISSING: $sh_path"
    FAILED=1
    continue
  fi
  # Line-count delta only. Deep content parity is NOT auto-checked: the two
  # scripts express the same docker invocation in different shell syntax
  # (${MountWorkspace} vs $MOUNT_WORKSPACE, /home/agent vs $HOME_DIR, Linux-only
  # `-e HOME=/tmp`), so any text-level cross-shell diff yields false positives.
  # Keep the pairs in sync by review. Mirror of check-parity.ps1.
  ps1_lines=$(wc -l < "$ps1_path")
  sh_lines=$(wc -l < "$sh_path")
  diff=$((ps1_lines - sh_lines))
  echo "$ps1 ($ps1_lines lines) vs $sh ($sh_lines lines) - diff $diff lines"
done

if [[ $FAILED -ne 0 ]]; then
  echo "PARITY CHECK FAILED" >&2
  exit 1
fi
echo "PARITY CHECK OK"
