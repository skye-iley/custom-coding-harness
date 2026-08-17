"""Raw trace sink — the per-model-call record writer (Milestone 7).

What this module is for: `final_message_text` (`agent.py`) keeps `{"type":
"text"}` parts and *deliberately drops* reasoning/thinking blocks and unknown
part shapes, because that is correct for a human-facing answer. A raw trace is
the same data with that transform **skipped** — everything the harness hands the
model and everything the model hands back, nothing dropped on the way to the
screen or the file (`milestone7.md` §1/§7).

Two rules shape every function here:

* **Bodies verbatim, additions structural only** (invariants 1–2). Separators,
  indices and counts are all this module adds. No truncation, no summary, no
  interpretation — a trace that elides the long tool result hides the reason the
  model got confused.
* **Nothing dropped, ever** (invariants 5a–5c). An unrecognised block shape is
  dumped as its literal JSON/`repr` rather than skipped; a block nobody
  anticipated is the single most interesting thing that can appear in a trace,
  and silence about it is the one unrecoverable failure.

**Import profile (invariant 21): stdlib plus `harness.scrub`, nothing else from
the package, and no langchain.** That keeps this module in the host test tier.
The middleware that needs the langchain base class lives in `agent.py` — the
same split `telemetry.py` vs `cli.TelemetryMiddleware` uses, for the same reason.
Everything here is therefore duck-typed: it reads `getattr`/`dict` shapes off
whatever the model layer produced, never an imported type.

**Fidelity is L1, message level** (`milestone7.md` §3): the final `system_message`
/ `messages` / `tools` at the innermost middleware seam, and the complete reply
object. It is **not** the HTTP body (L2) and **not** the model server's
template-rendered token string (L3, not reachable client-side — Ollama renders
the chat template inside `/api/chat`). The record header says so, so an operator
cannot read this as something it is not.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from harness.scrub import scrub

# --- the knob's values --------------------------------------------------------

MODE_OFF = "off"
MODE_FILE = "file"
MODE_CONSOLE = "console"
MODE_BOTH = "both"
#: The `raw_trace` FieldSpec's `choices`, in display order. `config.py` imports
#: this rather than re-spelling the list, so the enum has one declaration.
MODES = (MODE_OFF, MODE_FILE, MODE_CONSOLE, MODE_BOTH)

#: Modes that write the file sink / print to the console. Membership tests, not
#: `==` chains, so adding a mode is one edit here.
_FILE_MODES = (MODE_FILE, MODE_BOTH)
_CONSOLE_MODES = (MODE_CONSOLE, MODE_BOTH)

TRACE_DIR = "raw-trace"

# Whole-file cap, a module constant rather than a knob (fork §13.5). M5.1 makes
# adding a knob a one-line edit, so the argument is not cost — it is that 64 MiB
# of trace is far past the point where the answer is in the first few records.
# Promote it if a real sweep ever hits it.
DEFAULT_CAP_BYTES = 64 * 1024 * 1024

# Fidelity marker in every header (`milestone7.md` §3, risk "false confidence").
FIDELITY = "L1(message-level; not the wire body, not the chat template)"


def console_modes() -> tuple[str, ...]:
    """Modes in which the REPL prints the record **instead of** the rendered
    answer (§6). Exported so `cli` asks this module rather than re-listing."""
    return _CONSOLE_MODES


def trace_path(state_dir: Path | str, run_id: str) -> Path:
    """``<state-dir>/raw-trace/<run_id>.log`` — one file per run (fork §13.6).

    The **state dir**, never the workspace: same placement as ``past.sqlite`` /
    ``denials.jsonl`` / ``usage.jsonl``. Unlike those, the reason is the
    operator's disk rather than the agent's reach — the file is *meant* to hold
    prompt text — but the containment is the same, and stated at its real
    strength: file-tool-proof always (pathguard plus the workspace-rooted backend
    cannot address the path), shell-proof **only** under ``DEEPAGENTS_JAIL=1``.
    """
    return Path(state_dir) / TRACE_DIR / f"{run_id}.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- rendering ----------------------------------------------------------------
#
# Every `format_*` below is pure: object in, text out, no I/O and no mode check.
# The sink decides where the text goes; these decide what it says.


def _json(value) -> str:
    """A value as literal JSON, falling back to `repr` for anything unserialisable.

    The fallback is the point: an object the JSON encoder refuses is exactly the
    case where dropping the field would hide the interesting thing (invariant 5b).
    `default=str` handles the common near-misses (datetimes, enums) without
    losing them.
    """
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _get(obj, key, default=None):
    """Read `key` off a dict or an attribute off an object — the messages,
    blocks and responses here arrive as either, depending on the provider."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def model_label(model) -> str:
    """A human label for the model a call was made with.

    Duck-typed across the shapes a `ModelRequest.model` carries: a chat-model
    client (`model`/`model_name`), a bare provider string (native providers hand
    `create_deep_agent` a `str`), or something else entirely.
    """
    if isinstance(model, str):
        return model
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


