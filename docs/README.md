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

- **`milestone5.1.md`** — **Config Field Registry**: follow-on refactor of `milestone5.md`. M5 put
  every run knob behind one precedence chain but not behind one *declaration* — adding a knob is a
  ten-site edit where nine sites fail silently, and no field knows its own valid values, which is
  what blocks the arrow-key `/config` menu M5 scoped out. Replaces the duplication with one
  `FieldSpec` table everything derives from, then adds the picker on top. Behavior-preserving by
  construction: the M5 test suite is the oracle and must pass unchanged.

## In-progress milestones — `milestones/in-progress/`

- **`milestone4.md`** — **Real Trust Boundary**: workspace visibility (`.agentignore` +
  designated-secret floor + docker mount-mask) + path-guard middleware + `permission_denied`
  interrupt wiring, backed by the §10 security suite and the §12.1 CI / §12.2 `harness doctor`
  support tier. Slices A–G need no host-userns support; **the bwrap fs-tool jail (slice H) is core v1
  scope, not a stretch layer** — it's the piece that makes the boundary an allow-list rather than a
  curated deny-list. **H is built and opt-in** (`DEEPAGENTS_JAIL=1`), shipped as a re-exec of the
  harness into a bwrap namespace; it is off by default because enabling it requires a narrow seccomp
  relaxation on the outer container, a deliberate operator trade (`milestone4.md` §16 fork 7).
  Pulls together `docs/features/workspace_visibility.md` + `design_doc.md` §2/§10/§12. Code is on
  `feat/milestone_4` (slices A–H landed; not yet merged).
- **`milestone4.1.md`** — **LSM parity (M4 slice J)**: vendors moby's `docker-default` AppArmor
  profile with **only** its `deny mount,` rule narrowed, so the bwrap jail runs on an
  AppArmor-confined host without dropping the whole LSM. seccomp and AppArmor are independent gates
  and both must allow — M4's narrow seccomp profile fixed one of them, which is why
  `DEEPAGENTS_JAIL=1` failed on stock Ubuntu/Debian Docker (the majority of Linux container hosts)
  unless the operator opted into the blunt `DEEPAGENTS_JAIL_APPARMOR=unconfined`. **Built on
  `feat/milestone_4` except its live-host measurement**: the dev machine is Docker Desktop/WSL2,
  which loads no LSM policy and structurally cannot verify this slice — the same blind spot that let
  slice H ship believing the jail was universally verified. Ships as M4 PR7. Its invariants (39–41)
  live in `milestone4_invariants.md` with the rest.
- **`milestone4_invariants.md`** — the checkable invariants the M4 boundary must satisfy
  (floor / mask / path-guard / interrupt / git-pr / state-isolation / regression / structural),
  the test-facing companion to `milestone4.md`. Folds into `milestone4.md` on completion.

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

## Feature plans — `features/`

- **`workspace_visibility.md`** — restrict which workspace paths an agent can see
  (`.agentignore` policy, designated-secret floor, docker-mask → bwrap fs-tool jail →
  optional overlayfs). **Planned**; summarized in `design_doc.md` §2.

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
