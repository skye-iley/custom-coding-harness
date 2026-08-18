# Milestone 7 — Raw Trace Debug Mode

## 0. Build status

**Built — S1–S5 all landed** on `feat/raw-trace-debug`. Checkable properties:
`milestone7_invariants.md` (same folder) until the milestone moves to `complete/`, at which point it
folds in here as a section.

Suite state at build end: **988 passed, 4 skipped** (the four are Windows-platform skips —
posix container paths, symlink privilege), including the `live_model` tier run for real against
`ollama:gemma4`. §0.2 records what the build changed about this plan.

Source: `design_doc.md` §11 "Framework Enhancements → Raw prompt/response debug mode".

No separate `milestone7_spec.md`. M5 and M6 each carried one because the build touched six or more
modules and the schema decisions needed a home; this milestone is one middleware, one sink module,
one registry entry, one REPL branch, and one launcher line. The implementation-level detail lives in
§5–§9 here.

### 0.1 Revisions to the first draft

The first version of this doc made the knob a pre-spinup bool and argued live toggling was blocked
by the removable contract. **That was wrong and is reversed** (§10.1). It also scoped the trace to a
file sink only, and specced the *request* side in detail while treating the response as one blob.
Both are corrected: the response side is where the interesting loss is happening today (§7), and a
console mode that *replaces* the rendered answer is now first-class (§6). §8 (reasoning traces) and
§9 (streaming) are new requirements, not present in the first draft.

### 0.2 What the build changed about this plan

Four things, one of them a real bug the plan could not have predicted.