# Block `type` values whose payload is unreadable by construction. Matched as
# substrings because providers spell them differently (`redacted_thinking`,
# `encrypted_reasoning`, ...) and a new spelling must not become silence.
_ENCRYPTED_TYPE_MARKERS = ("redacted", "encrypted")
# Keys that carry an opaque payload even when the type name says nothing.
_ENCRYPTED_PAYLOAD_KEYS = ("data", "signature", "ciphertext", "encrypted_content")

# Block types that carry their body in a plain text field, and where that field
# is. Rendering these literally (rather than as JSON) is what keeps a prompt
# readable; any *other* key on the block is still dumped, so nothing is lost.
_TEXT_BEARING = {
    "text": ("text",),
    "reasoning": ("reasoning", "text"),
    "thinking": ("thinking", "text"),
    "reasoning_content": ("reasoning_content", "text"),
}


def _encrypted_size(block: dict) -> int | None:
    """Byte size of an encrypted block's payload, or None if it isn't one.

    Size and position are the diagnostically useful facts; the ciphertext is not
    (invariant 5c). Dumping kilobytes of base64 into a human-facing trace buries
    the blocks that *are* readable.
    """
    btype = str(block.get("type") or "")
    payload_keys = [k for k in _ENCRYPTED_PAYLOAD_KEYS if k in block]
    marked = any(m in btype.lower() for m in _ENCRYPTED_TYPE_MARKERS)
    if not marked and not payload_keys:
        return None
    if payload_keys:
        return sum(len(str(block[k]).encode("utf-8")) for k in payload_keys)
    return len(_json(block).encode("utf-8"))


def format_block(index: int, block) -> str:
    """One content block, labelled with its index and type.

    Four cases, in priority order, and **none of them is "skip"**:

    1. an encrypted/redacted block -> a typed placeholder carrying its byte size,
       recorded *in position* (invariant 5c);
    2. a text-bearing block -> its text verbatim, plus any remaining keys as JSON
       so a provider extension isn't silently dropped;
    3. any other dict -> the whole block as literal JSON;
    4. anything else (a bare string, an object) -> the string itself, or `repr`.
    """
    if isinstance(block, str):
        return f"[block {index}] text:\n{block}"
    if not isinstance(block, dict):
        return f"[block {index}] {type(block).__name__}: {block!r}"

    btype = str(block.get("type") or "?")
    size = _encrypted_size(block)
    if size is not None:
        return f"[block {index}] <encrypted reasoning block: type={btype}, {size} bytes>"

    text_keys = _TEXT_BEARING.get(btype)
    if text_keys:
        for key in text_keys:
            if key in block:
                rest = {k: v for k, v in block.items() if k not in ("type", key)}
                out = f"[block {index}] {btype}:\n{block[key]}"
                # Not decoration: a provider that adds a field to a known block
                # shape would otherwise vanish from the trace.
                return out + (f"\n+ extra: {_json(rest)}" if rest else "")

    return f"[block {index}] {btype}: {_json(block)}"


def format_content(content, *, indent: str = "") -> str:
    """A message's `content`, whether it is a string or a list of blocks."""
    if content is None:
        return f"{indent}(no content)"
    if isinstance(content, str):
        return _indent(content, indent)
    if isinstance(content, (list, tuple)):
        if not content:
            return f"{indent}(empty content list)"
        return "\n".join(
            _indent(format_block(i, block), indent) for i, block in enumerate(content)
        )
    return _indent(_json(content), indent)


def _indent(text: str, indent: str) -> str:
    if not indent:
        return text
    return "\n".join(indent + line for line in str(text).splitlines() or [""])


