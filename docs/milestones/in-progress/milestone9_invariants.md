# Milestone 9 — Invariants

> Test-facing companion to `milestone9.md` (same folder). Kept **separate** while M9 is
> in-progress so these checkable properties drive testing without the planning prose around them.
> On completion this folds into `milestone9.md` as a section and the standalone file is dropped
> (`docs/README.md` milestone lifecycle).
>
> **Status: written before the code**, on the M6/M7/M8 precedent. This milestone edits the one
> function (`jail.bwrap_args`) and the one script (`sandbox-exec.sh`) that stand between "every
> agent trusts the whole workspace" and "an agent trusts what its profile says" — a mistake here is
> a silent widening of the M4 slice H boundary, not a feature bug. Every invariant below exists to
> make a silent widening a test failure instead of a code-review judgement call.

M9 = the bind list both bwrap seams emit becomes data (`AgentProfile`) instead of one hardcoded
line each, with the shipped default byte-identical to today. The invariants split five ways:
**default-behavior preservation** (nothing changes for the one agent that exists today),
**bind construction** (the object says what it means), **ordering** (a scoped profile cannot
reopen what masking already closed), **script parity** (the shell seam matches the Python seam's
semantics), and **removability**.

The rule the milestone rests on, stated once: **a profile that is not the default must narrow
what an agent can reach, never widen it past what the docker mask/floor already permits, and the
default must not be distinguishable from today's hardcoded behavior by anything that reads the
argv.** Every invariant below is that rule applied to one seam.

## Default-behavior preservation (the one agent running today sees no change)

1. **`bwrap_args()` called with no `profile` argument produces byte-identical argv to the
   pre-M9 function.** Every existing call site (`jail.maybe_reexec`, `jail.preflight`, and every
   pre-existing test in `tests/test_jail.py`) passes no `profile` and must not need editing.
   Asserted by running the pre-M9 argv-shape tests unmodified against the post-M9 function.

2. **`DEFAULT_PROFILE`'s single bind entry (`relpath="."`, `mode="rw"`) resolves to exactly the
   one line it replaces**: `["--bind", str(workspace), str(workspace)]`. Not "an equivalent
   line" — the identical two-element pair, in the identical position in the argv (between the
   `project_root` read-only bind and the state-dir bind).

3. **`sandbox-exec.sh` with `AGENT_BIND_SCOPE` unset emits the unmodified line**
   `--bind "$WS" "$WS"`. The new loop is only reached when the variable is set; unset must not
   even evaluate the loop body.

## Bind construction (the object says what it means)

4. **`BindEntry.relpath` is workspace-relative and cannot express an absolute or `..`-escaping
   path.** Enforced at construction (raise, not a silently-clamped value) — this is the direct
   mitigation for the `design_doc.md` §10 "Sandbox Escape (Dynamic Binds)" risk ("An incorrectly
   configured `HarnessProfile` creates a bind mount that allows access to the host's `/root` or
   `/etc`"). Asserted by constructing a `BindEntry` with `/etc` and with `../../etc` and expecting
   both to fail before any bwrap argv is ever built — the earliest point of entry, same principle
   M5.1 applied to enum knobs.

5. **`mode` is per-entry, not per-profile.** A profile with one `"rw"` entry and one `"ro"` entry
   for two different sub-paths produces one `--bind` and one `--ro-bind`, not two of the same
   kind. Asserted on a profile mixing both.

6. **A scoped profile's binds are joined onto `workspace`, never onto anything else.**
   `str(workspace / entry.relpath)` for every entry — the same join `mask.py`'s masked-overmount
   loop already uses (`jail.py:433`), so there is exactly one path-joining convention across both
   loops in the function, not two that could drift apart.

## Ordering (a scoped profile cannot reopen what masking already closed)

7. **Masked overmounts still land after every profile bind, unconditionally.** With a scoped
   profile *and* a masked path present in the same call, the masked path's `--tmpfs` /
   `--ro-bind`-empty-file entry appears **after** every entry the profile contributed — "later
   mount wins" must hold regardless of how many bind lines precede it. Asserted with both present
   in one call, checking the masked entry's index is greater than every profile-bind index.

8. **System binds, `project_root`, and the state-dir bind are unaffected by `profile`.** Only the
   single workspace-bind line is replaced by the profile loop; nothing else in `bwrap_args`'s
   argument order moves. Asserted by diffing the full argv with and without a `profile=`, expecting
   the only difference to be the substituted segment.

## Script parity (the shell seam matches the Python seam's semantics)

9. **`sandbox-exec.sh`'s `AGENT_BIND_SCOPE` parsing produces one `--bind`/`--ro-bind` pair per
   `relpath:mode` entry, in the order given**, mirroring invariant 5 in shell. Asserted via a stub
   `bwrap` on `PATH` that dumps its received argv instead of executing (`tests/test_sandbox_exec.py`)
   — the same argv-equality standard `bwrap_args` is held to, not a live namespace.

10. **An `AGENT_BIND_SCOPE` entry the script cannot parse (missing `:mode`, empty relpath) is a
    hard failure of the script, never a silently-dropped entry.** A malformed scope must not
    fall back to binding the whole workspace — that would be a widening disguised as a parse
    error. Asserted with a deliberately malformed value, expecting non-zero exit and no `bwrap`
    invocation at all (checked via the stub's own invocation log).

**10a. `sandbox-exec.sh`'s `AGENT_BIND_SCOPE` parser rejects an absolute or `..`-escaping
relpath before `bwrap` is ever reached — same as `BindEntry` does at construction (invariant 4),
on the shell side.** **Built** (`milestone9.md` §0.2 fix landed): the per-entry loop rejects a
leading `/`, a Windows-style drive prefix (`?:`), and any `/`-separated `..` segment, each with
the same failure shape as the existing malformed-entry branch (stderr message, `exit 2`, no
`bwrap` invocation). Asserted by `test_absolute_relpath_is_a_hard_failure` and
`test_dotdot_escape_is_a_hard_failure` in `tests/test_sandbox_exec.py`, mirroring
`test_bind_entry_rejects_absolute_paths` / `test_bind_entry_rejects_dotdot_escape` in
`tests/test_profile.py`.

## Removability

11. **Deleting `harness/profile.py`, reverting `bwrap_args`'s `profile=` parameter, and reverting
    `sandbox-exec.sh`'s loop to the hardcoded line leaves M4 slice H byte-for-byte as it behaves
    today.** No `.harness-profile.yaml` key, CLI flag, or launcher env var references a profile
    object anywhere else in the tree — grep for `AgentProfile`/`AGENT_BIND_SCOPE` after deletion
    returns nothing.

## What is deliberately *not* invariant here

- **Anything about a second live agent.** No invariant constrains behavior when more than one
  `AgentProfile` is active in a real run — that is chain items 5/6 (routing gate, funnel), which
  do not exist yet. The "proof" profile in the test plan is asserted as argv output only, never
  run.
- **`NetworkPolicy`.** The `network` field is `None` and untyped in this milestone; nothing
  asserts its shape or enforcement. Chain item 3's job.
- **`harness_profile_key` resolution.** The field is reserved and unread by any code this
  milestone ships; nothing asserts what happens when it is set, only that it can be `None` without
  effect.
- **Profile *selection*.** No invariant covers choosing between profiles at runtime — every call
  site in this milestone either passes no profile (→ default) or passes one explicitly in a test.
  Chain item 4's job.
- **Effectiveness of the boundary with the jail off.** `DEEPAGENTS_JAIL=0` (the default) makes
  every invariant above true of code that never runs. That is M4 slice H's existing contract, not
  a new claim this milestone makes or weakens.
