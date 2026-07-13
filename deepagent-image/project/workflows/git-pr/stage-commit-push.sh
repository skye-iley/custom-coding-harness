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
