# Follow-up: Interactive Multi-Turn Session (MVP §1a)

Generated while implementing `design_doc_mvp.md` §1a (interactive REPL). Captures decisions made
without user sign-off, things that could not be verified in this environment, and options for
anything that was genuinely a judgment call. Delete sections once resolved/accepted.

## Branch name

Used `feat/interactive-repl-mvp`, not `feat/interactive-repl` — that name was already used (and
merged into `main` at `aa88ade`) for a docs-only PR that hardened the §1a requirements text itself,
without implementing the loop. This PR is the actual code.

## Decisions made (no user sign-off needed, but recording the reasoning)

1. **Ctrl-C "second consecutive" semantics** (`design_doc_mvp.md` §1a, decision bullet 2 / §8 test 8).
   Implemented as: `KeyboardInterrupt` raised *during* `agent.invoke()` is caught, prints
   `[harness] turn cancelled`, and the loop returns to the prompt; `KeyboardInterrupt` raised while
   blocked on `input()` (idle) ends the session, same as EOF. I did not add an explicit "consecutive
   presses" counter — re-reading the spec, "Ctrl-C at idle (or a second consecutive Ctrl-C) ends the
   session" falls out for free: a turn-cancelling Ctrl-C returns to an idle prompt, so an *immediate*
   second Ctrl-C lands on `input()` and is the idle case. No separate state needed.
   - **Alternative:** track a counter and force-end after 2 Ctrl-Cs even if they land mid-turn each
     time (i.e. two cancelled turns in a row also kills the session). Not implemented — the spec
     reads as idle-or-immediate-repeat, not "any 2 ever in a session." If the user wants the stricter
     behavior, it's a small change to `run_repl` in `harness/cli.py`.

2. **Stage marker text** — used the exact strings the design doc lists: `container loading`,
   `building agent`, `thinking`, `reading prompt`, `session closed`, each prefixed `[harness] `.
   No alternative considered; this was specified, not chosen.

3. **Non-interactive multi-line piped stdin** — if stdin is piped but contains multiple lines, the
   harness still only runs **one** turn (the initial/default task) and exits; it does not loop over
   piped lines as if they were prompts. This matches §1a's "non-interactive fallback... runs ... for
   exactly one turn, then exits" — flagging in case multi-turn-via-pipe was actually wanted for some
   CI use case (it isn't in the doc, so I didn't build it).

## Could not verify in this sandbox

This WSL distro has no Docker (`docker` binary exists on the Windows side via Docker Desktop, but
WSL integration isn't enabled for this distro), and no `pip`/installed `deepagents`/`langgraph`/
`dotenv` packages. So:

- **`scripts/build.ps1` / `build.sh`, `verify`, `smoke`, and a real `run-docker` session were not
  run.** I syntax-checked all changed `harness/*.py` files (`py_compile`, clean) and wrote a
  throwaway standalone test (not committed) that stubs out `deepagents`/`langgraph`/`dotenv` and
  drives `run_repl`/`run_turn`/`_is_exit_command` directly with a fake agent, covering: non-TTY
  single turn, interactive turn + `/exit`, Ctrl-C-during-turn survives, Ctrl-C-at-idle ends session,
  EOF ends session, and case-insensitive `/exit`/`/quit` matching. All passed.
- **Action needed from you:** run `.\scripts\build.ps1` then `.\scripts\run-docker.ps1` on the actual
  Windows/PowerShell host (Docker Desktop, WSL integration not required for that path) to confirm
  the real interactive session — especially TTY detection, which the design doc itself flags as
  "host-dependent on Windows (native PowerShell vs. Git-Bash/MSYS vs. piped stdin)" (§9). I cannot
  exercise that from here.
- `scripts/run-docker.ps1` TTY detection uses `[Console]::IsInputRedirected`; `run-docker.sh` uses
  `[[ -t 0 ]]`. Both are best-effort per the doc's own caveat — please confirm on your actual
  PowerShell host that `-it` is requested when you expect an interactive session, and that piping a
  task (e.g. for a CI-style check) still falls back cleanly.

## Pre-existing, unrelated repo state (not touched by this work)

- Every file in `git status` showed as modified before I started, with a 1:1 insertions/deletions
  count per file — this is CRLF/LF line-ending churn (`git diff` on `.gitignore` confirmed: identical
  content, only line endings differ), not real content changes. I did not stage or commit any file
  I didn't otherwise need to touch for §1a; the pre-existing line-ending diffs on untouched files are
  still sitting in your working tree, unstaged, exactly as before.
- Untracked files at repo root (`.bashrc`, `.bash_profile`, `.gitconfig`, `.gitmodules`, `.idea`,
  `.profile`, `.ripgreprc`, `.zprofile`, `.zshrc`) were present before this session and are not part
  of this change — left alone, not added to git.

## What changed for §1a (summary)

- `deepagent-image/project/harness/cli.py` — added `run_repl`, `run_turn`, `_is_exit_command`,
  `_stage`; `main()` now builds the agent once and loops via `run_repl` instead of one `invoke`.
  Empty task + TTY now goes straight to the prompt (previously always ran `DEFAULT_TASK`).
- `deepagent-image/scripts/run-docker.ps1` / `.sh` — request a TTY (`-it`, degrading to `-i` when
  stdin is redirected) so the prompt loop works; `--rm` and the `.env` guard are unchanged.
- `deepagent-image/CLAUDE.md`, root `CLAUDE.md` — run sections updated to describe the persistent
  session instead of the old single-shot flow.
