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
- `providers/ollama/models/gemma4-harnesstest1.toml` — `reasoning = false` for the tag, so it spends
  its tokens on content and tool calls. Per-tag, not per-provider: thinking-by-default is a property
  of the tag.

### 3.2 The second finding, which the first was hiding

With the drop fixed, the same task run through `run-docker` produces a *different* failure, and this
one is the model's:

```
--- response (18084 ms) | finish_reason=stop ---
ls{path:".conda/env"}
--- tool_calls (0) ---
--- invalid_tool_calls (0) ---
usage_metadata: {"input_tokens": 7583, "output_tokens": 10, "total_tokens": 7593}
```

Ten output tokens, all of them **content**: a made-up textual tool syntax instead of a structured
call, against a wrong path, with `tool_calls` empty. This is `design_doc.md` §11's motivating case
verbatim — *hallucinated tool JSON* — and the trace shows it in one screen.

Note it is **load-bearing that the two failures were sequential, not simultaneous.** While the
parser drop was live, this was unobservable: the turn rendered blank whatever the model did. Fixing
a silent-drop bug does not fix the thing it was hiding; it makes it appear, and an operator who
stops at the first green run will conclude the wrong thing.

**Difficulty is contextual, and a small probe will lie about it.** The same tag, same prompt, one
plainly-specified tool bound, returns `{'name': 'ls', 'args': {'path': '/project/workspace'}}`
correctly on the first call. It is the *real* request — 11 tools, the full agent system prompt,
7583 input tokens — that it cannot handle. Both measurements are true; only the second is about the
harness's actual workload. A probe simple enough to isolate a variable is often simple enough to
stop reproducing the bug, and this milestone's own output is what makes the difference visible.

Not fixed here, and deliberately: making a weak model emit structured tool calls is a prompt/model
question (a smaller tool set via `DEEPAGENTS_LEAN_TOOLS`, a different tag, or a tool-call repair
layer), not a tracing one. M7's job was to make it legible, which it now does.

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
