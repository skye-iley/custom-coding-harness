---
name: git-branch
hook: session.start
gate: trigger.sh
steps:
  - ./create-branch.sh
---
Branch the mounted workspace repo at session start (design_doc.md §3, git
session lifecycle). Asserts a clean tree, fetches origin/main, checks out
`agent/<provider>/<session-id>`, and persists the session id + branch + base
commit to `.deepagents/session.env` for the paired `git-pr` workflow to reuse.

No-op when the workspace is not a git repo (the gate) or is dirty (the step).
Paired with `git-pr` (session.end) — together they are the git session
lifecycle; split into two folders because a workflow binds to one hook point.
