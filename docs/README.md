# docs/

Spec and planning docs for the **holder** harness, organized by type and status.
`design_doc.md` (repo root) is the full target vision; the docs here are the concrete
slices and supporting specs. Where a milestone doc and the code disagree, the code wins
for "what is built"; where a milestone doc and `design_doc.md` disagree on *what we build
next*, the milestone doc wins.

## Layout

| Path | What lives here |
|------|-----------------|
| `milestones/complete/` | **Built** milestones — shipped scope, kept as a record. |
| `milestones/planned/` | **Not-yet-built** milestones — the forward plan. |
| `features/` | Named feature plans that aren't numbered milestones. |
| `specs/` | Focused technical specs referenced by the code and design doc. |
| `archive/` | Superseded compound-engineering artifacts (brainstorms, ideation, plans) kept for history. |

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

## Planned milestones — `milestones/planned/`

*Empty.* Milestone 3 was the last planned milestone; next work is drawn from `design_doc.md` §12
leftovers or a new named feature plan.

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
