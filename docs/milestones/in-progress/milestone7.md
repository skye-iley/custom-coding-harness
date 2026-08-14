# Milestone 7 — Raw Trace Debug Mode

## 0. Build status

**Spec only — no code yet.** Doc lands first on `feat/raw-trace-debug`; the build follows on the
same branch. Checkable properties: `milestone7_invariants.md` (same folder), which folds in here as
a section on completion.

Source: `design_doc.md` §11 "Framework Enhancements → Raw prompt/response debug mode". That entry
asks for one thing this milestone **cannot deliver as written** — see §3.

No separate `milestone7_spec.md`. M5 and M6 each carried one because the build touched six or more
modules and the schema decisions needed a home; this milestone is one middleware, one sink module,
one registry entry, and one launcher line. The implementation-level detail lives in §5–§7 here.

## 1. Goal & Definition of Done

**Goal.** Print, per model call, the literal message payload the harness hands the model — final
system prompt, full message history, tool schemas, and the tool-call / tool-result blocks — so a
weak-model failure (hallucinated tool JSON, ignored instructions, a tool the model never saw) can be
diagnosed from the harness's own output instead of by switching on the model server's debug logging.

**Done when:**

1. `DEEPAGENTS_RAW_TRACE=1` produces, for every model call in a run, a record containing the exact
   `system_message`, `messages`, and `tools` that call was made with — after every middleware in the
   stack has had its turn, including `_ExcludeToolsMiddleware` (§5).
2. Unset (the default) ⇒ the middleware is never constructed and the agent's middleware stack is
   identical to today's, list element for list element (§10).
3. The trace is written to a file under the **state dir**, never to stderr, and stderr gets exactly
   one line naming the path (§6).
4. A model call that the harness fails to trace still completes: a sink error degrades to one
   stderr warning and never propagates into the turn (§11).
5. Credential values are redacted through the existing `harness/scrub.py` before any byte is
   written, and the redaction is visible in the output rather than silent (§7).
6. `harness doctor` reports whether raw trace is on and where it is writing.
7. The `design_doc.md` §11 entry is corrected to match what shipped (§3 — the chat-template-marker
   claim).

## 2. What exists today — the honest inventory

| Surface | What it gives | Why it isn't this |
|---|---|---|
| `DEEPAGENTS_DEBUG` (`cli.py:405–436`) | Uncapped error detail, a full traceback, and `_dump_partial`'s accumulated-state dump | **Failure-only.** A turn that "succeeds" while the model ignores every instruction prints nothing. |
| `--stream` | Raw LangGraph events as they arrive | The *harness's* view of the stream, not the payload the model was sent; no tool schemas, no system prompt. |
| M6 telemetry (`telemetry.py`) | Per-turn counts: `model_calls`, `tool_calls` by name, `tool_errors` | Deliberately carries **no** prompt, reply, or tool-argument text (M6 invariant 10). It says a tool call failed, never what the model was looking at when it did. |
| `OLLAMA_DEBUG=1` | The model server's own rendered prompt | The workaround this milestone exists to remove: provider-specific, off-harness, gone the moment the provider changes. |

Telemetry and raw trace are complements, not overlaps: telemetry says *the tool-error rate spiked at
turn 7*, raw trace says *the model emitted `{"tool": "write_file"}` against a schema that wanted
`{"name": ..., "args": ...}`*. Neither answers alone.

## 3. Fidelity — what "raw" can and cannot mean

`design_doc.md` §11 asks for the payload "as close as possible to what the model itself sees (raw
tags included, e.g. Ollama's chat-template markers)". Three distinct things hide inside that
sentence, and only one of them is reachable from inside the harness:

- **L1 — message level.** The final `system_message`, `messages`, and `tools` at the innermost
  middleware seam. This is exactly what the harness sends; everything downstream is serialization.
  **This is what M7 ships.**
- **L2 — wire level.** The literal HTTP request/response body the provider client puts on the
  socket. Reachable via an `httpx` event hook, but it is still JSON messages — it adds the provider
  SDK's serialization choices and nothing else diagnostic. Deferred (§8).