def _message_role(message) -> str:
    """`human` / `ai` / `tool` / `system`, however the message spells it."""
    for attr in ("type", "role"):
        value = _get(message, attr)
        if isinstance(value, str) and value:
            return value
    return type(message).__name__


def format_message(index: int, message) -> str:
    """One history message: role header, content verbatim, then its tool calls.

    Tool calls are rendered from the message's own `tool_calls` list with **raw
    args, pre-parse** — the malformed JSON a weak model emitted is the answer to
    "why did this tool call do nothing", so it must not be repaired on the way to
    the trace.
    """
    role = _message_role(message)
    header = f"[{index}] {role}:"
    name = _get(message, "name")
    call_id = _get(message, "tool_call_id")
    tags = [t for t in (name, f"id={call_id}" if call_id else None) if t]
    if tags:
        header = f"[{index}] {role}: ({', '.join(tags)})"

    parts = [header, format_content(_get(message, "content"))]
    for label, calls in (
        ("tool_call", _get(message, "tool_calls") or []),
        ("INVALID tool_call", _get(message, "invalid_tool_calls") or []),
    ):
        for call in calls:
            parts.append(f"  {label}: {_json(call)}")
    return "\n".join(parts)


def format_system(system) -> str:
    """The system prompt, verbatim.

    `ModelRequest.system_message` is a **`SystemMessage` object**, not a string,
    on the path this actually runs (measured against a real ollama turn, not
    inferred). Rendering it straight through `format_content` produced
    ``content=[{'type': 'text', 'text': 'You are an expert...`` — the whole
    prompt escaped inside a `repr`, which is "nothing dropped" but not verbatim,
    and unreadable exactly where readability is the point. Unwrap to `.content`
    first; a plain string still passes through untouched.
    """
    if system is None:
        return "(none)"
    content = _get(system, "content")
    return format_content(content if content is not None else system)


def format_header(*, run_id: str, turn: int, call: int, model, ts: str | None = None) -> str:
    """The record's opening rule.

    Records are labelled `run_id / turn N / call M` — `N` bracketed by
    `cli.run_turn` (never `before_agent`, which fires once per *invoke*: a
    resilience retry and every HITL resume are more invokes of the same turn),
    `M` counted within the turn, since one turn legitimately makes many model
    calls (invariant 4).
    """
    return (
        f"===== run {run_id} | turn {turn} | call {call} | "
        f"{ts or _now_iso()} | {model_label(model)} | fidelity={FIDELITY} ====="
    )


def format_request(request) -> str:
    """The outgoing half: system prompt, full message history, tool schemas.

    Read off the `ModelRequest` at the **innermost** middleware seam, so this is
    the final view — after `_ExcludeToolsMiddleware` has filtered the tool list.
    A trace taken one layer out would log tools the model never received, which
    is the exact bug class this whole feature exists to diagnose.
    """
    out: list[str] = []

    system = _get(request, "system_message")
    out.append("--- system ---")
    out.append(format_system(system))

    messages = list(_get(request, "messages") or [])
    out.append(f"--- messages ({len(messages)}) ---")
    for i, message in enumerate(messages):
        out.append(format_message(i, message))

    tools = list(_get(request, "tools") or [])
    out.append(f"--- tools ({len(tools)}) ---")
    for tool in tools:
        out.append(f"{_tool_name(tool)}: {_json(_tool_schema(tool))}")

    for label, key in (("tool_choice", "tool_choice"), ("response_format", "response_format")):
        value = _get(request, key)
        if value is not None:
            out.append(f"--- {label} ---\n{_json(value)}")

    return "\n".join(out)


def _tool_name(tool) -> str:
    name = _get(tool, "name")
    if name:
        return str(name)
    fn = _get(tool, "function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return type(tool).__name__


def _tool_schema(tool):
    """A bound tool's literal schema, across the shapes a request carries.

    A `BaseTool` exposes `args_schema`; an OpenAI-style dict is already the
    schema. Falls through to the object itself so an unrecognised tool shape is
    dumped rather than reduced to its name.
    """
    if isinstance(tool, dict):
        return tool
    schema = getattr(tool, "args_schema", None)
    if schema is not None:
        for attr in ("model_json_schema", "schema"):
            fn = getattr(schema, attr, None)
            if callable(fn):
                try:
                    return fn()
                except Exception:  # noqa: BLE001 - fall through to the raw object
                    pass
        return schema
    description = getattr(tool, "description", None)
    if description is not None:
        return {"description": description}
    return tool


_NO_OUTPUT_WARNING = (
    "!! output_tokens={n} but this message carries no content, no tool calls and no "
    "additional_kwargs -- generated tokens were dropped before the message object "
    "existed (a provider-parser drop). L1 cannot show WHAT was lost; see the wire body."
)

# Neither None nor "" nor [] is "the model said something". A dict/object content
# is left alone -- an unknown container is not evidence of emptiness.
def _is_blank_content(content) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, (list, tuple)):
        return not content
    return False


