#!/bin/sh
# Gate for git-branch: run only inside a git work tree. A pure git/file check
# (no live in-memory state) -> trigger.sh is the right gate kind (§3).
# exit 0 = run the steps, non-zero = skip.
cd "${DEEPAGENTS_WORKSPACE:-.}" || exit 1
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 1
exit 0