- **L3 — template level.** The token string after the model's chat template runs (`<|im_start|>` and
  friends). **Not reachable client-side.** Ollama renders the template *server-side* inside
  `/api/chat`; the body the harness sends is JSON. Reproducing it would mean pulling the template
  from `/api/show` and re-rendering Go template semantics in Python — a second implementation of
  someone else's renderer, which would be wrong in exactly the cases you were debugging.

**Decision: L1 only.** The failure modes this milestone targets — a hallucinated tool JSON, an
instruction that never reached the model, a tool excluded from the schema list — are all visible at
L1. §12 requires the `design_doc.md` §11 wording to be corrected rather than left aspirational: an
operator who needs L3 should be sent to `OLLAMA_DEBUG=1` / `/api/show` by name, in the doc, instead
of discovering the gap by reading a trace that doesn't have it.

## 4. Scope (slices, in build order)

| Slice | What lands |
|---|---|
| **S1** | `harness/rawtrace.py` — the sink: record formatting, the scrub call, the byte cap, `trace_path()`. Stdlib + `harness.scrub` only, so it stays in the host test tier. No langchain import. |
| **S2** | `RawTraceMiddleware` in `harness/agent.py`, implementing `wrap_model_call` **and** `awrap_model_call`, appended after `_ExcludeToolsMiddleware` (§5). `build_agent` grows one keyword argument. |
| **S3** | The knob: one `FieldSpec` entry (`raw_trace`, `tier="prespinup"`, `env_var="DEEPAGENTS_RAW_TRACE"`, `cast=_to_bool`, `default=False`, `profile_key="raw_trace"`), `cli` construction, and the turn bracket from `run_turn` (§5). |
| **S4** | Launcher forwarding: `-e DEEPAGENTS_RAW_TRACE` in `run-docker.{sh,ps1}`, plus the state-dir note in both `CLAUDE.md` files. Both shells, per the parity rule. |
| **S5** | `harness doctor` line + docs: `deepagent-image/CLAUDE.md` section, `design_doc.md` §11 correction (§3). |

## 5. The seam — innermost `wrap_model_call`, and why the ordering is load-bearing

The capture point is `wrap_model_call(request, handler)`, the same seam
`_ExcludeToolsMiddleware` (`agent.py:151`) already uses. At that seam `request` carries
`system_message`, `messages`, `tools`, `tool_choice`, `response_format`, and `model_settings`
(langchain 1.3.15) — the complete outgoing view. `handler(request)` returns a `ModelResponse`
(`result`, `structured_response`), which is the reply half of the record.

**Ordering is not cosmetic.** langchain composes first-is-outermost (`cli.py:2182`), so the *last*
middleware appended is the *innermost* — the one whose view of `request` is final.
`_ExcludeToolsMiddleware` is appended last today (`agent.py:502`) precisely so it filters tools
injected by deepagents' own middleware. A raw trace appended anywhere earlier would log a tool list
the model never received — which is the exact bug class ("a tool the model never saw") this
milestone is meant to diagnose, reproduced inside the diagnostic. **`RawTraceMiddleware` is appended
after it**, and an invariant pins the position rather than the intent.

That is also why the middleware is constructed in `cli.py` but *installed* by `build_agent`: only
`build_agent` controls what comes after the exclusion filter. `cli.py` keeps the reference so it can
bracket turns.

**Turn brackets come from `run_turn`, never from `before_agent`.** M6 learned this the expensive
way (`cli.py:709–726`): `before_agent` fires once per *invoke*, and one turn can invoke several
times — the resilience layer re-invokes on retry, and every HITL resume is another invoke. A record
labelled by `before_agent` would restart its numbering mid-turn. `RawTraceMiddleware.begin_turn()`
is called from `run_turn` for the same reason `TelemetryMiddleware.begin_turn()` is.

Each record is therefore labelled `run_id / turn N / call M` — M being the model call *within* the
turn, since a single turn legitimately makes many.

