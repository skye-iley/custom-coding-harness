#!/bin/sh
# git-branch step: assert clean, fetch origin/main, check out the session branch,
# persist session state for git-pr (§3 "compute the session-id once and persist").
set -eu
cd "${DEEPAGENTS_WORKSPACE:-.}"

# Refuse to branch over uncommitted work (§3 clean-tree assertion). Skip (not
# fail) so a dirty workspace just opts out of auto-branching.
if [ -n "$(git status --porcelain)" ]; then
  echo "[workflow git-branch] workspace dirty; skipping auto-branch" >&2
  exit 0
fi

PROVIDER="${DEEPAGENTS_PROVIDER:-cloud}"
# session-id = sha256(user + workspace + timestamp)[:12]. The timestamp makes
# each run unique so branch names never collide across runs on one workspace.
RAW="${USER:-agent}|$(pwd)|$(date +%s)$$"
SID="$(printf '%s' "$RAW" | sha256sum | cut -c1-12)"
BRANCH="agent/${PROVIDER}/${SID}"

git fetch origin main >/dev/null 2>&1 || true
if git show-ref --verify --quiet refs/remotes/origin/main; then
  git checkout -b "$BRANCH" origin/main
else
  git checkout -b "$BRANCH"
fi
BASE="$(git rev-parse HEAD)"

mkdir -p .deepagents
{
  echo "DEEPAGENTS_SESSION_BRANCH=$BRANCH"
  echo "DEEPAGENTS_SESSION_ID=$SID"
  echo "DEEPAGENTS_SESSION_BASE=$BASE"
} > .deepagents/session.env

echo "[workflow git-branch] checked out $BRANCH" >&2
