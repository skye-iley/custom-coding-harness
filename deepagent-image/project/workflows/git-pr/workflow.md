---
name: git-pr
hook: session.end
gate: trigger.sh
steps:
  - ./stage-commit-push.sh
  - ./open-pr.sh
---
At session end, stage and commit the agent's changes, push the session branch,
and open a PR into main — **never auto-merged** (design_doc.md §3 merge policy:
human pre-review is mandatory). Reuses the branch + session id that `git-branch`
persisted to `.deepagents/session.env`.

No-op without a `session.env` (the gate — i.e. git-branch did not run), without
a configured remote (push is skipped, branch kept locally), or without `gh` +
`GH_TOKEN` (PR creation is skipped). Paired with `git-branch` (session.start).
