# Milestone 3 — Human-in-the-Loop (HITL)

> **Status:** 📋 Spec — full build spec, not a stub. Successor to `docs/milestones/complete/milestone2.md`
> (present/past memory, ✅ Built). Wins over `design_doc.md` for "what we build next." This milestone
> promotes the `design_doc.md` **§9 Human-in-the-Loop** design and the **§3 pause action tier** from
> planned prose into a built, tested slice, and pulls in the two `design_doc.md` §12 items that HITL
> structurally depends on (**§12.4 provider resilience**, **§12.3 headless mode**). Read §9 first.

---

## 1. Goal & Definition of Done

Give a running session a way to **stop and ask a human** — and give the human a way to answer — at
any point in the graph, durably, without losing the turn. Today the harness has no interrupt path:
an ambiguous prompt, a missing price, a provider error, or a blocked command either gets guessed at,
logged silently, or crashes the turn. Milestone 3 adds the one primitive that resolves all four.

The design is fully specified in `design_doc.md` §9 (**one interrupt spine, three trigger sources,
one human channel**) and §3 (the **pause** action tier). This milestone does not re-litigate that
design; it schedules the build and closes the open forks (§6 below).

**Done when** a session can suspend on any of the three sources, present a structured prompt on the
in-container REPL channel, block for a typed reply, resume from the persisted checkpoint (including
across a process restart), and route on the answer — with tests covering each source, and with the
two prerequisite layers (resilience, headless) in place so the provider-error interrupt and the
no-human-present policy are real, not stubbed.

## 2. Why this is its own milestone (not M2)

M2 shipped **present/past memory** — orthogonal to HITL. The two share one substrate, though: the
SqliteSaver checkpoint M2 leaned on for the *present* thread is the same durable-suspend/resume
mechanism HITL's `interrupt()` spine rides on. HITL is the natural next milestone precisely because
that checkpoint plumbing is already built and exercised.

## 3. What we pulled in from `design_doc.md` §12 (and what we left out)

M3 is HITL-themed, but two §12 operational-hardening items are **structural dependencies** of the
interrupt spine, so they are promoted into this milestone rather than left as separate work:

- **§12.4 provider resilience** — the source-3 *provider-error* interrupt is defined as firing *after*
  retry/backoff is exhausted. Without a backoff layer there is nothing to escalate from. Built as
  **slice P1** (prerequisite).
- **§12.3 headless one-shot mode** — slice 5's `timeout_policy` and the "no human present" deadlock
  question only have meaning on a real headless execution path. Built as **slice P2** (prerequisite).
- **§12.7 cost/telemetry persistence** — only the *interrupt audit* sliver is pulled in (slice 7),
  closing the "audit trail" fork. The fuller `usage.jsonl` sink stays in §12.7.

Deliberately **left out** of M3 (adjacent but not HITL, not next): §12.1 CI, §12.2 `harness doctor`,
§12.5 thread lifecycle (already M2), §12.6 skills/memories, §13 file-read middleware,
`features/workspace_visibility.md`, `specs/energy.md`, and all of §10/§11. The one exception is that
the source-3 *permission/security-gate* interrupt (slice 4) rides on the §2/§10 path-guard / NetJail
gates that already exist — it consumes them, it does not build them.

## 4. Scope (slices, in build order)

Slices are ordered by dependency. Each is independently shippable. Detailed design lives in
`design_doc.md` §9 unless noted. **P-slices are prerequisites**; **S-slices are the spine.**

### P1 — Provider resilience (retry/backoff + context-overflow) — *prereq for S4*
Promotes `design_doc.md` §12.4. Two narrow, safe behaviours around the per-turn model invoke:
- **Transient-error retry** — bounded exponential backoff (3 tries, jitter) for retryable statuses
  (429 / 5xx / connection reset), caps from env (`DEEPAGENTS_MAX_RETRIES`, `DEEPAGENTS_RETRY_BASE`).
- **Context-overflow stopgap** — catch the context-length error, trim/summarize oldest turns, retry
  once; explicitly flagged as the pre-§7 (Headroom) placeholder.

New `harness/resilience.py` (pure backoff/classification helpers, host-testable); wraps `run_turn` in
`harness/cli.py`. **S4 depends on this:** the provider-error interrupt is raised only once backoff is
exhausted, from the same except clauses.