## 6. Sink, format, size

**Path:** `<state-dir>/raw-trace/<run_id>.log`, via `archive.state_dir(workspace)` (honours
`DEEPAGENTS_STATE_DIR`; defaults to `<workspace>/.deepagents`). One file per run, so a bad run is
one file to read and one file to delete.

**Not stderr.** The full history plus tool schemas, every call, would drown the `you>` REPL and make
the session unusable — which would push an operator straight back to `OLLAMA_DEBUG`. stderr gets one
line at startup: `[harness] raw trace -> <path>`.

**Format:** human-readable text, not JSON. The reader is a person diagnosing a model, not a parser
(M6's `usage.jsonl` is the machine surface and stays that way). Each record:

```
===== run <run_id> | turn 3 | call 2 | 2026-08-14T18:22:41.115Z | ollama:gemma4 =====
--- system ---
<the literal system_message text>
--- messages (7) ---
[0] human: ...
[5] ai: (tool_calls: write_file)  <the literal content, then the literal tool_call blocks>
[6] tool: (write_file, id=call_abc)  <the literal result content>
--- tools (12) ---
write_file: {<the literal JSON schema>}
...
--- response (1284 ms) ---
<the literal reply content, then the literal tool_call blocks>
```

Separators and the counts in the headers are the **only** added formatting; message and schema
bodies go in verbatim (post-scrub, §7). Nothing is truncated per field — a trace that elides the
long tool result is a trace that hides the reason the model got confused.

**Size:** whole-file cap, default 64 MiB, a module constant rather than a knob. On reaching it the
sink writes one `[raw-trace] cap reached, further calls not recorded` line and stops; the run
continues. A debug flag must not be able to fill the disk under a long benchmark sweep. Fork §9.4
records why this is a constant.

## 7. Secrets & containment

The trace contains the full context, so it contains anything the agent read — including whatever a
workspace file happened to hold. Two controls, and the doc must not overstate either:

1. **Scrub before write.** Every record goes through `harness/scrub.py`'s `scrub()` (env-derived
   credential values, longest-first, plus the `sk-`/`ghp-`/`xoxb-` style patterns) — the same
   implementation `audit.py` and `telemetry.py` use. Redaction is visible: the `***REDACTED***`
   marker tells the reader the text was altered rather than leaving them to wonder why the model saw
   something different. It is a **backstop, not a boundary** — it redacts credentials it can
   recognise, and nothing else. A trace file is a secret-bearing artifact and the docs say so
   plainly.
2. **State dir, not workspace** — beside `past.sqlite`, `denials.jsonl`, and `usage.jsonl`. Same
   reasoning as M4 slice D and M6 §5a, with the same precision about what it buys: the sink is
   **file-tool-proof always** (pathguard plus the workspace-rooted backend cannot address it) and
   **shell-proof only under `DEEPAGENTS_JAIL=1`**. With the jail off, a container shell reaches it by
   absolute path.

Unlike telemetry, containment here is about the **operator's** disk, not the agent's reach: the file
is meant to hold prompt text. It is never appended to a PR, never read by a workflow, and never
leaves the state dir. `run-docker`'s state dir is host-side, so the operator reads it at
`deepagent-image/project/state/<ws-key>/raw-trace/`.

## 8. Non-goals

- **L2 wire-level and L3 template-level capture** (§3). L2 is a plausible later slice; L3 is not
  reachable client-side and the docs must say that rather than promise it.
- **A reader subcommand.** `harness telemetry show` exists because `usage.jsonl` is machine-shaped.
  This is a text file; `less` it.
- **Live toggling.** See fork §9.1.
- **Redaction beyond `scrub()`.** No PII detection, no workspace-file filtering. A trace is a
  secret-bearing artifact; that is a property to document, not to half-fix.
- **Trace-driven assertions.** Nothing in the harness may read its own trace back.

## 9. Forks (resolved)

