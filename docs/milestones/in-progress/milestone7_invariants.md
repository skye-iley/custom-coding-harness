# Milestone 7 — Invariants

> Test-facing companion to `milestone7.md` (same folder). Kept **separate** while M7 is in-progress
> so these checkable properties drive testing without the planning prose around them. On completion
> this folds into `milestone7.md` as a section and the standalone file is dropped (see the milestone
> lifecycle in `docs/README.md`).
>
> **Status: written before the code**, on the M6 precedent — and now **satisfied by it**. A debug
> surface whose correctness is "the output looks about right" is untestable by construction, so what
> "right" means was pinned first. Where each is checked:
>
> | Invariant | Pinned by |
> |---|---|
> | 1, 2 (verbatim / structural additions) | `test_rawtrace.py` — bodies, no truncation, plus the `format_system` regression |
> | 3 (one record per call, raising included) | `test_agent.py::test_a_model_call_that_raises_still_produces_one_record` |
> | 4 (labels across retries/resumes) | `test_cli.py` — turn bracket + the retry case |
> | 5, 5a, 5b, 5c (response completeness) | `test_rawtrace.py` blocks/metadata/unknown/encrypted, and `test_live_model.py` for 5 |
> | 6, 7, 8 (seam position, filtered tools, both hooks) | `test_agent.py` |
> | 9, 10, 10a, 11 (destinations, modes, cap) | `test_rawtrace.py` + `test_cli.py` headless/console cases |
> | 13, 14 (scrub on both paths, visible) | `test_rawtrace.py` |
> | 16, 17, 18 (never breaks a turn, pass-through) | `test_rawtrace.py` + `test_agent.py` |
> | 20, 20a (live, derived, validated knob) | `test_config.py` + `test_cli.py` applier cases |
> | 21 (import profile) | `test_rawtrace.py::test_rawtrace_imports_stdlib_plus_scrub_only` |
> | 22 (both launchers forward the env var) | `check-parity` + the launcher diff |
> | 23 (three-phase writer) | `test_rawtrace.py::test_one_shot_and_n_appends_produce_identical_bytes` |
>
> **12, 15, 19, 24 are documentation/absence properties**, not test targets: they assert what is
> *said* (tamper-resistance at its real strength), what does *not* exist (no other reader of the
> trace; no dead references after deletion), and what is not built yet (the streaming guard). See
> `milestone7.md` §0.2 for the two places the code deviates from an invariant's literal wording.
>
> **Revised from the first draft** alongside `milestone7.md` §0.1. Invariant 18 previously asserted
> that the middleware list is element-for-element the pre-M7 list when the feature is off. That was
> stricter than the removable contract actually requires, and it was the only thing making a live
> `/config` toggle look impossible. It now asserts pass-through *behaviour*. Invariants 5a, 5b, 5c
> (response completeness, unknown blocks, reasoning), 10/10a (modes and console substitution), 20a
> (live toggle timing), and 23–24 (streaming extendability) are new.

M7 = everything the harness sends and receives becomes visible, without the looking changing the
run and without the file becoming a leak the operator didn't expect. The invariants split five ways:
**fidelity** (the record describes the real call, and drops nothing), **position** (the seam is the
final one), **destination** (what goes where, and what must not change), **containment**, and
**non-interference / removability**.

The rule the milestone rests on, stated once: **a trace that disagrees with what the model received
is worse than no trace**, because it is trusted. Every fidelity invariant exists to make that
disagreement a test failure rather than a debugging dead end.

## Fidelity (the record describes the real call, completely)

1. **Bodies are verbatim.** The `system_message` text, each message's content, each tool-call block,
   each tool-result content, and each tool schema appear byte-for-byte as they were on the
   `ModelRequest`, modulo the scrub of invariant 13. No truncation, no pretty-printing, no
   re-serialization.

2. **Additions are structural only.** The `===== run … =====` header, the `--- system ---` /
   `--- messages (N) ---` / `--- tools (N) ---` / `--- response ---` rules, block indices, and counts
   are all the sink adds. No commentary, no summary, no interpretation of what the model did.

3. **Every model call produces exactly one record.** Including calls that raise: a provider error
   still records the request half and marks the outcome. Three model calls in a turn produce three
   records.

