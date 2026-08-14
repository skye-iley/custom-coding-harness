# Milestone 7 — Invariants

> Test-facing companion to `milestone7.md` (same folder). Kept **separate** while M7 is in-progress
> so these checkable properties drive testing without the planning prose around them. On completion
> this folds into `milestone7.md` as a section and the standalone file is dropped (see the milestone
> lifecycle in `docs/README.md`).
>
> **Status: written before the code**, on the M6 precedent. A debug surface whose correctness is
> "the output looks about right" is untestable by construction, so what "right" means is pinned
> first.

M7 = the payload the harness hands the model becomes readable, without the reading changing the
payload and without the file becoming a leak the operator didn't expect. The invariants split four
ways: **fidelity** (the record describes the call that actually happened), **position** (the seam is
the final one), **containment** (what is on disk and where), and **non-interference /
removability** (tracing never changes the run, and off means unchanged).

The one rule the milestone rests on, stated once: **a trace that disagrees with what the model
received is worse than no trace**, because it is trusted. Every fidelity invariant below exists to
make that disagreement a test failure rather than a debugging dead end.

## Fidelity (the record describes the real call)

1. **Bodies are verbatim.** The `system_message` text, each message's content, each tool-call block,
   each tool-result content, and each tool schema appear in the record byte-for-byte as they were on
   the `ModelRequest`, modulo §7's scrub. No truncation, no pretty-printing, no re-serialization.
   *(A record whose long tool result is elided hides the reason the model got confused.)*

2. **Additions are separators only.** Everything the sink adds — the `===== run … =====` header, the
   `--- system ---` / `--- messages (N) ---` / `--- tools (N) ---` / `--- response ---` rules, the
   index prefixes, the counts — is structural. No commentary, no summary, no interpretation of what
   the model did.

3. **Every model call produces exactly one record.** Including calls that raise: a provider error
   still records the request half and marks the outcome. A turn making three model calls produces
   three records, not one.

4. **Labels are correct across retries and resumes.** Records carry `run_id / turn N / call M`, `N`
   from `run_turn`'s bracket and `M` counted within the turn. A resilience retry or a HITL resume
   re-invokes the graph; neither may restart `N`. *(This is M6's `before_agent` finding — `cli.py`
   §`begin_turn` — applied here rather than re-derived wrong.)*

5. **The response half is the response.** The recorded reply content and tool-call blocks equal what
   `handler(request)` returned and what the turn went on to use. Asserted against a real model in
   the `live_model` tier, because a stub returns whatever the test wrote and cannot catch a trace
   that is internally consistent while describing nothing real.

## Position (the seam is the final one)

6. **`RawTraceMiddleware` is last in the assembled middleware list.** langchain composes
   first-is-outermost, so last = innermost = the final view of `request`. Asserted as a position in
   the list produced by `build_agent`, not as a comment. *(Pinned because the failure is silent: a
   trace taken one layer out logs tools the model never received — the exact bug class this
   milestone diagnoses.)*

7. **It is after `_ExcludeToolsMiddleware` specifically.** With `DEEPAGENTS_EXCLUDE_TOOLS` or
   `DEEPAGENTS_LEAN_TOOLS` set, the recorded `tools` list equals the **filtered** list, and excluded
   tool names appear nowhere in the record.

8. **Both hooks exist.** `wrap_model_call` and `awrap_model_call` are both implemented and record
   identically. *(`_ExcludeToolsMiddleware` carries both; a sync-only trace would go blank the day a
   path goes async, with no error to notice.)*

## Containment (what lands on disk, and where)

9. **The sink is in the state dir.** `<state-dir>/raw-trace/<run_id>.log`, resolved through
   `archive.state_dir(workspace)`, honouring `DEEPAGENTS_STATE_DIR`. Never inside the workspace
   mount.

10. **Tamper-resistance is stated at its real strength.** File-tool-proof always (pathguard plus the
    workspace-rooted backend cannot address the path); shell-proof **only** under
    `DEEPAGENTS_JAIL=1`. No doc, docstring, or `doctor` line may round this up. *(Same wording
    discipline M6 invariant 15/16 imposed.)*

11. **Scrub runs on every section before any byte is written.** System prompt, messages, tool
    schemas, and response alike — not just the message bodies. A credential pasted into a tool
    argument is the likeliest way one reaches this file.

12. **Redaction is visible.** Redacted spans carry the `***REDACTED***` marker, so a reader can tell
    altered text from text the model genuinely saw.

13. **Nothing else reads the trace.** No PR body, no workflow, no telemetry record, no archive row
    references or embeds trace content. The file has exactly one consumer: a person.

14. **Never on stderr.** No record body is written to stdout or stderr at any verbosity. The only
    stderr output is the single startup path line, plus the at-most-one warning of invariant 16 and
    the at-most-one cap notice of invariant 15.

15. **The cap holds and announces itself.** At the 64 MiB whole-file cap the sink stops writing and
    emits exactly one notice; the run continues normally and later turns still succeed. A debug flag
    cannot fill the disk.

## Non-interference & removability

16. **A sink failure never breaks a turn.** Every write path is wrapped; an `OSError` (read-only
    state dir, full disk, missing parent) degrades to one stderr warning **per run** and the model
    call returns its result unchanged. Tracing is never load-bearing.

17. **The handler's return value is passed through untouched.** `wrap_model_call` returns exactly
    what `handler(request)` returned — same object, no copy, no `override`. The middleware observes;
    it does not shape.

18. **Off ⇒ byte-for-byte unchanged.** With `DEEPAGENTS_RAW_TRACE` unset, `RawTraceMiddleware` is
    never constructed and the middleware list `build_agent` produces is element-for-element the
    pre-M7 list. No file is created, no directory is created.

19. **Deleting the feature reverts to M6.** Removing `harness/rawtrace.py`, the middleware class, the
    `FieldSpec` entry, and the launcher line leaves a harness that behaves as it did before this
    milestone, with no dead references.

20. **The knob is pre-spinup and derives from the registry.** One `FieldSpec` entry
    (`tier="prespinup"`, `cast=_to_bool`, `default=False`); precedence CLI > env > profile > default
    holds for it like any other field, and it is not `settable` — `/config set raw_trace` is not a
    thing (`milestone7.md` §9.1). Both launchers forward `-e DEEPAGENTS_RAW_TRACE`, so the host-side
    flag and the in-container reader cannot come apart. *(M5 §0.1's `DEEPAGENTS_JAIL` bug, which
    shipped a launcher that set a security knob the container never saw.)*

21. **Import profile.** `harness/rawtrace.py` imports stdlib plus `harness.scrub` and nothing else
    from the package — no langchain, so it stays in the host test tier. The middleware class, which
    needs the langchain base, lives in `agent.py`. *(Same split as `telemetry.py` vs
    `TelemetryMiddleware` in `cli.py`, for the same reason.)*

## What is deliberately *not* invariant here

- **Timing neutrality.** Formatting a large message list costs real time, and M6's decomposition will
  attribute it to `model_ms`. No invariant claims tracing is free; the docs instead state that
  telemetry taken with tracing on is not comparable to telemetry taken with it off.
- **L3 fidelity.** Nothing asserts the record matches the model server's template-rendered token
  string, because the harness cannot see it (`milestone7.md` §3). An invariant that cannot be
  checked is a claim, not a property.
- **Output stability.** The record layout is for humans and may change between milestones. No test
  pins the exact header text beyond what invariants 1–2 require, and nothing may parse it.
