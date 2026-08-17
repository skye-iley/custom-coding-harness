# docs/

Spec and planning docs for the **holder** harness, organized by type and status.
`design_doc.md` (repo root) is the full target vision; the docs here are the concrete
slices and supporting specs. Where a milestone doc and the code disagree, the code wins
for "what is built"; where a milestone doc and `design_doc.md` disagree on *what we build
next*, the milestone doc wins.

## Layout

| Path | What lives here |
|------|-----------------|
| `milestones/planned/` | **Not-yet-built** milestones — the forward plan. |
| `milestones/in-progress/` | **Being built** milestones — doc + a separate invariants doc + code on a feature branch. |
| `milestones/complete/` | **Built + merged** milestones — shipped scope, kept as a record. |
| `features/` | Named feature plans that aren't numbered milestones. |
| `specs/` | Focused technical specs referenced by the code and design doc. |
| `archive/` | Superseded compound-engineering artifacts (brainstorms, ideation, plans) kept for history. |

## Milestone lifecycle

A milestone moves through three folders. What each stage carries is deliberate:

1. **`planned/` — docs only.** Just the milestone doc (scope, slices, done-when, implementation
   reference). No code, no invariants doc.
2. **`in-progress/` — doc + `<milestone>_invariants.md` + code.** When build starts, the doc moves
   here and gains a **separate invariants doc** — the properties that MUST hold for the milestone to
   meet its core goal, each phrased as a checkable assertion. It is kept **separate from the milestone
   doc on purpose**: invariants drive testing, and keeping them out of the planning/implementation
   prose lets a test author (or reviewer) read the boundary that must hold without wading through
   *how* it's built. Code lands on the milestone's feature branch.
3. **`complete/` — doc only, invariants folded in.** On merge, the doc moves here and the invariants
   are **folded into the milestone doc as a section** (they no longer need to stand alone for testing).
   The separate `_invariants.md` file does not follow into `complete/`.

## Planned milestones — `milestones/planned/`

*(Empty — nothing is queued. `milestone8.md` moved to `in-progress/`. Other forward candidates, not
yet written up: `design_doc.md` §13 (file-read middleware), §12.6 (deepagents-native
skills/memories), and the "Core identity — dependency chain" list; `features/` holds the two named
non-milestone plans.)*

## In-progress milestones — `milestones/in-progress/`

- **`milestone8.md`** — **Benchmark Ladder, Tier 1 (Gold Set)** (`design_doc.md` §11): run the
  harness over a pinned set of bug-fix tasks, unattended, on the free local model, and have each run
  emit a scorable `git diff` plus a telemetry row that joins to it. **Plan + invariants written, no
  code yet.** The case for it is that the three defects before it — `num_ctx` silently 2048,
  discarded `message.thinking`, `reasoning = false` — each degraded *every run* and none was caught
  by ~1000 passing tests; one was found by an operator saying "before, it could handle this fine".
  Three slices: hard stops (B1), `--emit-patch` (B2), a `harness/bench/` driver (B3), then the gold
  set (B4) and a baseline record (B5). Two traps are called out up front: **`--max-turns` is not the
  benchmark bound** (§3 — an instance is one headless turn, the runaway is the ReAct loop inside it,
  and the only thing bounding that is a `recursion_limit` the harness never sets, whose pinned
  default is **10007**, not the widely-quoted 25 — no bound at all on a free model — and whose
  `GraphRecursionError` is classified as `error`), and **`git diff` is blind to untracked files**
  (§5.2 — a fix delivered as a new file emits an empty patch that applies as a no-op and scores 0,
  with a signature identical to "the model did nothing"). Scoring is a hard non-goal (§9): the
  contract is the predictions jsonl, and correctness stays with the official evaluation harness. It
  is also the first real consumer of M6 §5b, which is half its value — telemetry nobody has consumed
  is telemetry nobody has validated (§6). §12 resolves the four design forks; **§13 records six
  assumptions checked against the code rather than inferred**, one of which (`.git` surviving the
  ephemeral copy) the B2/B3 design would have been unbuildable without.
- **`milestone8_invariants.md`** — the checkable properties, written before the code: bounds (each
  bound terminates its own runaway, a stop records `stopped` + `stop_reason` and never `error`, an
  unset bound is *absent* rather than infinite), patch fidelity (an untracked new file appears; the
  patch is asserted by **applying** it to a fresh base, never by substring — the M7 §0.2 lesson),
  sweep integrity (resume, one failure never aborts, empty patches counted loudly), joinability (the
  key is `run_id` and never `thread_id`; `null` cost stays null), containment/non-interference (no
  branch, no PR, keyless import profile), and removability. Folds into `milestone8.md` on
  completion.

