# Milestone 6 — Telemetry

## 0. Build status

**Planned — nothing built yet.** Branch `feat/milestone6-telemetry` exists and carries these two
docs only. Checkable properties: `milestone6_invariants.md` (same folder), also unimplemented.

This doc is the plan, written before the code so the code has something to be wrong against. Every
claim below about *existing* behaviour was read off the tree at `6e0c104`; every claim about new
behaviour is a proposal until a slice lands and this §0 says so.

Chosen over the `HarnessProfile` chain deliberately: telemetry is one of the two core-identity items
with **no dependency on the trust-boundary chain** (`design_doc.md` → Product Identity → "Core
identity — independent of the chain above"), so it cannot be blocked by the one open question there
— whether the bwrap jail actually starts on an AppArmor host, which is being measured separately.

## 1. Goal & Definition of Done

`design_doc.md` §8 (Observability & Metrics) + §12.7 (Cost / telemetry persistence). The harness
**already computes** almost everything §8 asks for and then throws most of it away: M1's tracker
prints a per-turn usage line to stderr and forgets it, and M2 persists only a session-level roll-up
on one `past.sqlite` row. §8 is not a from-scratch build. It is a **sink** and a **surface**.

Done when:

1. Every turn appends a structured record to a durable per-run sink, secret-scrubbed.
2. Every run writes one session summary artifact derived from those records.
3. That summary reaches the **PR body** the `git-pr` workflow opens, when a PR is opened.
4. `harness past`-style read access exists for the summary without opening a JSON file by hand.
5. **Removable contract holds:** with telemetry off, the harness is byte-for-byte Milestone 5.1.
6. Nothing in the sink can leak a credential, a prompt, or a file's contents.

Explicitly *not* done-when: the §8 metrics that have no data source yet (see §5 fork 3).

## 2. What exists today — the honest inventory

| Source | Has | Gap |
|---|---|---|
| `cost.py` `UsageAccumulator` | per-turn `input`/`output`/`cache_read`/`cache_write`/`cost`/`energy_wh`/`unpriced_calls`/`estimated_calls` | printed to stderr, then **discarded**. No per-turn record survives the process |
| `cost.py` `CostTrackerMiddleware` | the per-call `after_model` seam where usage is already read | built **only when there is something to track** (M1's null=MVP contract) — so telemetry cannot assume it exists |
| `archive.py` `sessions` row | `input_tokens`, `output_tokens`, `cost_usd`, `cost_provenance`, per run | session-level only. No per-turn, no energy, no latency, no model mix, no tool-call counts |
| `audit.py` | jsonl append + **recursive** secret scrub + the two-sink (workspace / state-dir) split + `.agent_telemetry/` (git-ignored, git-pr-excluded) | it is an approval/boundary record, not telemetry. But it is the substrate to reuse rather than reinvent |
| `workflows/git-pr/open-pr.sh` | `gh pr create --body "<hardcoded string>"` | the PR surface seam. Nothing reads a summary today |
| `cli.py` session end | `_finalize_session(...)` then `_pr_approval(...)` then `run_hook(session.end)` in `finally` | the exact ordering slot a summary must be written into — after totals are final, before git-pr runs |

**Read this table as the reason the milestone is small.** Four of six rows are "the data is here";
the work is plumbing, placement, and not leaking anything.

## 3. Slices (in build order)

### T1 — `harness/telemetry.py`: the per-turn sink
New stdlib-only module. `record_turn(path, record)` appends one scrubbed JSON object per line to
`<sink>/usage.jsonl`. Reuses `audit.py`'s scrub — **extracted to a shared helper, not copy-pasted**
(the repo has been bitten by a byte-for-byte copy before: `tests/_bootstrap.py` exists because two
test modules had duplicated a loader).

Fields v1: `run_id`, `thread_id`, `turn`, `ts`, `provider`, `model`, token counts, `cost_usd`,
`cost_provenance`, `energy_wh`, `unpriced_calls`, `estimated_calls`, `duration_ms`, `tool_calls`.
No prompt text, no reply text, no tool arguments — see §4.

**Import layering:** `telemetry.py` must import no sibling but `harness.audit` (for the scrub).
`cost.py` must **not** import it — the acyclic guard `test_import_isolation` enforces stays intact;
`cli.py` feeds telemetry, exactly as it feeds `archive.py` today.

### T2 — wire the turn seam
`cli.run_turn` already knows the turn boundary and holds the tracker. Emit one record per completed
turn, and one on a **failed** turn too (a turn that burned tokens and then threw is precisely the
one an operator wants to see). Writes must never raise into the turn path — same rule the audit
sink and `/config` dispatch already follow.

### T3 — session summary artifact
At session end, derive `<sink>/session.json` from the turn records: totals, model mix, turn count,
failed-turn count, wall-clock duration, interrupt count. Written in `cli.main`'s end sequence
**after** `_finalize_session` (so the numbers match both stderr and the `past.sqlite` row) and
**before** `_pr_approval` / `run_hook("session.end")` (so git-pr can read it).

`past.sqlite` stays **authoritative** for session totals; `session.json` is derived. Two files
disagreeing is worse than one file missing, so the invariants pin the derivation, not the values.

### T4 — telemetry-to-PR
`open-pr.sh` gains: if the summary file exists, append a fenced summary block to the PR body
(`--body-file`, so a large body isn't shell-quoting-fragile). Absent file, unreadable file, or `gh`
missing ⇒ the current body, unchanged. Only aggregate numbers cross this boundary — never a turn
record, never a prompt. This is the one slice that publishes anything outward, so it is also the one
that gets the tightest test.

### T5 — read access
`harness telemetry show [--run <id>]` via the existing keyless `dispatch` route (stdlib only, like
`memadmin`), printing the summary the same way `harness past show` prints a session.

### T6 — removability + tests
`DEEPAGENTS_TELEMETRY=0` ⇒ no sink, no session file, no PR block, no middleware. Host-tier tests for
the record shape, the scrub, the derivation, and the git-pr no-ops; live-model case for the one
thing a stub cannot check — that a real turn against a real model produces a record whose token
counts are non-zero and whose `duration_ms` is plausible (the CLAUDE.md testing guideline: assert
against a real model where the behaviour is the model's, and `usage_metadata` is exactly the field
providers have silently omitted before).

## 4. Non-goals

- **No prompt/response trace.** `.agent-trace.jsonl` in §8 reads as "model inputs and responses";
  that is `DEEPAGENTS_RAW_TRACE` (`design_doc.md` §11), a separate feature with a different risk
  profile. This milestone records *measurements*, not *content* — which is also what makes the
  scrub a backstop rather than the primary defence.
- **No remote export, no OpenTelemetry, no dashboard.** Files on disk + a PR block.
- **No new cost math.** M1 owns pricing/energy; telemetry only records what it already computes.
- **No automatic retention/GC.** Same posture as M2: manual prune, policy deferred.

## 5. Forks (decide before T1)

1. **Which sink dir — workspace `.agent_telemetry/` or the state dir?**
   → **Workspace**, matching `interrupts.jsonl`. Telemetry is UX/metrics, not boundary evidence, and
   git-pr already excludes that directory. **Recorded caveat:** the agent's own tools can therefore
   rewrite it. That is acceptable *only* because `past.sqlite` (state dir, outside the mount) stays
   the authoritative ledger — if telemetry ever becomes an audit surface, it moves, the way M4 slice
   D moved denials.
2. **Extend `past.sqlite` or write files?**
   → **Both, with one authority.** Per-turn stream in jsonl (append-only, cheap, greppable);
   session totals stay the sqlite row. `session.json` is derived from the stream and must equal the
   row on the fields they share.
3. **Which §8 metrics ship in v1?**
   → Only those with a data source **today**: tokens, cost, energy, per-turn latency, model mix,
   tool-call count, turn/failure counts, interrupt count. **Deferred by dependency, not by choice:**
   routing accuracy (no router — core-identity item 5), token-reduction ratio (no compression — §7),
   session success rate / PR quality (needs GitHub state after the fact), TTFT (needs streaming,
   which the harness does not use). Each deferral is named in the doc so a future reader does not
   read the omission as an oversight.
4. **Naming — `design_doc.md` §8 says `.agent-trace.jsonl` / `.agent-metrics.json`.**
   → **Deviate**: `.agent_telemetry/usage.jsonl` + `.agent_telemetry/session.json`. The dir already
   exists, is already git-ignored and git-pr-excluded, and the repo's convention there is snake_case.
   Recorded here so the doc-vs-code difference is a decision, not drift; `design_doc.md` §8 gets
   updated when this lands.
5. **Where does `duration_ms` come from?** → wall clock around the turn in `cli.run_turn`, not the
   provider's own timing. It includes tool execution and HITL wait time, so the field means
   "how long the turn took", not "how long the model took" — and the invariant says so, because a
   latency number whose definition is ambiguous is worse than none.

## 6. Risks

- **Double-counting.** Two writers of the same numbers is how ledgers drift. Mitigation: fork 2's
  single-authority rule + an invariant that pins `session.json` to the sqlite row.
- **Leakage into a PR.** T4 publishes outward. Mitigation: aggregate-only by construction (the PR
  block is built from `session.json`, which has no free-text field), plus the scrub, plus a test
  that a turn record containing a planted secret never reaches the body.
- **Per-turn write cost.** One append per turn is negligible next to a model call; but the write
  must not be in the model's critical path in a way that can raise. Mitigation: same
  never-raise wrapper the audit sink uses.
- **Removable contract erosion.** Every milestone since M1 has kept "off ⇒ prior milestone,
  byte-for-byte". Telemetry touching `run_turn` is the riskiest such seam yet, because it is
  per-turn rather than per-session. Mitigation: the middleware/sink is appended only when enabled,
  and the invariants include a byte-for-byte-off check.

## 7. Open questions for the operator

- Should telemetry default **on** or **off**? M1's tracker defaults on-when-useful; the audit trail
  defaults off (HITL-gated). Recommendation: **on**, since it writes to a git-ignored dir and costs
  one append per turn — but it is the operator's call, and the answer changes T6's default.
- Does the PR block belong in the body, or in a comment? Body is simpler and survives; a comment is
  easier to update later. Recommendation: body for v1.