**1. `console` goes to stdout — except under `--headless`, where it goes to stderr.**
Invariant 10 says "stdout only", and §6/§13.4 say the headless JSON is a machine contract with a
real consumer (M6 §5b's sweep join). Taken literally together those conflict: `--headless
--raw-trace console` would interleave trace records into the one stream a sweep parses. The JSON
*object* would still be byte-identical, and the invariant would still read green, while the
contract was broken in practice. `build_raw_trace` therefore takes `headless=` and points the sink
at stderr in that mode. Invariant 10's "stdout only" holds for the interactive case it was written
about; the headless case gets the treatment §13.4 actually intends.

**2. The system prompt needed unwrapping, and only the live model found it.**
`ModelRequest.system_message` is a **`SystemMessage` object**, not a string. Passing it straight to
`format_content` produced `content=[{'type': 'text', 'text': 'You are an expert...` — the whole
prompt escaped inside a `repr`. That satisfies "nothing dropped" and violates "bodies are verbatim"
(invariant 1), and it is unreadable exactly where readability is the entire product.

The instructive part is *how* it surfaced. Every stubbed case passed, because a stub hands the
formatter a string. The live case passed too at first, because it asserted
`BASE_SYSTEM_PROMPT[:40] in text` — and the prompt **is** a substring of its own `repr`. It was
caught by *reading a real trace*. `format_system` is the fix; the live case now asserts
`"content=[" not in` the system section, which is the property, not the symptom. Filed here because
it generalises: a substring assertion against a serialised blob cannot tell verbatim from escaped.

**3. `config.py` imports `harness.rawtrace`.** The module's docstring said "stdlib only", and it now
takes one intra-package import to get `MODES` for the `raw_trace` spec's `choices`. Deliberate and
narrow: `rawtrace` is a leaf (stdlib + `harness.scrub`, no langchain), so the keyless/host import
profile `test_import_isolation` pins is untouched. The alternative — spelling the four values a
second time in `config.py` — is precisely the drift M5.1 exists to remove, and would leave the
validator and the writer able to disagree.

**4. `/config save`'s value collection is still hand-written.** M5.1 derived nearly everything from
the registry, but `_handle_config`'s `save` branch builds its `values` dict field by field, so
`raw_trace` needed a line there. Not fixed in this milestone (it is M5.1's seam, not M7's), but
noted: it is the one remaining place where adding a persisted live field is more than a `FieldSpec`
plus an applier.

### 0.3 Post-completion addendum — request-side dedup (found via M8 self-test)

`milestone8_selftest_findings.md` used `--raw-trace file` against real bench sweeps and found the
request half repeats its two largest blocks — the system prompt and the tool schema list — on
**every** model call in a run, even though both are static far more often than not (they only
change on an `AGENTS.md` edit or a `/config set model` rebuild). A multi-step ReAct turn re-prints
both in full at every step, which is waste with no diagnostic value: the record already has the
byte-for-byte body from an earlier call in the same file.

Fix: `rawtrace.format_request_dedup` (used by `agent.RawTraceMiddleware` in production;
`format_request` stays as the always-full renderer and is now test-only) replaces a block with a
one-line pointer — `(unchanged from previous call, sha256=<hash>)` — **only** when it hashes
identical to the immediately preceding call's rendering of that same block, and renders it in full
again the moment either changes. Message history is never touched by this — it is the interesting
part of a growing run, not repetition to collapse.

This narrows **invariant 1** below (verbatim bodies) without violating its intent: the intent is
that a reviewer can be certain nothing was dropped or altered unnoticed, not that identical bytes
are physically repeated on disk. Both conditions the narrowing depends on hold structurally — the
full body always exists earlier in the same file (the first call is never elided, since there is no
prior hash to compare against), and a change is never silently absorbed (a hash mismatch always
falls back to full rendering, and the mismatch is the same code path that would render on the very
first call). The invariant's wording is amended in place below to say this rather than carry a
silent exception. Tests: `test_rawtrace.py`'s three `format_request_dedup` cases (elide, re-render
on change, first-call parity with `format_request`) plus `test_agent.py`'s
`RawTraceMiddleware`-level cases proving the same properties hold through the actual production
seam, not just the pure function.

## 1. Goal & Definition of Done

**Goal.** Show, per model call, everything the harness hands the model and everything the model
hands back — with nothing dropped on the way to the screen or the file. The harness *already*
receives all of it and then transforms it for human consumption; this milestone makes that transform
skippable rather than mandatory.

**Done when:**

1. `raw_trace` is a four-valued knob — `off` / `file` / `console` / `both` — settable at launch
   **and in-session via `/config set raw_trace`**, which opens the M5.1 picker because the field
   carries `choices` (§10).
2. For every model call, the record contains the exact `system_message`, `messages`, and `tools`
   that call was made with, after every middleware in the stack has had its turn — including
   `_ExcludeToolsMiddleware` (§5).
3. The response side records the **whole** message object: every content block in order, reasoning
   and thinking blocks included, unknown block types dumped verbatim rather than dropped, plus
   `tool_calls`, `additional_kwargs`, `response_metadata`, `usage_metadata`, and the finish reason
   (§7).
4. An encrypted or redacted reasoning block is recorded **in position** as a typed placeholder with
   its byte size — never silently omitted, never dumped as ciphertext (§8).
5. In `console` / `both`, the REPL prints the raw record **instead of** the rendered answer. The
   value `run_turn` returns and the headless JSON are unchanged in every mode (§6).
6. `off` ⇒ the middleware is a pure pass-through: same handler return object, no file, no directory,
   no output, no formatting work performed (§10.2).
7. The record structure is **append-incremental** — open span, body, close — so a future streaming
   implementation appends chunks to an open record without changing the format or the reader (§9).
8. Credential values are redacted through `harness/scrub.py` before any byte is written or printed,
   and the redaction is visible rather than silent (§11).
9. `harness doctor` reports the mode and, in `file`/`both`, the path.
10. `design_doc.md` §11 is corrected where it over-promises (§3).

## 2. What exists today — the honest inventory

| Surface | What it gives | Why it isn't this |
|---|---|---|
| `DEEPAGENTS_DEBUG` (`cli.py:405–436`) | Uncapped error detail, full traceback, `_dump_partial`'s accumulated-state dump | **Failure-only.** A turn that "succeeds" while the model ignores every instruction prints nothing. |
| `--stream` (`cli.py:944–951`) | `pprint` of raw LangGraph events | Real, but a different axis: graph events, not the model payload. No system prompt, no tool schemas. Also bypasses the resilience layer and the HITL loop, so it is not a mode you can leave on. |
| `final_message_text` (`agent.py:436–465`) | The human-readable answer | **This is the transform being made skippable.** It keeps only `{"type": "text"}` parts and *deliberately drops* reasoning/thinking blocks and unknown part shapes — the comment at `agent.py:450–455` says so. Correct for an answer; it is also exactly the information a raw trace exists to show. |
| M6 telemetry | Per-turn counts: `model_calls`, `tool_calls` by name, `tool_errors` | Carries **no** prompt/reply/tool-arg text by construction (M6 invariant 10). Says the tool-error rate spiked; never what the model was looking at. |
| `OLLAMA_DEBUG=1` | The model server's own rendered prompt | The workaround this milestone removes: provider-specific, off-harness, gone the moment the provider changes. |

Telemetry and raw trace are complements: telemetry says *the tool-error rate spiked at turn 7*, raw
trace says *the model emitted `{"tool": "write_file"}` against a schema that wanted
`{"name": …, "args": …}`*. Neither answers alone.

## 3. Fidelity — what "raw" can and cannot mean

Three distinct things hide inside "as close as possible to what the model sees":

- **L1 — message level.** The final `system_message`, `messages`, `tools` on the way out and the
  complete message object on the way back, at the innermost middleware seam. Everything downstream
  is serialization. **This is what M7 ships**, and the standard it is held to is *nothing dropped*
  (§7), not *nothing added*.
- **L2 — wire level.** The literal HTTP request/response body the provider client puts on the
  socket, via an `httpx` event hook. Adds the SDK's serialization choices. **Deferred, not
  rejected** — it is the natural next slice if L1 turns out to be hiding something, and it is the
  only way to get strictly closer to L3 from inside the harness.
- **L3 — template level.** The token string after the model's chat template runs (`<|im_start|>`
  and friends). **Not reachable client-side.** Ollama renders the template *server-side* inside
  `/api/chat`; the body the harness sends is JSON. Reproducing it means pulling the template from
  `/api/show` and re-implementing Go template semantics in Python — a second copy of someone else's
  renderer, wrong in exactly the cases you were debugging.

### 3.1 L1's blind spot, measured — the case for L2

L2 was deferred on the condition above: *"the natural next slice if L1 turns out to be hiding
something."* It has. **Measured 2026-08-17**, first real diagnostic use of the shipped trace, on
`ollama:gemma4:harnesstest1`:

| Field | Value |
|---|---|
| `usage_metadata.output_tokens` | 450 |
| `response_metadata.eval_count` | 450 |
| `done_reason` | `stop` |
| `content` | `""` |
| `tool_calls` / `invalid_tool_calls` | 0 / 0 |
| `additional_kwargs` | `{}` |

450 tokens generated, billed, counted by M6 — and present in **no field of the message**. The turn
rendered as a blank REPL reply with no error anywhere.

Cause, confirmed against the installed source: Ollama returns reasoning in `message.thinking`, a
**sibling** of `message.content`, and `langchain-ollama` 1.1.0 captures it only behind a truthy
guard (`chat_models.py:1264`) —

```python
if reasoning and (thinking_content := stream_resp["message"].get("thinking")):
    additional_kwargs["reasoning_content"] = thinking_content
```

The harness passes no `reasoning`, so `think: null` goes out, the tag's own default is to think,
and the returned reasoning is dropped. Its own docstring says so (`reasoning=None` … *"think tags
… unless you set `reasoning` to `True`"*), which is what makes this a documented default rather
than a bug in that library.

**The trace was faithful and still could not show the answer.** The drop happens inside the
provider's response parser, *before* the message object the L1 seam observes exists — invisible at
L1 by construction, not by an implementation gap. Reaching it took a hand-rolled `/api/chat` probe
plus reading the parser's source; **L2 would have shown `message.thinking` populated in the first
minute.** That is the concrete argument for the slice, replacing the hypothetical one.

Three things changed in response, none of which repair the blind spot — they narrow the cost of it:

- `rawtrace._dropped_output_warning` — `output_tokens > 0` with an empty content/tool-calls/
  `additional_kwargs` triple now prints a warning **above** the content in the record. L1 cannot say
  *what* was lost; it can say *that something was*, instead of rendering a blank that reads like
  silence. This generalizes past Ollama: any provider parser dropping generated text trips it.
- `agent._reasoning_text` — a turn whose entire output was reasoning no longer renders as nothing.
  §7's drop is right when there *is* an answer and wrong when the reasoning is all there is.
- `providers/ollama/models/gemma4*.toml` — `reasoning = true` for both gemma4 tags, so the thinking
  is **captured** into `additional_kwargs` instead of discarded. Per-tag, not per-provider:
  reasoning-by-default is a property of the tag (`llama3.1`, measured, emits no thinking at all).
  §3.2 records why `false` — the first thing tried — is the wrong branch.

### 3.2 `reasoning = false` was the wrong branch — and the "model failure" it caused

The first fix for §3.1 set `reasoning = false`, reasoning that if the thinking channel is being
discarded, the cleanest repair is to stop the model using it. That removed the token loss and
introduced a worse failure, which was then **misdiagnosed as the model's**.

With `false`, the same task through `run-docker` returned:

```
--- response (18084 ms) | finish_reason=stop ---
ls{path:".conda/env"}
--- tool_calls (0) ---
usage_metadata: {"input_tokens": 7583, "output_tokens": 10, "total_tokens": 7593}
```

Made-up textual tool syntax, wrong path, `tool_calls` empty — `design_doc.md` §11's motivating case,
*hallucinated tool JSON*, apparently reproduced live. It was written up here as a separable
weak-model limitation. It was not. A/B on the real 11-tool agent, same model, same prompt, only the
setting varying:

| `reasoning` | tool calls | final reply |
|---|---|---|
| `false` | **none** | `"What is the absolute path to the workspace directory…"` |
| `true` | **`ls`** | `"The files in the workspace are: /hello.txt"` |

Identical on both gemma4 tags. **The model plans in the thinking channel**; take it away and agentic
tool use collapses into prose. So the two failure modes pull in opposite directions and only `true`
satisfies both — thinking captured to `additional_kwargs` rather than dropped, `content` carrying the
answer, tool calls intact.

Three lessons, and the second is the one that cost the most:

- **A one-tool probe cannot see this.** With a single plainly-specified tool bound, `reasoning=false`
  returns `{'name': 'ls', 'args': {'path': '/project/workspace'}}` correctly. That probe was used to
  *retract* an earlier correct suspicion about tool use, on the grounds that the small case worked.
  A probe simple enough to isolate a variable is often simple enough to stop reproducing the bug.
- **A fix's own side effects are the first hypothesis for a regression that follows it, not the
  last.** The failure appeared immediately after `reasoning = false` landed and was attributed to
  model capability across several exchanges. The operator's *"before, it could handle this fine"* is
  what reopened it. A trace shows what happened, not what changed — for that you need the A/B.
- **The blank turn genuinely was hiding something**, so the original instinct was sound; what it was
  hiding just wasn't a model limitation. Fixing a silent-drop bug makes the next problem appear, and
  the next problem may still be yours.

Still open and genuinely separable: nothing here establishes how this model behaves at 11 tools once
`reasoning=true` is on and the prompt is not truncated (§3.3) — it succeeded on `list files in
workspace`, which is one call. `DEEPAGENTS_LEAN_TOOLS` remains the lever if a harder task stalls.

### 3.3 The blind spot has a second direction — server-side truncation

Both cases above lose data on the **response** side. A third, found while building a negative control
for §3.1's warning, loses it on the **request** side, and L1 cannot see that either.

Running `ollama:gemma4:latest` — the spelling `ollama list` prints — the trace read
`--- tools (11) ---` and `input_tokens: 2051`, on two consecutive turns. Identical input counts
across turns whose histories differ by 1694 tokens is the signature of truncation to a fixed
ceiling, and 2051 ≈ Ollama's default `num_ctx` of 2048. The registry entry is named `gemma4`, so the
`:latest` spelling matched nothing, resolved to zero options, and never received the registry's
`num_ctx = 65536`. About 5500 tokens were discarded by the daemon — from the **left**, so the model
kept the tail and saw only the last 3 of its 11 tool schemas. It then described exactly those three
when asked what tools it had.

The trace was not wrong: the harness really did send 11 tools, and L1 records what the harness
emitted. What it cannot record is what the server discarded before the model read it. So the symptom
— *"the model only knows about the last three tools"* — reads as a model attention problem and is
actually a config miss two layers away. The fix is `Provider.registry_key` normalizing a trailing
`:latest` to the registered stem (`providers.py`), since in Ollama `name` and `name:latest` are the
same model by definition; verified by the same run going 2051 → **7583** input tokens.

Three measured cases now, in three different places: a response-parser drop, a model-behaviour
failure the first was masking, and a server-side request truncation. Only the middle one is visible
at L1. That is not an argument that the trace is weak — it caught all three by making the numbers
disagree in public — but it is a standing argument that **L1's honest scope is "what the harness
handed over"**, and every question of the form *"what did the model actually read"* needs L2 or the
server's own logs. §12's done-when already requires saying so; these are the cases that show why.

Raw tags are **ideal, not required**. The requirement is *everything the harness receives or emits*,
which L1 satisfies completely. §12 requires `design_doc.md` §11 to be corrected so an operator who
needs L3 is sent to `OLLAMA_DEBUG=1` / `/api/show` by name rather than discovering the gap by
reading a trace that doesn't have it.

## 4. Scope (slices, in build order)

All five landed; the "what lands" column is what shipped.

| Slice | What lands |
|---|---|
| **S1** | `harness/rawtrace.py` — the sink: the append-incremental record writer (§9), block rendering including the unknown-block and encrypted-reasoning cases (§7/§8), the scrub call, the byte cap, `trace_path()`. Stdlib + `harness.scrub` only; no langchain import, so it stays in the host test tier. |
| **S2** | `RawTraceMiddleware` in `harness/agent.py` — `wrap_model_call` **and** `awrap_model_call`, appended after `_ExcludeToolsMiddleware` (§5). `build_agent` grows one keyword argument. Pass-through when the mode is `off`. |
| **S3** | The knob: one `FieldSpec` (`raw_trace`, `tier="live"`, `choices=("off","file","console","both")`, `cast=str`, `default="off"`, `env_var="DEEPAGENTS_RAW_TRACE"`, `profile_key="raw_trace"`, `settable=True`) + the `cli._LIVE_APPLIERS` entry, and the `run_turn` turn bracket (§5). |
| **S4** | Console mode: the REPL suppresses the rendered answer in `console`/`both` (§6). `run_turn`'s return value and the headless JSON are untouched. |
| **S5** | Launcher forwarding (`-e DEEPAGENTS_RAW_TRACE`, both shells), `harness doctor` line, `deepagent-image/CLAUDE.md` section, `design_doc.md` §11 correction. |

## 5. The seam — innermost `wrap_model_call`, and why the ordering is load-bearing

The capture point is `wrap_model_call(request, handler)`, the seam `_ExcludeToolsMiddleware`
(`agent.py:151`) already uses. `request` carries `model`, `messages`, `system_message`,
`tool_choice`, `tools`, `response_format`, `model_settings`, `state`, `runtime` (langchain 1.3.15) —
the complete outgoing view. `handler(request)` returns a `ModelResponse` (`result`,
`structured_response`), the reply half.

**Ordering is not cosmetic.** langchain composes first-is-outermost (`cli.py:2182`), so the *last*
middleware appended is the *innermost* — the one whose view of `request` is final.
`_ExcludeToolsMiddleware` is appended last today (`agent.py:502`) precisely so it filters tools
injected by deepagents' own middleware. A trace appended earlier would log a tool list the model
never received — the exact bug class ("a tool the model never saw") this milestone diagnoses,
reproduced inside the diagnostic. **`RawTraceMiddleware` goes after it**, and an invariant pins the
list position rather than the intent.

That is also why the middleware is constructed in `cli.py` but *installed* by `build_agent`: only
`build_agent` controls what comes after the exclusion filter. `cli.py` keeps the reference so it can
bracket turns and flip the mode live.

**Turn brackets come from `run_turn`, never `before_agent`.** M6 learned this the expensive way
(`cli.py:909–926`): `before_agent` fires once per *invoke*, and one turn can invoke several times —
the resilience layer re-invokes on retry, and every HITL resume is another invoke. Numbering keyed to
`before_agent` restarts mid-turn. `RawTraceMiddleware.begin_turn()` is called from `run_turn` for
the same reason `TelemetryMiddleware.begin_turn()` is.

Records are labelled `run_id / turn N / call M`, M being the model call *within* the turn, since one
turn legitimately makes many.

## 6. Modes and destinations

| Mode | File sink | Console | Rendered answer |
|---|---|---|---|
| `off` (default) | — | — | printed as today |
| `file` | yes | — | printed as today |
| `console` | — | yes | **suppressed** |
| `both` | yes | yes | **suppressed** |

**Console mode replaces, it does not add.** The point is to see the harness's actual traffic instead
of its human-facing summary; printing both would just bury one in the other. Suppression happens at
the REPL's print site — `run_turn` still returns `final_message_text(result)` unchanged, so nothing
downstream sees a different value. `--headless` JSON is **never** raw: it is a machine contract with
a consumer (a benchmark sweep joins on it, M6 §5b), and a mode knob must not change a contract.

**File path:** `<state-dir>/raw-trace/<run_id>.log`, via `archive.state_dir(workspace)` (honours
`DEEPAGENTS_STATE_DIR`, defaults to `<workspace>/.deepagents`). One file per run: a bad run is one
file to read and one to delete. `run-docker`'s state dir is host-side, so the operator reads it at
`deepagent-image/project/state/<ws-key>/raw-trace/`.

**Format:** human-readable text, not JSON. The reader is a person; M6's `usage.jsonl` is the machine
surface and stays that way. JSON would force either escaped newlines (unreadable prompts) or
pretty-printing (no longer literal).

```
===== run <run_id> | turn 3 | call 2 | 2026-08-14T18:22:41.115Z | ollama:gemma4 =====
--- system ---
<the literal system_message text>
--- messages (7) ---
[0] human: <literal content>
[5] ai: <literal content, then each tool_call block verbatim>
[6] tool: (write_file, id=call_abc) <literal result content>
--- tools (12) ---
write_file: {<the literal JSON schema>}
--- response (1284 ms) | finish_reason=tool_calls ---
[block 0] reasoning: <literal reasoning text>
[block 1] text: <literal text>
[block 2] tool_use: {<literal args>}
--- response metadata ---
usage_metadata: {...}
response_metadata: {...}
additional_kwargs: {...}
=====
```

Separators, indices, and counts are the **only** additions; bodies go in verbatim (post-scrub, §11).
Nothing is truncated per field — a trace that elides the long tool result hides the reason the model
got confused.

**Size:** whole-file cap, default 64 MiB, a module constant (fork §10.5). On reaching it the sink
writes one `[raw-trace] cap reached` line and stops; the run continues. Console mode is uncapped —
the terminal is the operator's problem, and a cap there would silently hide the thing they asked to
see.

## 7. The response side — nothing dropped

This is where today's loss actually happens. `final_message_text` (`agent.py:449–465`) keeps only
bare strings and `{"type": "text"}` / typeless `{"text": …}` blocks, and **drops everything else** —
reasoning, thinking, tool_use, and any block shape it doesn't recognise. That is right for an answer
(the comment at `agent.py:450–455` explains the `str(item)` leak it fixed) and is precisely what a
raw trace must not do.

The record therefore serializes the **whole** returned message:

- **Every content block, in order, with its index and type.** A string content becomes one block.
- **Unknown block types are dumped verbatim**, as their literal `repr`/JSON, never skipped. A block
  type nobody anticipated is the single most interesting thing that can appear in a trace.
- **`tool_calls`** with raw `args` as received — before any parsing or repair.
- **`additional_kwargs`, `response_metadata`, `usage_metadata`,** and the finish reason. Providers
  put refusals, safety verdicts, logprobs, and model-version drift here, and none of it reaches a
  human today.
- **Invalid tool calls.** langchain surfaces malformed tool JSON separately from `tool_calls`; that
  list is the direct answer to "why did this local model's tool call do nothing", so it is recorded
  explicitly rather than left to `additional_kwargs`.

## 8. Reasoning traces

If a provider emits reasoning, the trace shows it. Three cases, all handled at the block level (§7):

1. **Plaintext reasoning / thinking.** Recorded verbatim as its own block, in position. This
   includes the inline `<think>…</think>` convention local models use — it arrives as ordinary
   content and needs no special handling, which is the point of recording blocks rather than
   interpreting them.
2. **Summarised reasoning.** Whatever the provider sends is what is recorded; the harness does not
   distinguish a summary from full reasoning, because it cannot.
3. **Encrypted / redacted reasoning.** Recorded **in position** as a typed placeholder:

   ```
   [block 1] <encrypted reasoning block: type=redacted_thinking, 2481 bytes>
   ```

   Position, type, and size, never the ciphertext — it is unreadable by construction, and dumping
   kilobytes of base64 into a human-facing trace buries the blocks that *are* readable. The
   placeholder is a **record that reasoning happened there**, which is the diagnostically useful
   fact.

The general rule: the trace reports block shapes, it does not interpret them. A provider that
invents a new reasoning representation tomorrow shows up as an unknown block dumped verbatim (§7)
rather than as silence.

## 9. Streaming compatibility

Not implemented in v1, but the design must not foreclose it.

**What makes it extendable:** records are written **append-incrementally** — `open_record(header)`,
then zero or more body appends, then `close_record(footer)`. Non-streaming writes all three in one
call; a streaming implementation appends a chunk per token-batch to the already-open record. The
on-disk format and the reader are identical either way, so streaming is a new *writer* path, not a
new format. An invariant pins that the writer exposes the three-phase API even though v1 only ever
calls it in one shot — an API that only works when the whole body is known in advance is the thing
that would have to be rewritten.

**Where a streaming implementation would hook.** `AgentMiddleware` exposes
`transformers: Sequence[TransformerFactory]` (langchain 1.3.15) — stream transformer factories
merged into the graph at compile time, each invoked as `factory(scope)` for a fresh instance per
invocation. That is the seam for seeing token chunks. Two cautions to record now: the type comes
from `langgraph.stream._mux`, a **private** module path, so the coupling needs the same
construction-time guard `_WorkspaceShellBackend` uses for `_resolve_path` (`agent.py:187–195`); and
the existing `--stream` flag is a *different* axis (LangGraph graph events, and it bypasses both the
resilience layer and the HITL loop), so a future streaming trace composes with `--stream` rather
than replacing it.

**v1 behaviour under `--stream`:** `wrap_model_call` still fires, so records are still produced. In
`console`/`both` the two outputs interleave; that is acceptable and documented, not fixed.

## 10. The knob

`FieldSpec(name="raw_trace", tier="live", env_var="DEEPAGENTS_RAW_TRACE",
profile_key="raw_trace", cast=str, default="off",
choices=("off", "file", "console", "both"), label="Raw trace", settable=True)`

Consequences that come free from the M5.1 registry, none of them hand-written: precedence
CLI > env > profile > default; rejection of an invalid value at every point of entry; the
`/config set raw_trace` **picker** (M5.1 R6 opens one for any field carrying `choices`); a wizard
entry; and inclusion in both `/config` renderers. It needs exactly one hand-written pairing — a
`cli._LIVE_APPLIERS` entry — which M5.1 invariant 7 asserts in both directions.

### 10.1 Live-settable — reversing the first draft

The first draft made this pre-spinup, arguing that a middleware which can be enabled later must be
installed always, which would break the removable contract. **The premise was wrong.** The contract
in `docs/README.md`'s glossary is about *deleting the code* and about *observable behaviour* with
the feature off — not about the middleware list's element count. An always-installed pass-through
that returns `handler(request)` unchanged is behaviourally identical to today; the first draft's
invariant 18 asserted list identity, which is stricter than the contract and is the only thing that
made live toggling look hard. Rewritten (§10.2).

The operator case is also better than the draft credited: the point of a live toggle is not to
retro-trace a turn that already failed, it is to flip tracing on and **re-run the same prompt in the
same session**, against the same thread and the same accumulated context. Restarting the container
loses exactly the state that made the failure reproducible.

`tier="live"` also means the applier can flip the mode on the already-constructed middleware — no
agent rebuild, unlike `/config set model`.

### 10.2 Removable contract (restated)

- **Off:** the middleware's hooks return `handler(request)` / `await handler(request)` and nothing
  else. No file, no directory, no output, and no record formatting performed — the mode is checked
  before any work, so `off` costs one attribute read per model call.
- **Deleted:** removing `harness/rawtrace.py`, the middleware class, the `FieldSpec` entry, the
  applier entry, the REPL branch, and the launcher line leaves the harness behaving exactly as it
  did at M6, with no dead references.

## 11. Secrets & containment

The trace contains the full context, so it contains anything the agent read — including whatever a
workspace file happened to hold. Two controls, neither overstated:

1. **Scrub before write or print.** Every section goes through `harness/scrub.py`'s `scrub()` (env
   credential values, longest first, plus `sk-`/`ghp-`/`xoxb-` style patterns) — the same
   implementation `audit.py` and `telemetry.py` use, applied to console output too, so the two
   destinations cannot disagree about what is safe. Redaction is visible (`***REDACTED***`) so a
   reader can tell altered text from text the model genuinely saw. It is a **backstop, not a
   boundary**: it redacts credentials it can recognise and nothing else.
2. **State dir, not workspace** — beside `past.sqlite`, `denials.jsonl`, `usage.jsonl`. Same
   reasoning as M4 slice D and M6 §5a, with the same precision: **file-tool-proof always**
   (pathguard plus the workspace-rooted backend cannot address the path), **shell-proof only under
   `DEEPAGENTS_JAIL=1`**.

Unlike telemetry, containment here is about the operator's disk, not the agent's reach: the file is
*meant* to hold prompt text. A trace file is a **secret-bearing artifact** and the docs say so
plainly. It is never appended to a PR, never read by a workflow, and never leaves the state dir.

## 12. Non-goals

- **L2 wire-level and L3 template-level capture** (§3). L2 is a later slice; L3 is not reachable
  client-side and the docs must say so rather than promise it.
- **A reader subcommand.** `harness telemetry show` exists because `usage.jsonl` is machine-shaped.
  This is a text file; `less` it.
- **Streaming capture in v1** (§9) — extendability is required, implementation is not.
- **Redaction beyond `scrub()`.** No PII detection, no workspace-file filtering.
- **Trace-driven assertions.** Nothing in the harness may read its own trace back.

## 13. Forks (resolved)

**13.1 — enum, not bool.** Four modes need four values, and M5.1 gives an enum validation at every
entry point plus a picker for free. A bool plus a second "where does it go" knob would be two knobs
that can disagree. Note `cast=str`: M5.1 invariant 19 requires `cast is str` wherever `choices` is
set, and bool knobs deliberately carry no `choices` so the launcher spellings keep working.

**13.2 — scrub, no opt-out.** A literal-bytes mode (`…_SCRUB=0`) was considered and dropped: the only
text `scrub()` alters is text matching a credential, which is never the text being debugged. A knob
whose sole function is "write my API key to disk verbatim" is a footgun with no diagnostic payoff.

**13.3 — console replaces rather than adds.** Printing the rendered answer *and* the raw record
buries the signal. An operator who wants both runs `both` and reads the file.

**13.4 — headless JSON is never raw.** It is a machine contract with a real consumer (M6 §5b's
sweep join). Mode knobs must not change contracts.

**13.5 — the size cap is a constant, not a knob.** M5.1 makes adding a knob a one-line edit, so the
argument is not cost — it is that 64 MiB of trace is far past the point where the answer is in the
first few records. Promote it if a real sweep hits it.

**13.6 — one file per run, no rotation.** `run_id` already scopes a run; rotation would split the
one artifact an operator wants to read end to end.

## 14. Risks

- **Ordering regression.** A future middleware appended after `RawTraceMiddleware` silently makes the
  trace non-final. Invariant 6 pins the position, so it is a red test rather than a subtly wrong
  trace.
- **The trace becomes the bug.** Formatting a large message list on every call costs real time, and
  M6's decomposition would attribute it to `model_ms`. Mitigated by `off` doing zero work; the docs
  must state that telemetry taken with tracing on is not comparable to telemetry taken with it off.
- **Sink failure breaking a turn.** A full disk or read-only state dir must not kill the run.
  Invariant 16: every sink path is wrapped, one warning per run, never re-raised.
- **Live toggle mid-turn.** `/config set` is processed between turns at the REPL prompt, so a mode
  change cannot land halfway through a model call. The applier writes one attribute; the middleware
  reads it at the top of each hook. Pinned by invariant 20 rather than assumed.
- **False confidence.** An operator may read the trace as L3. The header names the level and the
  docs say it.

## 15. Test plan

Host tier (stdlib, no image, no model) unless noted.

- `tests/test_rawtrace.py` — bodies verbatim, additions structural only; every block type rendered
  including unknown shapes (dumped, not dropped) and encrypted reasoning (placeholder with type and
  size, never ciphertext); scrub applied to every section; the three-phase writer produces the same
  bytes when driven in one shot as in N appends (§9); the cap stops writing and emits one notice; an
  `OSError` is swallowed and warns once; `trace_path` honours `DEEPAGENTS_STATE_DIR`.
- `tests/test_agent.py` — the middleware is **last** in the assembled stack, after
  `_ExcludeToolsMiddleware`; with `DEEPAGENTS_EXCLUDE_TOOLS` set, the recorded tools are the
  filtered list; `off` returns the handler's response object unchanged and writes nothing; both the
  sync and async hooks record identically.
- `tests/test_config.py` — the `FieldSpec` resolves through the four tiers, rejects an invalid mode
  at every point of entry, and defaults to `off`; the applier pairing holds both ways (M5.1
  invariant 7).
- `tests/test_cli.py` (image tier) — `run_turn` brackets turns, so two model calls in one turn are
  `call 1` / `call 2` of the same turn and a retried turn does not restart the counter; in
  `console`/`both` the rendered answer is suppressed while `run_turn`'s return value is unchanged;
  the headless JSON is byte-identical across all four modes.
- `tests/test_live_model.py` (`live_model` marker) — the case a stub cannot cover: a real
  local-model turn with tracing on, asserting the recorded tool schemas match the tools the agent
  was actually built with, that the recorded reply is the reply the turn returned, and — on a model
  that emits them — that reasoning blocks appear in the trace and **not** in
  `final_message_text`'s output. A stub answers however the test wrote it; this is the tier that
  catches a trace which is internally consistent and describes nothing real.

## 16. Invariants (folded in from `milestone7_invariants.md` on completion)

> The checkable assertions this milestone's build and tests were held to. Kept **separate** while M7
> was in-progress so they could drive testing without the planning prose around them; folded in here
> on merge, and the standalone `milestone7_invariants.md` dropped, per the milestone lifecycle in
> `docs/README.md`.
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

### Fidelity (the record describes the real call, completely)

1. **Bodies are verbatim.** The `system_message` text, each message's content, each tool-call block,
   each tool-result content, and each tool schema appear byte-for-byte as they were on the
   `ModelRequest`, modulo the scrub of invariant 13. No truncation, no pretty-printing, no
   re-serialization. **Amended (§0.3):** the system-prompt and tool-schema blocks specifically may
   be replaced by a `(unchanged from previous call, sha256=…)` pointer when byte-identical to the
   immediately preceding call's rendering of that same block — the intent this invariant protects is
   that nothing is dropped or altered *unnoticed*, and a pointer satisfies that exactly when (a) the
   full body it points at is guaranteed present earlier in the same file (the first call is never
   elided) and (b) any change falls back to a full re-render, never a silent no-op. Message content,
   tool calls, and tool results are never elided under this exception — only the two blocks that are
   large and near-static call to call.

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

### Position (the seam is the final one)

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

### Destination (what goes where — and what must not change)

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

### Containment

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

### Non-interference & removability

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

### Streaming extendability

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

### What is deliberately *not* invariant here

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