### P2 — Headless one-shot mode — *prereq for S5*
Promotes `design_doc.md` §12.3. A first-class `--headless` path in `cli.main` (`run_batch` beside
`run_repl`): run task(s) to completion, run the `session.end` git-pr workflow, emit one structured
JSON result on stdout (final message, token/cost totals, thread id, branch, PR URL, exit code); stage
markers stay on stderr. **S5 binds here:** the non-TTY / no-human-present interrupt behaviour is
defined and tested on this path.

### S1 — Interrupt spine + human channel — *the core primitive*
Wire LangGraph `interrupt()` over the existing `checkpoints.sqlite`. Define the interrupt request
object `{id, kind, prompt, options, context, default, timeout_policy}`, `kind ∈ {approve, choose,
input, resolve}`, with a **stable `id`** (uuid) so a resume value binds to the right interrupt
(§6, keying). Surface/collect on the in-container REPL (`harness/cli.py`) — the only channel that
exists today. **Acceptance bar:** resume-across-process-restart — the interrupt request itself
round-trips through `checkpoints.sqlite`, so a container that dies while waiting re-surfaces the
*same* prompt (not a lost or duplicated one) on restart.

### S2 — Deterministic pause action tier (§3)
A `pause` step type in the workflow engine (`harness/workflows.py`), gated by the existing
deterministic predicate gates (no model in the loop). Ships the `autonomy_level` presets
(`strict`/`guided`/`autonomous`) as bundled pause-workflow sets, and `review_triggers` path/keyword
matching (match contract in §6). Fires at `tool.start` (gate a tool call) and `session.end` (gate the
PR) first.

### S3 — Agent-initiated `ask_human` tool
A Deep Agents tool the agent calls when *it* decides it is blocked (ambiguous requirements, missing
credential, design fork it should not guess). Same interrupt object, same channel. Deterministic-gate
vs. `ask_human` collision resolved in §6.

### S4 — System-event interrupts — *depends on P1*
Promote three existing failure/uncertainty points to interrupts, toggled by `system_interrupts` in
`.harness-config.yaml`:
- **Missing price** (M1 cost ledger) — surface before untracked spend accrues (suppression rule in §6).
- **Provider error** (P1 / §12.4) — after retry/backoff is exhausted: *retry / switch provider / abort*.
- **Permission / security gate** (§2, §10) — path-guard / NetJail / bwrap denial becomes a resolvable
  *allow once / deny* prompt. Consumes the existing gates; does not build them.

### S5 — Policy & headless behavior — *depends on P2*
`interruption_policy` (`blocking` | `shadow`) and per-request `timeout_policy` so an unattended
headless run (P2) has defined behavior with no human present (deadlock resolution in §6).

### S6 — REPL input ergonomics (`prompt_toolkit`)
- **PR-a — ✅ BUILT & merged** (`feat/repl-prompt-toolkit`). `harness/cli.py` now routes the prompt
  read through `_read_prompt()` / `_make_prompt_session()`: line-oriented `prompt_toolkit`
  `PromptSession` with multi-line input (Ctrl-J / Alt+Enter / bracketed paste), persistent history +
  Ctrl-R reverse search at `<state-dir>/repl_history`, and slash-command preview completion — gated on
  `sys.stdin.isatty()`, degrading to plain `input()` off-TTY. Kept line-oriented, so the M1
  stdout-answer / stderr-stage-marker split is untouched. The full-screen host TUI (`textual`) is
  still deferred; the `kind`-keyed request object carries the input contract forward.
- **PR-b — after S1.** The `choose`-kind single-line arrow-key select menu, over the S1 request
  object, replacing typed option indices. Falls back to a numbered-text prompt off-TTY.

### S7 — Interrupt audit trail — *slice of §12.7*
Persist every interrupt + its human response to telemetry for reproducibility/replay: append a
scrubbed record (interrupt `id`, `kind`, prompt, resolved value, source, timestamps) to
`<workspace>/.agent_telemetry/interrupts.jsonl` (git-ignored, excluded by the git-pr workflow, secret-
scrubbed per §10). The human reply re-enters agent context **as a tool result** (trusted input,
resolution in §6). Off unless HITL is active — preserves the removable-seam / byte-for-byte-MVP
contract.

## 5. Config surface

The `.harness-config.yaml` from `design_doc.md` §9 (`autonomy_level`, `review_triggers`,
`interruption_policy`, `system_interrupts`), plus the two prerequisite layers' existing knobs:
P1's `DEEPAGENTS_MAX_RETRIES` / `DEEPAGENTS_RETRY_BASE` env, P2's `--headless` flag. No new
`.harness-config.yaml` keys beyond what §9 defines.