**9.1 — prespinup, not live-settable.** Tempting to make `/config set raw_trace on` work mid-session
("turn it on when the weird turn happens"). Rejected: a middleware that can be enabled later must be
*installed* always, which breaks the removable contract (§10) — off would no longer mean a
byte-identical stack. And it buys little, because you cannot retro-trace the turn that already went
wrong; you re-run it. The container is disposable and a session is one command. `tier="prespinup"`,
matching `telemetry`.

**9.2 — scrub, and no opt-out.** A literal-bytes mode (`DEEPAGENTS_RAW_TRACE_SCRUB=0`) was
considered and dropped: the only text `scrub()` alters is text that matches a credential, which is
never the text you are debugging. A knob whose only function is "write my API key to disk verbatim"
is a footgun with no diagnostic payoff.

**9.3 — text, not JSON.** The audience is human. JSON would force either escaped newlines (unreadable
prompts) or a pretty-printer (no longer literal). M6 owns the machine-readable surface.

**9.4 — the size cap is a constant, not a knob.** M5.1 makes adding a knob a one-line edit, so the
argument is not cost — it is that 64 MiB of trace is already far past the point where the answer is
in the first few records. Promote it to a `FieldSpec` if a real sweep hits the cap; do not
pre-emptively add a knob nobody has needed.

**9.5 — one file per run, no rotation.** `run_id` already scopes a run. Rotation would split the
one artifact an operator wants to read end to end.

## 10. Removable contract

`DEEPAGENTS_RAW_TRACE` unset ⇒ `RawTraceMiddleware` is never constructed, `build_agent`'s new
keyword defaults to `None`, and the middleware list is element-for-element what it is today. Deleting
`harness/rawtrace.py`, the middleware class, the `FieldSpec` entry, and the launcher line reverts the
harness to M6 behaviour with no residue.

## 11. Risks

- **Ordering regression.** A future middleware appended after `RawTraceMiddleware` silently makes the
  trace non-final. Invariant 3 pins the position by asserting it is last, so the failure is a red
  test rather than a subtly wrong trace.
- **The trace becomes the bug.** Formatting a huge message list on every call costs real time, and
  M6's `duration_ms` decomposition would attribute it to `model_ms`. Mitigated by the flag being off
  by default; §12 requires one live-model timing sanity check, and the docs must state that
  telemetry numbers taken with raw trace on are not comparable to numbers taken with it off.
- **Sink failure breaking a turn.** A full disk or a read-only state dir must not kill the run.
  Invariant 6: every sink path is wrapped, one warning per run, never re-raised.
- **False confidence.** An operator may read the trace as "what the model saw" in the L3 sense. The
  header line and the docs name the level explicitly.

## 12. Test plan

Host tier (stdlib, no image, no model) unless noted.

- `tests/test_rawtrace.py` — record formatting is verbatim for bodies and additive only for
  separators; the scrub is applied to every section; the cap stops writing and emits exactly one
  notice; a sink `OSError` is swallowed and warns once; `trace_path` honours `DEEPAGENTS_STATE_DIR`.
- `tests/test_agent.py` — the middleware is **last** in the assembled stack, after
  `_ExcludeToolsMiddleware`; with the flag unset the stack equals the pre-M7 stack exactly; a stub
  `wrap_model_call` round-trip records one call and returns the handler's response unchanged; the
  async twin is present and behaves the same.
- `tests/test_config.py` — the `FieldSpec` entry resolves CLI > env > profile > default, and the
  removable default is `False`.
- `tests/test_cli.py` (image tier) — `run_turn` brackets turns, so two model calls in one turn are
  `call 1` / `call 2` of the same turn, and a retried turn does not restart the turn counter.
- `tests/test_live_model.py` (`live_model` marker) — the one case a stub cannot cover: run a real
  local-model turn with tracing on and assert the recorded tool schemas match the tools the agent was
  actually built with, and that the recorded reply is the reply the turn returned. A stub answers
  however the test wrote it to; this is the tier that catches a trace which is internally consistent
  and describes nothing real.