## Complete milestones — `milestones/complete/`

- **`mvp.md`** — the shipped baseline: a one-command, disposable Docker container running a
  Deep Agents coding agent against a mounted workspace, provider-agnostic, secret-safe.
- **`milestone1.md`** — cost/token visibility + resource caps (`harness/cost.py`,
  per-session budgets, `run-docker` `--cpus`/`--memory`/`--pids-limit`).
- **`milestone2.md`** — present/past memory: fresh-by-default thread + a separate, on-demand
  archive that accumulates across sessions (`harness/archive.py`, `harness/memadmin.py`).
- **`milestone3.md`** — human-in-the-loop: one `interrupt()` spine, three trigger sources
  (deterministic pause middleware, agent `ask_human` tool, system events) + the two `design_doc.md`
  §12 prereqs it rides on (P1 resilience, P2 headless). **Built** — §0 records what shipped vs.
  deferred (`missing_price`/`permission_denied` events, `shadow` policy, clock-pause, S6 PR-b).
- **`milestone4.md`** — **Real Trust Boundary**: workspace visibility (`.agentignore` +
  designated-secret floor + docker mount-mask) + path-guard middleware + `permission_denied`
  interrupt wiring, backed by the §10 security suite and the §12.1 CI / §12.2 `harness doctor`
  support tier. **Slices A–J all landed.** The bwrap fs-tool jail (slice H) was core v1 scope, not
  a stretch layer — it is what makes the boundary an allow-list rather than a curated deny-list —
  and ships as a **re-exec of the harness into a bwrap namespace**, so "every fs tool routes through
  the jail" is structural rather than an assertion. It stays **opt-in** (`DEEPAGENTS_JAIL=1`)
  because enabling it needs a narrow seccomp relaxation on the outer container, an operator's trade
  (§16 fork 7). Deferred v2 (overlayfs view, slice I) and invariant 16 stay out of scope. The 45
  checkable invariants are folded in as **§19**. Pulls together
  `docs/features/workspace_visibility.md` + `design_doc.md` §2/§10/§12.
- **`milestone4.1.md`** — **LSM parity + the third gate (M4 slice J)**: vendors moby's
  `docker-default` AppArmor profile with **only** its `deny mount,` rule narrowed, so the bwrap jail
  runs on an AppArmor-confined host without dropping the whole LSM. **Complete and measured on a
  live host** (2026-08-14, Ubuntu VM) — and the measurement is the point: the derived rule set had
  four rules wrong and one missing, and a second round deleted a seventh as inert, so the shipped
  set is *measured, narrowed by subtraction*, never read off bwrap's source. It also surfaced a
  **third gate nobody had accounted for** — the kernel's `mount_too_revealing()` refusing bwrap's
  fresh `--proc` while Docker's `maskedPaths` cover the container's procfs, independent of both
  profiles — closed by `--security-opt systempaths=unconfined` from the launchers. CI now pins
  `JAIL_CHECK=1` on an AppArmor-confined runner. Its invariants (37–45) live in `milestone4.md` §19.
- **`milestone4_manual_verification.md`** — the manual/live verification record behind M4's
  boundary claims: what was run, on which host, and what it printed. Kept as evidence, not guidance.
- **`milestone5.md`** (+ **`milestone5_spec.md`**, full implementation spec) — **Unified Config
  Surface**: CLI flags + an in-session `/config` command for live knobs (model, budgets, HITL
  preset) and a pre-spinup wizard (`harness config` / `harness config security`) for knobs fixed
  at container start (mask mode, jail/AppArmor, resource caps, NetJail), all resolved through one
  `harness/config.py` precedence chain (CLI flag > env > profile file > default) instead of
  scattered env-var/`.env` edits. **Built** — §0/§4 record three deliberate scope-downs from the
  original plan (no `-Autonomy` host flag, no arrow-key `/config` menu, no NetJail list editor in
  `harness config security`) and one real bug the build surfaced and fixed
  (`PauseMiddleware` was caching `autonomy_level`/`on_deny` at construction instead of reading them
  live, so a `/config set hitl.*` edit would have had no effect). §8 holds the folded invariants.