```yaml
autonomy_level: guided            # strict | guided | autonomous — preset pause-workflow set
review_triggers:                  # force a pause regardless of level
  - { on: path,    pattern: "*.env" }        # see §6 for the {on, pattern} contract
  - { on: command, pattern: "rm -rf*" }
interruption_policy: blocking     # blocking | shadow
system_interrupts:                # which harness events raise (vs. log/crash)
  missing_price: true
  provider_error: true
  permission_denied: true
```

## 6. Design decisions (open forks, now closed)

The stub left these as open questions. This spec closes them so the slices are buildable. Each
decision names the slice it unblocks. One fork (shadow-mode UX) remains genuinely open and is flagged.

### Channel & presentation (S1, S5, S6)
- **Host channel scope — DECIDED: REPL-only for M3.** The Rich prompt / batched host-TUI review
  panel stays deferred. The S1 request object is channel-agnostic (a `render(request)` contract), so
  the host prompt is a later *renderer*, not a re-model.
- **Non-terminal-friendly rendering — DECIDED: cap + expand.** A large `context` payload (diff /
  command) is truncated in the REPL to N lines with a `… +M lines — /show to expand` footer; `/show`
  prints the full payload. Full paged rendering is deferred to the host TUI. S6 PR-a's multi-line
  input already covers the *collect* side.

### Interrupt identity & resume correctness (S1)
- **Keying — DECIDED: stable `id`.** The request object carries a uuid `id`; the resume protocol
  references it rather than relying on LangGraph's positional resume, which is fragile once shadow
  mode batches more than one.
- **Restart mid-interrupt — DECIDED (this is the S1 acceptance bar).** The interrupt request itself
  (not just graph state) round-trips through `checkpoints.sqlite`; a container that dies while
  waiting re-surfaces the *same* keyed prompt on restart.

### Gate semantics & collision (S2, S3)
- **`review_triggers` match target — DECIDED: per-trigger `{on, pattern}`.** `on ∈ {tool_name, arg,
  path, command}`; `pattern` is a glob by default (a `re:` prefix opts into regex). `path` matches
  files in the pending diff; `command` matches the raw shell string.
- **Deterministic gate vs. `ask_human` collision — DECIDED: gate wins, dedupe.** If a `tool.start`
  pause already gates a call, a same-step `ask_human` for the same tool-call id is suppressed (one
  prompt, not two). `ask_human` still fires for anything no gate covers.

### Headless, budget & audit (P2, S4, S5, S7)
- **Headless default & deadlock — DECIDED: fail-closed.** On non-TTY runs `timeout_policy` defaults to
  `default` (fall through to the request's `default` value). `strict` + `blocking` on a non-TTY run
  has no valid fall-through, so it **aborts with a distinct non-zero exit code** rather than blocking
  forever — a stuck pause in CI is worse than a labelled abort.
- **Does the clock keep ticking? — DECIDED: pause the clock.** The M1 budget / wall-clock / resource
  caps **pause on interrupt and resume on reply**, so a human at lunch cannot trip a runaway-session
  abort. Requires making the cost middleware interrupt-aware (small change, tracked under S1).
- **Missing-price interrupt in a keyless run — DECIDED: suppress + dedupe.**
  `system_interrupts.missing_price` auto-suppresses when the *whole* run is keyless (NULL cost by
  design); otherwise it dedupes to one prompt per model per session.
- **Audit trail — DECIDED: yes (S7).** Every interrupt + reply persists to `interrupts.jsonl`
  (scrubbed). The human reply counts as trusted input and is fed back **as a tool result**, not a
  system note, so it is attributable in the trace.

### Human edit path (S3)
- **`resolve`/`edit` kind — DECIDED: edit re-runs the gates.** `kind == resolve` lets the human *edit*
  the proposed tool call / diff (not just approve/deny). The edited value re-enters the graph as the
  step's routed value, and **an edited command re-runs the deterministic gates** — an edit could
  re-introduce a `*.env` write, so it must not bypass `review_triggers`.

### Remaining open fork (blocks S5 only)
- **Shadow-mode ordering & resume — OPEN.** When several interrupts fire mid-run and collect at
  `agent.end`/`session.end`, the exact UX is not yet pinned: one paged list vs. sequential; whether
  the human may answer out of order; and whether the graph replays from the earliest unanswered
  interrupt or each interrupt carries its own keyed resume point. **Recommended default** (to validate
  before S5 builds): one paged list, answer in `id` order, per-interrupt keyed resume (each resumes at
  its own checkpoint, leaning on the S1 keying decision). This is the one item that needs concrete UX
  sign-off; the `blocking` path (the M3 acceptance bar) does not depend on it.
