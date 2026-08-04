#!/bin/sh
# Gate for git-pr: run only if git-branch created a session this run (a
# .deepagents/session.env exists). Pure file check -> trigger.sh (§3).
# exit 0 = run, non-zero = skip.
cd "${DEEPAGENTS_WORKSPACE:-.}" || exit 1
[ -f "${DEEPAGENTS_STATE_DIR:-.deepagents}/session.env" ] || exit 1
exit 0
