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

# M4 (§15.1): exclude the resolved mask set from staging so masked secrets are
# never pushed. Reads the FROZEN <state>/mask-snapshot.txt written by the
# host-side pre-flight scan at launch — never re-runs mask-scan here. A live
# rescan would reflect whatever .agentignore currently says, and the workspace
# .agentignore is agent-writable; an agent (or prompt injection) could delete a
# mask entry mid-session and a rescan would then fail to exclude a path whose
# content is still frozen-empty in the real mounted fs, letting the empty
# version get committed over the real secret. The snapshot is the one thing
# that can't be tampered with post-launch (state dir is agent-unreachable).
if [ "${DEEPAGENTS_MASK:-1}" != "0" ]; then
  mask_snapshot="${DEEPAGENTS_STATE_DIR:-$PWD/.deepagents}/mask-snapshot.txt"
  if [ -f "$mask_snapshot" ]; then
    while IFS=' ' read -r tier relpath; do
      relpath="${relpath%$(printf '\r')}"  # defensive: strip a stray CR (CRLF-written snapshot)
      [ -n "$relpath" ] || continue
      git reset -q -- "$relpath" 2>/dev/null || true
    done < "$mask_snapshot"
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
