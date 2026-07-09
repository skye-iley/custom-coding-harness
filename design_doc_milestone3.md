# Milestone 3 — Human-in-the-Loop (HITL) — *stub*

> **Status:** 🔬 Stub — scope sketch, not a full spec. Successor to `design_doc_milestone2.md`
> (present/past memory, ✅ Built). Wins over `design_doc.md` for "what we build next" once a fuller
> spec lands here. This milestone promotes the `design_doc.md` **§9 Human-in-the-Loop** design and
> the **§3 pause action tier** from planned prose into a built, tested slice.

---

## 1. Goal & Definition of Done

Give a running session a way to **stop and ask a human** — and give the human a way to answer — at
any point in the graph, durably, without losing the turn. Today the harness has no interrupt path:
an ambiguous prompt, a missing price, a provider error, or a blocked command either gets guessed at,
logged silently, or crashes the turn. Milestone 3 adds the one primitive that resolves all four.

The design is fully specified in `design_doc.md` §9 (**one interrupt spine, three trigger sources,
one human channel**) and §3 (the **pause** action tier). This milestone does not re-litigate that
design; it schedules the build. Read §9 first.

**Done when** a session can suspend on any of the three sources, present a structured prompt on the
in-container REPL channel, block for a typed reply, resume from the persisted checkpoint (including
across a process restart), and route on the answer — with tests covering each source.

## 2. Why this is its own milestone (not M2)

M2 shipped **present/past memory** — orthogonal to HITL. The two share one substrate, though: the
SqliteSaver checkpoint M2 leaned on for the *present* thread is the same durable-suspend/resume
mechanism HITL's `interrupt()` spine rides on. HITL is the natural next milestone precisely because
that checkpoint plumbing is already built and exercised.

## 3. Scope (slices, roughly by leverage)

Each slice is independently shippable. Detailed design lives in `design_doc.md` §9 unless noted.

1. **Interrupt spine + human channel.** Wire LangGraph `interrupt()` over the existing
   `checkpoints.sqlite`. Define the interrupt request object
   `{kind, prompt, options, context, default, timeout_policy}`. Surface/collect on the in-container
   REPL (`harness/cli.py`) — the only channel that exists today. Resume-across-restart is the
   acceptance bar for this slice.
2. **Deterministic pause action tier (§3).** A `pause` step type in the workflow engine
   (`harness/workflows.py`), gated by the existing deterministic predicate gates. Ships the
   `autonomy_level` presets (`strict`/`guided`/`autonomous`) as bundled pause-workflow sets and
   `review_triggers` path/keyword matching. Fires at `tool.start` and `session.end` first.
3. **Agent-initiated `ask_human` tool.** A Deep Agents tool the agent calls when it decides it is
   blocked. Same interrupt object, same channel.
4. **System-event interrupts.** Promote three existing failure/uncertainty points to interrupts,
   toggled by `system_interrupts` in `.harness-config.yaml`:
   - **Missing price** (M1 cost ledger) — surface before spend accrues untracked.
   - **Provider error** (§12 resilience) — after retry/backoff is exhausted.
   - **Permission / security gate** (§2, §10) — path-guard / NetJail / bwrap denial.
5. **Policy & headless behavior.** `interruption_policy` (`blocking` | `shadow`) and per-request
   `timeout_policy` so an unattended headless run (§12) has defined behavior with no human present.

## 4. Config surface

The `.harness-config.yaml` from `design_doc.md` §9 (`autonomy_level`, `review_triggers`,
`interruption_policy`, `system_interrupts`). No new config keys beyond what §9 defines.

## 5. Open questions

Each of these is a design fork that must close before the slice it blocks is spec-complete. Grouped
by the slice they gate.

### Channel & presentation (slices 1, 5)

- **Host channel scope.** M3 targets the in-container REPL channel only. The Rich prompt / batched
  `shadow`-mode review panel depends on the §9 host CLI/TUI, still ⬜ Planned — decide whether M3
  stops at the REPL or pulls a minimal host prompt forward. Leaning: REPL-only for M3, host channel
  deferred, but the interrupt request object (slice 1) must be channel-agnostic so the host prompt
  is a later renderer, not a re-model.
- **Shadow-mode ordering & resume.** When several interrupts fire mid-run and collect at
  `agent.end`/`session.end`, decide how they present (one list, or paged), whether the human can
  answer out of order, and how partial answers resume — does the graph replay from the earliest
  unanswered interrupt, or does each interrupt carry its own resume point? Needs concrete UX before
  slice 5.
- **Non-terminal-friendly rendering.** The `context` payload can be a large diff or command. Decide
  a truncation/paging contract for the REPL so a big interrupt doesn't flood the `you>` prompt.

### Interrupt identity & resume correctness (slice 1)

- **Keying.** With multiple concurrent/pending interrupts, how does a resume value bind to the
  *right* interrupt? Needs a stable interrupt id in the request object and a resume protocol that
  references it — LangGraph resumes positionally by default, which is fragile once shadow mode
  batches more than one.
- **Restart mid-interrupt.** Acceptance bar for slice 1 is resume-across-process-restart. Confirm
  the interrupt request itself (not just graph state) round-trips through `checkpoints.sqlite`, so a
  container that dies while waiting for a human re-surfaces the *same* prompt on restart, not a lost
  or duplicated one.

### Gate semantics & collision (slices 2, 3)

- **`review_triggers` match target.** Patterns match against *what* — the tool name, the tool args,
  file paths in the pending diff, the raw shell command string? And glob vs. regex. A `*.env`
  trigger is meaningless until this is pinned; likely a per-trigger `{on, pattern}` shape.
- **Deterministic gate vs. agent `ask_human` collision.** If a `tool.start` pause already gates a
  call and the agent *also* raises `ask_human` for the same decision, dedupe or double-prompt?
  Decide precedence so `strict` autonomy doesn't stack two prompts on one action.

### Headless, budget & audit (slices 4, 5)

- **Headless default & deadlock.** With no human present, `strict` + `blocking` deadlocks. Decide
  the default `timeout_policy` for non-TTY runs (§12 headless) — fall through to `default`, or
  fail-closed abort? A pause that blocks forever is worse than a wrong guess in CI.
- **Does the clock keep ticking?** While suspended for a human, do the M1 resource caps / budget /
  wall-clock timeouts keep counting? A human at lunch should not trip a "runaway session" abort.
  Likely: pause the budget/timeout clock on interrupt, resume it on reply — but that needs the cost
  middleware to be interrupt-aware.
- **Missing-price interrupt in a keyless run.** A fully keyless run has NULL cost by design;
  surfacing a missing-price prompt for *every* untracked model would be noise. Decide whether
  `system_interrupts.missing_price` auto-suppresses when the whole run is keyless, or dedupes to one
  prompt per model per session.
- **Audit trail.** Should every interrupt + its human response persist to telemetry (§8) for
  reproducibility and later replay? If so, does the human reply count as trusted input, and how is
  it fed back into agent context (as a tool result vs. a system note)?

### Human edit path (slice 3)

- **`resolve`/`edit` kind.** §9 lists `kind ∈ {approve, choose, input, resolve}`. Define whether
  `resolve` lets the human *edit* the proposed tool call / diff (not just approve/deny), how the
  edited value re-enters the graph, and whether an edited command re-runs the deterministic gates
  (it should — an edit could re-introduce a `*.env` write).
