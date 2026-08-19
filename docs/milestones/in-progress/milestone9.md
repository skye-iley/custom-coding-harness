# Milestone 9 — HarnessProfile (per-agent scoped bind/tool config)

## 0. Status

**Built — all seven §3 done-when items land on `feat/milestone9-agentprofile`.** Checkable
properties: `milestone9_invariants.md` (same folder) until the milestone moves to `complete/`, at
which point it folds in here as a section, per `docs/README.md`'s milestone lifecycle. **One gap
found in post-build review, not yet fixed — see §0.2, next session's first task.**

### 0.1 What the build confirmed rather than changed

No design fork in §5 needed revisiting — the build matched the plan exactly:

- `harness/profile.py` (new, stdlib-only, no harness-sibling imports) defines `BindEntry`
  (workspace-relative-only, per-entry `rw`/`ro`, raises at construction on an absolute path or a
  `..`-escape) and `AgentProfile` (`name`, `binds`, reserved `harness_profile_key`/`network`).
  `DEFAULT_PROFILE` is a single `BindEntry(relpath=".", mode="rw")`, and `Path(workspace) / "."`
  normalizes to `Path(workspace)` exactly (verified), which is what makes invariant 2's "identical
  two-element pair, not an equivalent one" hold without special-casing `"."`.
- `jail.bwrap_args` gained `profile: AgentProfile | None = None` (keyword-only, after `*`) and now
  imports `harness.profile` — the one deliberate exception to "imports no harness sibling" the
  module's own docstring claims, noted inline as safe because `profile.py` itself imports nothing
  and so carries no cycle risk. The single hardcoded `--bind` line became a loop over
  `(profile or DEFAULT_PROFILE).binds`, in the same position between the `project_root` bind and
  the state-dir bind. `jail.maybe_reexec`'s one internal call site passes no `profile`, so it and
  the pre-M9 test suite (51 tests) are byte-identical with zero edits.
- `scripts/sandbox-exec.sh` reads `AGENT_BIND_SCOPE` (`relpath:mode,relpath:mode`); unset or
  empty short-circuits before the loop body ever runs (invariant 3), and a malformed entry
  (missing `:mode`, empty relpath, unknown mode) exits 2 before `bwrap` is ever reached — verified
  via a stub `bwrap` on `PATH` that logs argv instead of exec'ing (`tests/test_sandbox_exec.py`,
  new, needs a real `bash` — the script uses arrays and `${var@Q}`, not POSIX `sh`).
- The ordering invariant (masked overmounts strictly after every profile bind, regardless of how
  many bind lines the profile contributes) and the "only the substituted segment differs" argv-diff
  invariant are both asserted directly in `tests/test_jail.py`'s new M9 section, argv-equality
  style — never a substring check, per the M7/M8 lesson.
- No `run-docker.{ps1,sh}`, `config.py`, or `.harness-profile.yaml` touched, and `check-parity`
  stays green untouched — there is still no selection surface (§4, §6), exactly as scoped.

Full test run: `pytest tests/` — 1283 passed, 14 skipped (pre-existing Windows/live-model skips
only), including the new `test_profile.py` (9), the M9 additions to `test_jail.py` (8), and
`test_sandbox_exec.py` (6).

### 0.2 Known gap — `sandbox-exec.sh`'s `AGENT_BIND_SCOPE` skips the §10 escape check (found in
post-build review, next session's first task)

`harness/profile.py`'s `BindEntry` rejects an absolute or `..`-escaping `relpath` **at
construction** (§5 Fork A, invariant 4) — the direct mitigation for `design_doc.md` §10's "Sandbox
Escape (Dynamic Binds)" risk. That check exists **only** on the Python side. `sandbox-exec.sh`'s
`AGENT_BIND_SCOPE` parser (§2/§6) does no equivalent validation — `target="$WS/$relpath"` is plain
string concatenation, so `AGENT_BIND_SCOPE="../../etc:rw"` produces the literal bwrap argument
`$WS/../../etc`. **Verified live**: bwrap resolves it and binds host `/etc` **read-write** into the
nested shell jail — the exact escape §10 exists to name.

Currently inert: no shipped launcher sets `AGENT_BIND_SCOPE` yet (§4). But §2 itself calls an env
var "the existing pattern" for this seam — whatever wires chain item 4 (config-driven selection)
is expected to set it directly, and would land the escape live unless this closes first. Not a
missed §3 done-when item (Fork A/invariant 4 only ever scoped the Python object), but a real gap
in the seam this milestone shipped, worth closing before anything sets the var for real.