4. **Labels are correct across retries and resumes.** Records carry `run_id / turn N / call M`, `N`
   from `run_turn`'s bracket and `M` counted within the turn. A resilience retry or a HITL resume
   re-invokes the graph; neither may restart `N`. *(M6's `before_agent` finding, `cli.py`
   `begin_turn`, applied here rather than re-derived wrong.)*

5. **The response half is the response.** The recorded reply content and tool-call blocks equal what
   `handler(request)` returned and what the turn went on to use. Asserted against a real model in
   the `live_model` tier, because a stub returns whatever the test wrote and cannot catch a trace
   that is internally consistent while describing nothing real.

   **5a. Nothing on the response is dropped.** Every content block appears, in order, with its index
   and type; plus `tool_calls` (raw args, pre-parse), invalid/malformed tool calls as their own
   section, `additional_kwargs`, `response_metadata`, `usage_metadata`, and the finish reason.
   *(Directly opposes `final_message_text` (`agent.py:449–465`), which keeps text parts and drops
   the rest — correct for an answer, and exactly what this must not do.)*

   **5b. Unknown block types are dumped, never skipped.** A content block whose shape the sink does
   not recognise is written verbatim as its literal `repr`/JSON. A block type nobody anticipated is
   the most interesting thing that can appear in a trace; silence about it is the one unrecoverable
   failure. *(Test: a synthetic block of an invented type survives into the record intact.)*

   **5c. Reasoning is present in position, encrypted or not.** Plaintext reasoning/thinking blocks
   are recorded verbatim. An encrypted or redacted block is recorded **in position** as a typed
   placeholder carrying its type and byte size — never omitted, never dumped as ciphertext. A trace
   in which reasoning happened but nothing marks the spot is a false negative.

## Position (the seam is the final one)

6. **`RawTraceMiddleware` is last in the assembled middleware list.** langchain composes
   first-is-outermost, so last = innermost = the final view of `request`. Asserted as a position in
   the list `build_agent` produces, not as a comment. *(Pinned because the failure is silent: a trace
   taken one layer out logs tools the model never received — the exact bug class this milestone
   diagnoses.)*

7. **It is after `_ExcludeToolsMiddleware` specifically.** With `DEEPAGENTS_EXCLUDE_TOOLS` or
   `DEEPAGENTS_LEAN_TOOLS` set, the recorded `tools` list equals the **filtered** list and excluded
   tool names appear nowhere in the record.

8. **Both hooks exist and agree.** `wrap_model_call` and `awrap_model_call` are both implemented and
   record identically. *(`_ExcludeToolsMiddleware` carries both; a sync-only trace would go blank the
   day a path goes async, with no error to notice.)*

## Destination (what goes where — and what must not change)

9. **The file sink is in the state dir.** `<state-dir>/raw-trace/<run_id>.log`, resolved through
   `archive.state_dir(workspace)`, honouring `DEEPAGENTS_STATE_DIR`. Never inside the workspace
   mount.