def _dropped_output_warning(message) -> str:
    """Flag output tokens that landed in no field of the message.

    A message reporting ``output_tokens > 0`` while its content, tool calls and
    ``additional_kwargs`` are all empty generated text that the provider's own
    parser then discarded: the tokens are billed, counted by M6, and unreadable
    by anyone. The drop happens *before* the message object exists, so it is
    invisible at L1 by construction — the trace cannot show what was lost, but it
    can and must say that something was, rather than rendering a blank section
    that reads like the model stayed silent.

    Measured case (2026-08-17, `milestone7.md` §3): ``langchain-ollama`` 1.1.0
    captures Ollama's ``message.thinking`` only when ``reasoning`` is truthy and
    drops it otherwise, so a thinking-by-default tag produced 450 output tokens,
    an empty ``AIMessage``, and a blank REPL turn with nothing anywhere to say so.
    """
    usage = _get(message, "usage_metadata")
    if not isinstance(usage, dict):
        return ""
    try:
        produced = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return ""
    if produced <= 0:
        return ""
    if not _is_blank_content(_get(message, "content")):
        return ""
    for key in ("tool_calls", "invalid_tool_calls"):
        if list(_get(message, key) or []):
            return ""
    if _get(message, "additional_kwargs"):
        return ""
    return _NO_OUTPUT_WARNING.format(n=produced)


def format_response(response, *, elapsed_ms: float) -> str:
    """The reply half — **the whole returned message**, nothing dropped.

    Directly opposes `final_message_text`, which keeps text parts and drops the
    rest: every content block appears in order with its index and type, plus
    `tool_calls` (raw args), invalid tool calls as their own section,
    `additional_kwargs`, `response_metadata`, `usage_metadata` and the finish
    reason (invariant 5a). Providers put refusals, safety verdicts, logprobs and
    model-version drift in those metadata bags, and none of it reaches a human
    today.
    """
    messages = _response_messages(response)
    out = [f"--- response ({int(elapsed_ms)} ms) | finish_reason={_finish_reason(messages)} ---"]
    if not messages:
        # Not an error and not nothing: record the object as-is rather than
        # printing an empty section that reads like the model said nothing.
        out.append(_json(response))
        return "\n".join(out)

    for i, message in enumerate(messages):
        if len(messages) > 1:
            out.append(f"[message {i}] {_message_role(message)}")
        # Before the content, not after: a reader scanning for "what did the model
        # say" stops at the blank and never reaches the metadata that contradicts it.
        warning = _dropped_output_warning(message)
        if warning:
            out.append(warning)
        out.append(format_content(_get(message, "content")))

        for label, key in (
            ("tool_calls", "tool_calls"),
            ("invalid_tool_calls", "invalid_tool_calls"),
        ):
            calls = list(_get(message, key) or [])
            out.append(f"--- {label} ({len(calls)}) ---")
            for j, call in enumerate(calls):
                out.append(f"[{j}] {_json(call)}")

        out.append("--- response metadata ---")
        for key in ("usage_metadata", "response_metadata", "additional_kwargs"):
            out.append(f"{key}: {_json(_get(message, key))}")

    structured = _get(response, "structured_response")
    if structured is not None:
        out.append(f"--- structured_response ---\n{_json(structured)}")
    return "\n".join(out)


def _response_messages(response) -> list:
    """The message(s) a handler's return carries.

    `ModelResponse.result` is a list; a middleware test double may hand back a
    bare message or a `{"messages": [...]}` dict. All three are the same thing
    for tracing purposes, so normalise rather than require one.
    """
    if response is None:
        return []
    result = _get(response, "result")
    if result is None:
        result = _get(response, "messages")
    if result is None:
        return [response] if _get(response, "content") is not None else []
    if isinstance(result, (list, tuple)):
        return list(result)
    return [result]