**Fix, scoped small — port `_validate_relpath`'s three checks into the shell:**
- `deepagent-image/scripts/sandbox-exec.sh`, in the per-entry loop (currently lines ~27–33, right
  after the existing empty/malformed check and before the `case "$mode"` block), add:
  - reject `relpath` starting with `/`
  - reject a Windows-style drive prefix (`?:`) — parity with the Python check, low-value on Linux
    alone but keeps the two validators textually mirrored
  - reject any `/`-separated segment equal to `..` (e.g. `case "/$relpath/" in *"/../"*) ...`)
  - same failure shape as the existing malformed-entry branch: message to stderr, `exit 2`, no
    `bwrap` invocation
- `tests/test_sandbox_exec.py`: two new hard-failure cases mirroring the existing three
  malformed-entry tests (same `bwrap_stub` fixture, assert non-zero exit + empty argv log) —
  `AGENT_BIND_SCOPE="../../etc:rw"` and `AGENT_BIND_SCOPE="/etc:ro"` — mirroring
  `test_bind_entry_rejects_dotdot_escape` / `test_bind_entry_rejects_absolute_paths` in
  `tests/test_profile.py`.
- `milestone9_invariants.md`: new invariant between 10 and 11 ("Removability"), stated the way
  invariant 4 is stated for the Python side — `sandbox-exec.sh`'s `AGENT_BIND_SCOPE` parser
  rejects an absolute or `..`-escaping relpath before `bwrap` is ever reached, same as `BindEntry`
  does at construction.

Source: `design_doc.md` §2 ("HarnessProfile dynamic bind mounts", "Specialized Profiles") + §4
("Specialized Profiles": Architect vs. Coder toolsets) + the "Core identity — dependency chain"
item 2 (`design_doc.md` line ~112). Second link in the chain after **M4 slice H** (bwrap fs-tool
jail, built, opt-in `DEEPAGENTS_JAIL=1`) — this milestone is what M4 slice H's own doc calls out as
depending on it: *"profile bind-scoping is only real once bwrap enforces it; today there's a fixed
bind list and no profile object at all."*

## 1. Naming collision — read this before writing code

`design_doc.md` uses "`HarnessProfile`" for two different things, and they must not be conflated:

1. **`deepagents.profiles.HarnessProfile`** — a real class in the installed `deepagents` package
   (flagged **beta** by its own docstring), reached via `register_harness_profile(key, ...)`. It
   controls **prompt assembly and tool/middleware exclusion** — `base_system_prompt` (replaces the
   SDK's `BASE_AGENT_PROMPT`), `system_prompt_suffix`, `excluded_tools`, `excluded_middleware`. This
   already exists, needs no building, and has four measured footguns recorded in `design_doc.md`
   §5 "Per-agent prompts & profiles — measured constraints" (2026-08-17): registration **merges**
   rather than replaces (an omitted field silently inherits a prior registration's value), the
   module-level registry dict has no lock (register→build is a critical section), a spec string with
   two colons resolves to **nothing** silently, and profiles bake at `create_deep_agent` build time
   (a later registration never reaches an already-built agent).
2. **`design_doc.md` §2's "HarnessProfile dynamic bind mounts"** — a **bwrap bind-mount scoping**
   concept (`--bind /workspace/src/components` vs. the full workspace) plus, per §2's own code
   sketch, a `network: NetworkPolicy` field. **Nothing in the `deepagents` package knows what bwrap
   is** — this half has no library implementation to reach for. It does not exist in this repo
   either: `grep -r HarnessProfile deepagent-image/` outside `design_doc.md` returns nothing.

**This milestone builds (2) as a harness-owned object, named `AgentProfile`
(`harness/profile.py`) — not reusing the `HarnessProfile` name** — that *optionally holds a
reference to* a (1)-style registration for the agent it describes, rather than subclassing or
wrapping the beta library class. Two independent reasons, not one:

- §5's "Design rule for the funnel" (already written, unbuilt) says to split the two axes — *role*
  (per-agent, via the subagent's own `system_prompt`) and *model family* (shared, one
  `HarnessProfile` registered once before any build). Bind scope is a **third, orthogonal** axis
  (per-agent like role, but a filesystem property, not a prompt property) — folding it into the
  beta class's already-merging, already-lockless registry only stacks a new footgun onto three
  measured ones.
- The beta class's fields are 1:1 with `create_deep_agent`'s own prompt-assembly seams. Bind scope
  is consumed by `jail.bwrap_args` and `sandbox-exec.sh` — a completely different call path that
  runs *before* `create_deep_agent` is ever reached (the bwrap re-exec happens at process startup,
  `jail.maybe_reexec`, `harness/jail.py:582`). There is no shared consumer to justify one object.

**Follow-up filed, not done here:** `design_doc.md` §2/§4's "HarnessProfile" wording should be
corrected to name `AgentProfile` once this ships, the same kind of correction M7 made to §11's
"raw tags included" claim. Not done in this doc because the object doesn't exist yet to point at.

## 2. The seam — exact, not aspirational

Two hardcoded full-workspace binds are what this milestone parameterizes. Both today bind the
**entire** workspace read-write, unconditionally, regardless of which agent (today, the only agent)
is running:

- **`harness/jail.py:410`** — `bwrap_args()`, the harness's own re-exec namespace (M4 slice H):
  ```python
  args += ["--bind", str(workspace), str(workspace)]
  ```
  Immediately preceded by the read-only system/`project_root` binds (§364–408) and followed by the
  state-dir bind and masked overmounts (§410–437, "later mount wins" ordering — masked entries must
  stay last). A scoped profile inserts **between** project_root and masked: replace the one line
  with a loop over the profile's bind list, each entry becoming `--bind` (read-write) or `--ro-bind`
  (read-only) instead of the single unconditional full bind.
- **`scripts/sandbox-exec.sh:50`** — the shell tool's **nested** jail (invoked per shell call, not a
  re-exec, so it cannot read a Python object directly):
  ```bash
  --bind "$WS" "$WS" \
  ```
  Needs the scoped bind list passed in some form the shell script can consume without a runtime
  dependency — an env var is the existing pattern (`AGENT_WORKSPACE` already works this way). See
  §6 for the proposed `AGENT_BIND_SCOPE` env carrying a `src:mode` list, generated by whichever
  Python code launches the shell tool and read by a small loop replacing the one hardcoded line.

Both binds are **inert unless `DEEPAGENTS_JAIL=1`** (M4 slice H is the enforcer this depends on,
per the dependency-chain note in §0). With the jail off, the docker mount-mask is still the whole
boundary and scoping a profile's binds changes nothing — this milestone must not claim otherwise.

## 3. Goal & Definition of Done

**Goal.** Turn "which paths an agent can bind-mount" from a hardcoded bash/Python line into data —
one `AgentProfile` per agent, defaulting to today's exact full-workspace behavior — so that when the
funnel (chain item 6) eventually builds multiple agents, giving each one a different bind scope is a
new `AgentProfile` value, not a new code path.

**Explicitly not this milestone's job:** making the harness run more than one agent. There is
exactly one agent today (no funnel, chain item 5/6 unbuilt), so "per-agent" scoping has nothing to
differentiate yet. This milestone's job is narrower: build the object, wire it through both bind
seams with **zero behavior change** at the default, and prove the mechanism with a second,
deliberately-scoped profile exercised only by tests / a manual CLI override — not a second live
agent, which doesn't exist to receive it.

**Done when:**

1. `harness/profile.py` defines `AgentProfile` (stdlib dataclass, no langchain/deepagents import —
   it must be constructible by the same pre-runtime-stack code that builds `bwrap_args`, and stay in
   the host test tier per the `harness`/`harness.entry` keyless-import convention `test_import_isolation`
   already polices). Fields: `name: str`, `binds: list[BindEntry]` (`BindEntry` = relpath under the
   workspace + `"rw"`/`"ro"`), `harness_profile_key: str | None` (the optional pointer to a §1-(1)
   registration, unresolved and unused by this milestone — reserved so item 6's funnel doesn't need
   a second field-registry change to add it), `network: None` (reserved, unused — the literal
   placeholder for chain item 3's `NetworkPolicy`, so *that* milestone adds a type, not a field).
2. `DEFAULT_PROFILE` = `AgentProfile(name="default", binds=[BindEntry(relpath=".", mode="rw")],
   harness_profile_key=None, network=None)` — resolves to exactly the single full-workspace `--bind`
   line both seams emit today.
3. `jail.bwrap_args` gains a `profile: AgentProfile | None = None` parameter (default `None` →
   `DEFAULT_PROFILE`, so every existing caller and every existing test is unaffected without editing
   them — the M7 §0.2 lesson about additive parameters applies here too). The single hardcoded
   `--bind` line (§2) is replaced by a loop over `profile.binds`.
4. `scripts/sandbox-exec.sh` reads an optional `AGENT_BIND_SCOPE` env var (`relpath:mode,relpath:mode`,
   unset → today's single full-workspace bind, unchanged) and emits one `--bind`/`--ro-bind` pair per
   entry instead of the hardcoded line. `run-docker.{sh,ps1}` do **not** set it yet (no profile
   selection surface exists to feed it from — see §6) — the knob exists in the script and is inert
   until something populates it, the same "declared without being built" shape M8's `Runner`
   protocol used for tier 2.
5. A **second**, deliberately narrower profile (e.g. `AgentProfile(name="readonly-docs",
   binds=[BindEntry(".", "ro")])`) is exercised by `tests/test_profile.py` / the `jail.py` bind-list
   tests — asserting the generated bwrap argv actually differs (a `--ro-bind` instead of `--bind` for
   the workspace root) — proving the mechanism before anything needs it live.
6. `bwrap_args`'s existing docstring ordering guarantee (system binds → project_root → workspace →
   state dir → masked-last) is preserved for the scoped case too: masked overmounts must still land
   after every profile bind, or a scoped-but-not-masked profile could re-expose a floor path the
   default profile's ordering was hiding.
7. Removable contract: delete `harness/profile.py`, drop the `profile=` parameter from
   `bwrap_args` (revert to the hardcoded line), revert `sandbox-exec.sh`'s loop to the hardcoded
   line — M4 slice H behaves byte-for-byte as it does today. No `.harness-profile.yaml` (M5's file,
   a **different** file — see §6) or CLI flag references a profile that no longer exists.

## 4. Non-goals

- **No second live agent.** That's chain items 5 (routing gate) and 6 (funnel) — separate,
  unbuilt milestones. This milestone's "proof" (§3.5) is a test asserting the argv, not a running
  second agent.
- **No `NetworkPolicy`.** Chain item 3, explicitly depends on this one (`network` field on
  `AgentProfile` is a placeholder `None`, not a type). Building it here would be doing item 3's job
  under item 2's name.
- **No config-driven profile *selection* UI.** Chain item 4 ("config-driven allowlist selection")
  is the `@group`/`enabled.txt`-style menu; this milestone hardcodes which profile a run uses
  (`DEFAULT_PROFILE`, or a manual override for testing) rather than building a picker for a set of
  profiles nothing yet needs to choose between.
- **No change to the docker mount-mask (M4 slices A–G) or `.agentignore`.** Those are the
  deny-list/floor visibility policy, evaluated regardless of the jail. `AgentProfile.binds` is
  strictly *inside* the bwrap allow-list boundary and only ever narrows what the jail additionally
  restricts — it cannot widen past what the mask/floor already hides, and this milestone does not
  touch `mask.py`.
- **No touching `deepagents.profiles.HarnessProfile` / `register_harness_profile`.** §1 explains
  why; adopting it is scoped to whichever future milestone actually needs per-agent *prompts*
  (plausibly chain item 6, when there's a role to author one for).

## 5. Open design forks — resolved here, revisit if wrong

**Fork A — does `AgentProfile.binds` express workspace-relative paths only, or absolute host
paths?** Resolved: **workspace-relative only** (`BindEntry.relpath`, joined onto `workspace` the
same way `mask.py`'s `MaskResult.masked` entries already are — `jail.bwrap_args:433`). An absolute
path would let a profile bind something outside the workspace, which is exactly the "Sandbox Escape
(Dynamic Binds)" risk `design_doc.md` §10 already names ("An incorrectly configured `HarnessProfile`
creates a bind mount that allows access to the host's `/root` or `/etc`" — mitigation: "reject any
path outside `/workspace`"). Enforcing "relative only" in the type is cheaper and more certain than
validating an absolute path stays inside the workspace after every `..`/symlink trick pathguard
already has to defend against elsewhere.

**Fork B — read-write vs. read-only granularity: per-entry, or one mode for the whole profile?**
Resolved: **per-entry** (`BindEntry.mode`). §2's own bind-mount sketch already shows mixed
examples ("Full Access" vs. "Read-Only Scoping" as separate bullets under one profile) — an
Architect-style profile plausibly wants full read access to `src/` and read-only to `docs/`
simultaneously, and a whole-profile mode can't express that.

**Fork C — where does a profile's bind list live: Python literal, or a config file like
`.agentignore`?** Resolved for *this* milestone: **Python literal** (`DEFAULT_PROFILE` +
whatever the proof profile in §3.5 needs). A file format is chain item 4's job ("config-driven
allowlist selection... makes it operable from a menu instead of hand-edited comment toggles") —
building one now, with nothing but `DEFAULT_PROFILE` to select between, is speculative surface. If
a future milestone adds a file, it should **not** be named `.harness-profile.yaml` or
`.harness-profiles.yaml` — that name is already M5's resolved-`Settings` profile file
(`project/.harness-profile.yaml`, singular, a completely different schema: model/budgets/HITL/mask
mode) and a name collision there would be actively misleading. `.harness-agent-profiles.yaml` (or
a `profiles/` directory, mirroring `workflows/`) are the two shapes worth choosing between when
that milestone starts.

## 6. Implementation reference

| File | Change |
|---|---|
| `harness/profile.py` (new) | `BindEntry`, `AgentProfile`, `DEFAULT_PROFILE`. Stdlib only. |
| `harness/jail.py` | `bwrap_args(..., profile: AgentProfile \| None = None)`; replace `jail.py:410`'s single `--bind` with a loop over `(profile or DEFAULT_PROFILE).binds`, inserted between the `project_root` bind and the state-dir bind, preserving the masked-last ordering (§3.6). |
| `scripts/sandbox-exec.sh` | Read `AGENT_BIND_SCOPE` (unset → today's line, unchanged); loop emitting `--bind`/`--ro-bind` per `relpath:mode` entry. |
| `tests/test_profile.py` (new, host tier) | `DEFAULT_PROFILE` round-trips to today's exact single bind; a scoped profile produces a differing argv (rw→ro, subset of paths); ordering invariant (§3.6) holds with a scoped profile *and* a masked path present together. |
| `tests/test_jail.py` | Extend existing `bwrap_args` argv-shape tests with a `profile=` case; assert the no-`profile`-argument call sites (every existing caller) are byte-identical to before this milestone. |
| `tests/test_sandbox_exec.py` (new, host tier) | First direct unit coverage of `sandbox-exec.sh` (today it is exercised only live, via `smoke -JailCheck`, and referenced only as a string pattern in `test_nsguard.py`'s denylist). A stub `bwrap` executable placed first on `PATH` for the subprocess (dumps its argv instead of execing) lets the script's `AGENT_BIND_SCOPE` loop be asserted the same way `bwrap_args` is — argv equality, not a live namespace. Cases: unset → today's single `--bind "$WS" "$WS"` unchanged; set → one `--bind`/`--ro-bind` pair per `relpath:mode` entry in order. |

No `run-docker.{ps1,sh}`, `config.py`, or `.harness-profile.yaml` changes in this milestone — there
is no selection surface to wire yet (§4, §5 Fork C), and `AGENT_BIND_SCOPE` stays unset in every
shipped launcher path until one exists.

## 7. Test plan sketch

- **Host tier** (`tests/test_profile.py`): pure dataclass + bind-list-to-argv-fragment logic, no
  bwrap/docker needed — mirrors `tests/test_limits.py`'s "arithmetic only" placement.
- **`tests/test_jail.py` additions**: `bwrap_args()` called with an explicit scoped `AgentProfile`,
  asserting the exact argv list (same style as the existing masked-overmount-ordering tests) —
  never a substring check, per the M7 §0.2 / M8 patch-fidelity lesson about serialized-blob
  assertions.
- **No live-model or smoke-tier case needed.** Nothing here depends on model behavior (the M6/M7/M8
  "exercise the real model" rule applies to behavior *the model* produces — this is pure bind-mount
  argv construction, deterministic and host-testable end to end). A live jail smoke case (`smoke
  -JailCheck`) should still be re-run once `bwrap_args` changes shape, to confirm the default-profile
  path still passes the existing M4.1 gates — regression, not new coverage.