- **`milestone5.1.md`** — **Config Field Registry**: follow-on refactor of `milestone5.md`. M5 put
  every run knob behind one precedence chain but not behind one *declaration* — adding a knob was a
  ten-site edit where nine sites failed silently, and no field knew its own valid values, which is
  what blocked the arrow-key `/config` menu M5 scoped out. One `FieldSpec` table is now the single
  declaration; `Settings`, profile I/O, the resolver, both display renderers, `/config set`
  dispatch, the wizard screens, and the new picker all derive from it. **Built — R1–R7 all landed**
  on `feat/milestone5.1-config-field-registry`, with the M5 suite passing **unedited** (§0.2), plus
  the one sanctioned behavior change: every enum knob now rejects an invalid value at the point of
  entry, closing the M5 §0.2 gap where `mask_mode: alow` persisted and silently resolved to `deny`.
  §9 holds the folded invariants: derivation (nothing that should derive is hand-written), behavior
  preservation, the enum-validation change, and the picker's fallbacks.
- **`milestone6.md`** — **Telemetry** (`design_doc.md` §8 + §12.7): a durable per-turn sink
  (`<state-dir>/usage.jsonl`), a derived session summary, that summary appended to the PR body
  `git-pr` opens, and keyless read access. Not a from-scratch build — M1 already computes per-turn
  tokens/cost/energy and throws them away, M2 persists only a session roll-up, and `audit.py`
  already has the jsonl-append + recursive-scrub substrate; this is the missing **sink** and
  **surface**. Picked as the next milestone because it is one of the two core-identity items with
  no dependency on the trust-boundary chain, so it cannot be blocked on the open AppArmor
  measurement. **Telemetry is treated as an audit surface** — the sink lives in the state dir with
  `past.sqlite` / `denials.jsonl`, not in the agent-writable workspace, because the subject of the
  audit must not be able to edit the record (§5a). **Built — T1–T6 all landed** on
  `feat/milestone6-telemetry-impl`. §0.1 records what the build changed about the plan; the notable
  one is that §3.1's probe **reversed the fix the spec had planned** (telemetry is the outer
  `wrap_tool_call`, but the gate *suspends* the graph rather than blocking, so the human wait was
  never inside the wrapper and the planned subtraction would have removed time nobody counted).
  Its **primary purpose is benchmark-grade attribution** (§5b): wall clock decomposes into
  `model_ms` / `tool_ms` / `retry_sleep_ms` / `paced_sleep_ms` + a bounded residual, each measured
  at its own seam, with per-tool-name counts and `run_id` in the headless JSON so a sweep (e.g.
  SWE-bench Lite, one instance per `--headless` run, `--topic <instance_id>` as the join key) can
  aggregate. It does **not** score a benchmark — correctness stays with the benchmark's own
  evaluation of the produced diff. §8 holds the folded invariants, written before the code: capture
  (one record per turn, failed turns included, never breaks a turn, **and the wall-clock
  decomposition** — invariant 4a, the one that catches a future blocking call vanishing into
  "overhead"), derivation (the summary is derived and must agree with the `past.sqlite` row, which
  stays authoritative), containment (no prompt/reply/tool-arg text by construction; aggregates only
  reach a PR; git-pr degrades to today's body on any telemetry failure), removability, and
  joinability.
- **`milestone6_spec.md`** — the implementation-level spec (same relationship `milestone5_spec.md`
  has to `milestone5.md`): exact `usage.jsonl` / `session.json` schemas, which hook captures which
  field, the `scrub.py` extraction, the `FieldSpec` entry for the on/off knob, the PR-block format,
  the `harness telemetry` argv grammar, failure paths, test plan, and build order. **Written to be
  sufficient to build from cold** — including the one composition fact the repo had never verified
  (whether telemetry's `wrap_tool_call` nests outside `PauseMiddleware`'s), which §3.1 made a probe
  step rather than a guess. **§3.1 now records the answer and both findings**: telemetry is outer,
  and the subtraction that fact was supposed to justify is wrong for an unrelated reason, so the
  probe's real payoff was catching that the gate's control flow passes *through* telemetry's
  wrapper.

- **`milestone7.md`** — **Raw Trace Debug Mode** (`design_doc.md` §11): `DEEPAGENTS_RAW_TRACE=file`
  writes, per model call, the literal payload the harness hands the model — final system prompt,
  full message history, tool schemas, tool-call/tool-result blocks — so a weak-model failure
  (hallucinated tool JSON, ignored instructions, a tool the model never saw) is diagnosable from the
  harness's own output instead of by switching on the model server's debug logging
  (`OLLAMA_DEBUG=1`, the workaround this removes). **Built — S1–S5 all landed** on
  `feat/raw-trace-debug`, merged in PR #52; §0 is the build status and §0.2 what the build changed
  about the plan. Complements M6 rather than overlapping it: telemetry says the tool-error
  rate spiked at turn 7 and deliberately carries no text; this says what the model was looking at.
  Two decisions worth knowing before reading: the capture point is the **innermost**
  `wrap_model_call`, *after* `_ExcludeToolsMiddleware`, because a trace taken one layer out logs
  tools the model never received — the exact bug class it exists to diagnose (§5); and the
  design-doc's "raw tags included, e.g. Ollama's chat-template markers" is **not deliverable** —
  Ollama renders the template server-side, so §3 defines three fidelity levels, ships the
  message-level one, and required `design_doc.md` §11 to be corrected rather than left aspirational.
  The knob is a four-valued enum (`off`/`file`/`console`/`both`), **live and `/config`-settable**,
  which gets the M5.1 picker and enum validation for free; `console` **replaces** the rendered
  answer, which is the point — `final_message_text` (`agent.py:449–465`) deliberately drops
  reasoning/thinking blocks and unknown part shapes, and that transform is what this makes
  skippable (§7). §8 covers reasoning traces, including encrypted ones (recorded in position as a
  typed placeholder with its byte size, never as ciphertext); §9 covers streaming, which v1 does not
  implement but must not foreclose — hence a three-phase append-incremental writer and a note on
  `AgentMiddleware.transformers` as the future seam. §0.1 records what the first draft got wrong.
  **§16 holds the folded invariants**, written before the code: fidelity (bodies verbatim, additions
  structural only, one record per model call, labels correct across retries and HITL resumes,
  **nothing on the response dropped** — unknown block types dumped rather than skipped), position
  (the middleware is last in the stack, asserted not commented), destination (mode-exact output;
  console changes the display and never `run_turn`'s return value or the headless JSON), containment
  (state-dir sink, scrub on every section including the console path, tamper-resistance stated at
  its real strength), non-interference/removability (a sink failure never breaks a turn; `off` is a
  true pass-through), and streaming extendability.

## Feature plans — `features/`

- **`workspace_visibility.md`** — restrict which workspace paths an agent can see
  (`.agentignore` policy, designated-secret floor, docker-mask → bwrap fs-tool jail →
  optional overlayfs). **Planned**; summarized in `design_doc.md` §2.
- **`selinux_compatibility.md`** — **Planned**, and deliberately **separate from Milestone 4.1**
  rather than an open thread inside it: M4.1 is complete without it. A pre-release compatibility
  check — run `DEEPAGENTS_JAIL=1` on a RHEL/Fedora host and record what happens. SELinux is **not
  confirmed to work and not confirmed to fail**; M4.1 fork J4 closed only the *reporting* gap (the
  harness names the unknown instead of misreading an SELinux context as an AppArmor profile).
  Carries the measurement protocol (`ausearch -m AVC`, not `dmesg`), what each of four outcomes
  obliges, and the standing rule: no claim of SELinux support until a measurement exists. Rootless
  Docker/Podman is the same shape of gap.

## Specs — `specs/`

- **`energy.md`** — energy-tracking spec. The per-token estimate ships (`cost.py`); the
  measured local-device path is specified, not built.

## Archive — `archive/`

Superseded compound-engineering documents, kept for provenance. These describe the
**shared test infrastructure on a multi-stage build** work, which has since been
**implemented** (`deepagent-image/project/tests/` with `conftest.py`, `_bootstrap.py`,
fixtures, and the `runtime`/`test` Dockerfile split). Not live guidance — read the code.

- `2026-06-25-test-suite-revamp-ideation.html` — ideation (ce-ideate).
- `2026-06-25-shared-test-infra-requirements.md` — requirements brainstorm (ce-brainstorm).
- `2026-06-25-shared-test-infra-multistage-plan.md` — implementation plan (ce-plan).

## Glossary

**Removable contract:** A feature whose code can be deleted and the harness 
reverts to prior-milestone behavior *byte-for-byte*, with no residual coupling. 
Examples:
  - `DEEPAGENTS_MASK=0` disables M4 masking, leaving M3 unchanged.
  - `DEEPAGENTS_ARCHIVE=0` disables M2 past archive, falling back to M1.
  - Deleting `archive.py` + `memadmin.py` and rewiring defaults reverts to M1.

Removable contracts ensure features can be disabled or removed without leaving 
dead code or partial state behind. Each milestone doc lists its removable contract 
as part of "Def-of-Done."