def _finish_reason(messages) -> str:
    for message in reversed(messages):
        meta = _get(message, "response_metadata") or {}
        if isinstance(meta, dict):
            for key in ("finish_reason", "stop_reason", "done_reason"):
                if meta.get(key):
                    return str(meta[key])
    return "unknown"


def format_error(exc: BaseException, *, elapsed_ms: float) -> str:
    """A model call that raised still gets a record (invariant 3): the request
    half is already written, and this marks the outcome. A provider error with
    no record is exactly the trace an operator most wants."""
    return (
        f"--- response ({int(elapsed_ms)} ms) | RAISED ---\n"
        f"{type(exc).__name__}: {exc}"
    )


def format_footer() -> str:
    return "====="


# --- the sink -----------------------------------------------------------------


class TraceSink:
    """Three-phase record writer: ``open_record`` / ``append`` / ``close_record``.

    v1 always drives all three in one shot, so why the phases? Because an API
    that only works when the whole body is known in advance is precisely what a
    streaming implementation would have to rewrite (`milestone7.md` §9,
    invariant 23). Driving the same content through N appends produces
    byte-identical output to driving it in one, which is what makes a future
    `AgentMiddleware.transformers` path a new *writer* rather than a new format.

    ``mode`` is a plain mutable attribute, read at the top of every method: a
    live ``/config set raw_trace`` writes it, and because ``/config`` is
    processed between turns a change can never land halfway through a call
    (invariant 20a).
    """

    def __init__(
        self,
        mode: str = MODE_OFF,
        path: Path | str | None = None,
        *,
        run_id: str = "unknown",
        stream=None,
        cap_bytes: int = DEFAULT_CAP_BYTES,
        env: dict | None = None,
    ):
        self.mode = mode
        self.path = Path(path) if path is not None else None
        self.run_id = run_id
        # Resolved per write, not captured here: pytest's capsys swaps
        # sys.stdout after construction, and so does anything else that wraps
        # the streams late.
        self._stream = stream
        self.cap_bytes = cap_bytes
        self.env = env
        self.written = 0
        self._capped = False
        self._warned = False  # one sink warning per run, not per record

    # --- destinations ---------------------------------------------------------

    @property
    def stream(self):
        return self._stream if self._stream is not None else sys.stdout

    def _emit(self, text: str) -> None:
        """One chunk to whichever destinations the mode names.

        **Scrubbed here, once, before any byte is written *or* printed**
        (invariant 13) — so the file and the console cannot disagree about what
        is safe. `scrub` is a backstop, not a boundary: it redacts credential env
        values and common key shapes, and nothing else. Redaction is visible
        (`***REDACTED***`) so a reader can tell altered text from text the model
        genuinely saw.
        """
        text = scrub(text, self.env) or ""
        if self.mode in _CONSOLE_MODES:
            # Uncapped deliberately: a cap here would silently hide the thing the
            # operator asked to see, and the terminal is their problem.
            print(text, file=self.stream, flush=True)
        if self.mode in _FILE_MODES:
            self._write_file(text + "\n")

    def _write_file(self, text: str) -> None:
        """Append to the run's file, honouring the whole-file cap.

        Every path here is wrapped: a read-only state dir or a full disk degrades
        to **one** stderr warning per run and the model call returns its result
        unchanged (invariant 16). Tracing is never load-bearing.
        """
        if self.path is None or self._capped:
            return
        payload = text.encode("utf-8")
        try:
            if self.written + len(payload) > self.cap_bytes:
                self._capped = True
                payload = (
                    f"[raw-trace] cap reached ({self.cap_bytes} bytes) — "
                    "no further records will be written to this file.\n"
                ).encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as fh:
                fh.write(payload)
            self.written += len(payload)
        except OSError as exc:
            self._warn(f"raw-trace: {exc}")

    def _warn(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            print(f"[harness] {message}", file=sys.stderr)

    # --- the three phases -----------------------------------------------------

    def open_record(self, header: str) -> None:
        self._emit(header)

    def append(self, body: str) -> None:
        self._emit(body)

    def close_record(self, footer: str = "") -> None:
        self._emit(footer or format_footer())
