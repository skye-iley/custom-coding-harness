#!/bin/sh
# git-pr step 1: stage, commit, push the session branch (§3 teardown).
set -eu
cd "${DEEPAGENTS_WORKSPACE:-.}"
. "${DEEPAGENTS_STATE_DIR:-.deepagents}/session.env"

git add -A
# Never commit harness state (checkpoints DB, session.env) or telemetry — §3
# excludes these from the agent's mutations. With DEEPAGENTS_STATE_DIR set the
# state lives outside the workspace and never enters the index at all; the reset
# still guards the default in-workspace `.deepagents` layout.
git reset -q -- .deepagents .agent_telemetry 2>/dev/null || true

# M4: exclude resolved mask set from staging so masked secrets are never pushed.
# Runs mask-scan inside the agent container (state dir is writable here).
if [ "${DEEPAGENTS_MASK:-1}" != "0" ]; then
  mask_scan_output=$(python3 -m harness mask-scan "$PWD" "${DEEPAGENTS_STATE_DIR:-$PWD/.deepagents}" 2>/dev/null) || true
  if [ -n "$mask_scan_output" ]; then
    echo "$mask_scan_output" | while IFS=' ' read -r mode type tier relpath rest; do
      relpath="$(printf '%s' "$relpath" | sed 's/%20/ /g')"
      git reset -q -- "$relpath" 2>/dev/null || true
    done
  fi
fi

if git diff --cached --quiet; then
  echo "[workflow git-pr] no changes to commit" >&2
  exit 0
fi

git commit -m "agent(session-${DEEPAGENTS_SESSION_ID}): automated codebase mutations" >&2

if git push origin "$DEEPAGENTS_SESSION_BRANCH" >&2; then
  echo "[workflow git-pr] pushed $DEEPAGENTS_SESSION_BRANCH" >&2
else
  echo "[workflow git-pr] push failed (no remote/creds?); branch kept locally" >&2
fi