10. **Output matches the mode, exactly.** `off`: nothing anywhere. `file`: the file only. `console`:
    stdout only, no file created. `both`: both, and the two carry identical content.

    **10a. Console mode replaces the rendered answer, and only that.** In `console`/`both` the REPL
    does not print `final_message_text`'s output; in `off`/`file` it does. In **all four** modes
    `run_turn` returns the same value, and the `--headless` JSON is byte-identical. *(A display knob
    that changes a machine contract is a different feature; M6 §5b's sweep joins on that JSON.)*

11. **The file cap holds and announces itself.** At the 64 MiB whole-file cap the sink stops writing
    and emits exactly one notice; the run continues and later turns still succeed. A debug flag
    cannot fill the disk. Console output is deliberately uncapped — a cap there would hide the thing
    the operator asked to see.

## Containment

12. **Tamper-resistance is stated at its real strength.** File-tool-proof always (pathguard plus the
    workspace-rooted backend cannot address the path); shell-proof **only** under
    `DEEPAGENTS_JAIL=1`. No doc, docstring, or `doctor` line may round this up. *(The wording
    discipline M6 invariants 15/16 imposed.)*

13. **Scrub runs on every section before any byte is written *or printed*.** System prompt, messages,
    tool schemas, response blocks, and metadata alike — and on the console path as well as the file
    path, so the two destinations cannot disagree about what is safe. A credential pasted into a
    tool argument is the likeliest way one reaches this surface.

14. **Redaction is visible.** Redacted spans carry `***REDACTED***`, so a reader can tell altered
    text from text the model genuinely saw.

15. **Nothing else reads the trace.** No PR body, no workflow, no telemetry record, no archive row
    references or embeds trace content. Exactly one consumer: a person.

## Non-interference & removability

16. **A sink failure never breaks a turn.** Every write path is wrapped; an `OSError` (read-only
    state dir, full disk, missing parent) degrades to one stderr warning **per run** and the model
    call returns its result unchanged. Tracing is never load-bearing.

17. **The handler's return value is passed through untouched.** `wrap_model_call` returns exactly
    what `handler(request)` returned — same object, no copy, no `override`. The middleware observes;
    it does not shape.

18. **`off` is a true pass-through.** With mode `off` the hooks do nothing but call the handler: no
    file, no directory, no output, and **no record formatting performed** — the mode is checked
    before any work. Observable behaviour is identical to M6. *(Asserted behaviourally. The draft's
    "the middleware list is element-for-element the pre-M7 list" is deliberately **not** an
    invariant: it is stricter than the removable contract in `docs/README.md`'s glossary, and it was
    the only obstacle to a live toggle.)*

19. **Deleting the feature reverts to M6.** Removing `harness/rawtrace.py`, the middleware class, the
    `FieldSpec` entry, the applier entry, the REPL branch, and the launcher line leaves a harness
    that behaves as it did before this milestone, with no dead references.

20. **The knob is live, derived, and validated.** One `FieldSpec` (`tier="live"`, `cast=str`,
    `choices=("off","file","console","both")`, `default="off"`, `settable=True`): precedence
    CLI > env > profile > default holds; an invalid mode is rejected at **every** point of entry
    (profile, env, CLI, `/config set`) per M5.1 §3.1; `/config set raw_trace` with no value opens the
    picker; and `cli._LIVE_APPLIERS` pairs with it exactly, both directions (M5.1 invariant 7).

    **20a. A live change takes effect on the next model call and never mid-call.** The applier writes
    one attribute on the already-constructed middleware — no agent rebuild — and each hook reads the
    mode at its top. `/config` is processed between turns, so a mode change cannot land halfway
    through a call.

21. **Import profile.** `harness/rawtrace.py` imports stdlib plus `harness.scrub` and nothing else
    from the package — no langchain — so it stays in the host test tier. The middleware class, which
    needs the langchain base, lives in `agent.py`. *(Same split as `telemetry.py` vs
    `TelemetryMiddleware` in `cli.py`, for the same reason.)*

22. **Both launchers forward `-e DEEPAGENTS_RAW_TRACE`.** The host-side flag and the in-container
    reader cannot come apart. *(M5 §0.1's `DEEPAGENTS_JAIL` bug: a launcher that set a knob the
    container never saw.)*

## Streaming extendability

23. **The writer is three-phase.** `open_record` / append body / `close_record` is the only way a
    record is produced, even in v1 where all three are called in one shot. Driving the same content
    through N appends produces byte-identical output to driving it in one. *(An API that only works
    when the whole body is known in advance is precisely what a streaming implementation would have
    to rewrite; `milestone7.md` §9.)*

24. **The private-API coupling is guarded, not assumed.** If and when the streaming path uses
    `AgentMiddleware.transformers` (whose `TransformerFactory` comes from the private
    `langgraph.stream._mux`), it carries a construction-time guard that fails loud on an upstream
    rename — the pattern `_WorkspaceShellBackend.__init__` uses for `_resolve_path`
    (`agent.py:187–195`). Not applicable until that path exists; recorded now so it is not
    rediscovered.

## What is deliberately *not* invariant here

- **Timing neutrality.** Formatting a large message list costs real time, and M6's decomposition
  attributes it to `model_ms`. No invariant claims tracing is free; the docs instead state that
  telemetry taken with tracing on is not comparable to telemetry taken with it off.
- **L3 fidelity.** Nothing asserts the record matches the model server's template-rendered token
  string, because the harness cannot see it (`milestone7.md` §3). An invariant that cannot be
  checked is a claim, not a property.
- **Output stability.** The record layout is for humans and may change between milestones. No test
  pins exact header text beyond what invariants 1–2 require, and nothing may parse it.
- **Interleaving under `--stream`.** With `--stream` and `console`/`both` together, graph events and
  trace records interleave. Documented, not fixed — they are different axes (`milestone7.md` §9).
