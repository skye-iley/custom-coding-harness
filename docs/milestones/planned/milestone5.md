# Milestone 5 — Unified Config Surface

## 0. Build status

**Planned** — docs only, no code yet. `milestone5_spec.md` (same folder) is the full
implementation-level spec: exact module layout, `Settings` dataclass, precedence algorithm, file
formats, CLI/REPL surfaces, and the concrete file-by-file diff. Read this doc for scope/DoD/why;
read the spec for how.

## 1. Goal & Definition of Done

Today, changing how a run behaves means editing `project/.env`, passing scattered CLI flags
(container-side only, and only for a handful of knobs), or hand-setting environment variables —
and the **security** knobs (mask mode, bwrap jail, AppArmor, resource caps, NetJail) are
host-launcher-only, with no menu and only partial flag coverage. There is no single place to see
"what will this run actually do."

Goal: a human testing the harness can see and change *most* config — which model, HITL posture,
mask/jail/security posture, budgets, resource caps — without hand-editing files, through:

- **CLI flags** for a single-run override (works today for a few knobs; extend to the rest).
- **An in-session `/config` menu** for the subset that can change without restarting the
  container (model, budgets, HITL preset, topic).
- **A pre-spinup wizard** (host side, before `docker run`) for the subset that's fixed at
  container start (mask mode, jail/AppArmor, resource caps, NetJail) — the "separate security
  config program, for now" the ask named, sharing the same resolver/profile machinery as
  everything else rather than being built twice.

`.env` keeps its current role: the baseline record of defaults when nothing else overrides it.

Done when:
- `harness/config.py` resolves every knob in §5 through one precedence chain, and both
  `parse_args()`/`run-docker.{ps1,sh}` and the `/config` REPL command read/write the same
  resolved object — no second source of truth.
- A saved profile (`project/.harness-profile.yaml`, gitignored, `.example` checked in) persists
  explicit choices across runs; CLI flags and REPL edits are session-only unless explicitly saved.
- `harness config` (keyless, host side) walks through model + security posture and writes the
  profile; `harness config security` is the narrower security-only entry point.
- `harness doctor` reports the *resolved* config (profile + env + CLI merge), not raw env grepping.
- Every existing env-var-only knob keeps working un-set — this is additive. No profile file present
  ⇒ byte-for-byte current behavior (removable contract, like every prior milestone).

## 2. Why this is its own milestone

Not a bug fix or a slice of M4 — it touches the CLI parser, `run-docker.{ps1,sh}` (both), the REPL
loop, and adds a new host-side entry point, all sharing one new module. It's QOL, not a trust- or
capability-boundary change, but it's a prerequisite for anyone other than the harness's author
comfortably testing the security posture (M4) or HITL posture (M3) without reading source.

## 3. The pre-spinup / in-session split (why config isn't one flat list)

This is the load-bearing fact the design turns on. Confirmed against the current code
(`run-docker.ps1:278-334` for the mask pre-flight; the launcher-env table in
`deepagent-image/CLAUDE.md` for the rest):

| Fixed at container start (host side, `run-docker`) | Live in-session (container side) |
|---|---|
| Mask scan + empty-overlay mounts (`DEEPAGENTS_MASK`/`_MODE`) | Model/provider (rebuilds the agent) |
| bwrap jail (`DEEPAGENTS_JAIL` → `--security-opt seccomp=...`) | Budgets (`--max-cost`/`--max-tokens`) |
| AppArmor profile selection (`DEEPAGENTS_JAIL_APPARMOR`) | HITL `autonomy_level`/`review_triggers`/`on_deny` |
| Resource caps (`--cpus`/`--memory`/`--pids-limit`) | Topic label |
| NetJail (`--internal` network + allowlists) | — |
| `MAP_HOST_USER`/ephemeral workspace mode | — |

An in-REPL menu **cannot** touch the left column without a container restart — no amount of UI
polish changes that. So "in-repl/in-program menu" (the ask) means two different programs at two
different times, not one menu with everything in it:

- **Pre-spinup**: CLI flags on `run-docker`, or the `harness config` / `harness config security`
  wizard, writing the profile *before* `docker run`.
- **In-session**: `/config` REPL command, editing the live subset only, with an explicit `save`
  to persist.

## 4. Scope (slices, in build order)

### C1 — `harness/config.py`: the resolver — *foundation, everything else depends on it*

One `Config` dataclass covering every knob in §5. Precedence, highest wins:

```
CLI flag  >  env var (shell-exported or .env, indistinguishable once dotenv loads it)  >
profile file (.harness-profile.yaml)  >  built-in default
```

Rationale for env-above-profile: `.env` is already the ask's "record of defaults when run without
any other config" and dotenv loads it into `os.environ` before anything else runs, so treating
"env" as one tier (ambient) above "profile" (an explicit prior save) keeps the mental model to
three tiers an operator actually reasons about: *what I typed just now* > *what's ambient* >
*what I saved last time*. Pure host-runnable, stdlib only — same tier as `providers.py`/`loaders.py`
(`tests/test_config5.py` or similar; name TBD to avoid colliding with the existing `harness/config.py`
HITL module — see open fork in §6).

### C2 — CLI flag parity (container side)

Extend `parse_args()` to route through C1 instead of `_env_defaults()` directly (behavior
unchanged, just re-plumbed). No new container-side flags are obviously missing today beyond what
C1 needs internally.

### C3 — CLI flag parity (host side, `run-docker.{ps1,sh}`)

Add flags mirroring the launcher-env table for knobs that only have env-var coverage today:
`-Model`/`MODEL`, `-MaskMode`/`MASK_MODE` (currently env/`.env`-only per the pre-flight code),
`-Jail`/`JAIL`, `-Autonomy`/`AUTONOMY` (writes the HITL preset the container reads). Keep the
existing `-Cpus`/`-Memory`/`-PidsLimit`/`-NetJail`/`-Ephemeral`/`-SaveWorkspace`. **Keep `.ps1`/`.sh`
in sync** (repo-wide rule).

### C4 — Profile file (`.harness-profile.yaml`)

New file, gitignored like `.env`/`.harness-config.yaml`; `.harness-profile.yaml.example` checked
in. Schema mirrors `Config` field names. `harness config save` (keyless) and `/config save`
(in-session) both write through the same C1 serializer, so there's one writer, not two.

### C5 — `/config` REPL command (in-session, live subset only)

View the resolved config (source-tagged per field — CLI/env/profile/default, so it's visible *why*
a value is what it is); edit model (triggers agent rebuild via the existing `build_agent` path),
budgets, HITL `autonomy_level`/`review_triggers`/`on_deny`, topic. Reuses the S6 arrow-select
infra (`cli._arrow_select`) when `prompt_toolkit`+TTY are available, falls back to typed input
otherwise — same pattern as the existing HITL `choose` prompts, not a new input stack.

### C6 — `harness config` wizard (host side, keyless, pre-spinup)

Standalone entry point (`python3 -m harness config`, or a thin `configure.{ps1,sh}` wrapper if a
host-native launcher reads better than asking the user to have the harness venv active — TBD,
see §6). Walks model + security posture questions, writes the profile via C4's shared writer.

### C7 — `harness config security` (host side, keyless, pre-spinup)

Narrower entry point: mask mode, a guided `.agentignore` editor (add/remove patterns, floor
entries), jail/AppArmor toggle, resource caps, NetJail domain/host-service list editor. This *is*
the "separate security config program, for now" — built as a subcommand of the same module so it
shares C1's resolver and C4's profile writer instead of duplicating either, per the "eventually
incorporated into the main program" framing (it already is the same program; it's just a narrower
entry point into it).

### C8 — `harness doctor` + docs integration

`doctor` reports resolved config (profile + env + CLI merge) instead of grepping raw env vars, so
its output matches what an actual run will do. `ENV_VARS.md` gets a pointer to the profile file as
the persisted alternative to hand-editing `.env`. `deepagent-image/CLAUDE.md` gets a "Unified
config" section (like the existing Model routing / Present-past-memory sections).

## 5. Config surface (knobs this milestone brings under the resolver)

| Knob | Today | Tier |
|---|---|---|
| Model/provider (`DEEPAGENTS_MODEL`, `--model`) | env + CLI | in-session |
| Thread id / topic | env + CLI | in-session |
| Budgets (`--max-cost`/`--max-tokens`) | env + CLI | in-session |
| Headless (`--headless`) | env + CLI | pre-spinup (behavior fixed at process start) |
| HITL `autonomy_level`/`review_triggers`/`on_deny` | `.harness-config.yaml` only | in-session |
| Mask mode / `.agentignore` | env + in-workspace file | pre-spinup |
| Jail (`DEEPAGENTS_JAIL`) / AppArmor stance | env only | pre-spinup |
| Resource caps (cpu/mem/pids) | launcher flags only | pre-spinup |
| NetJail on/off + allowlists | launcher flag + text files | pre-spinup |
| `MAP_HOST_USER`, ephemeral workspace | launcher flags/env only | pre-spinup |

## 6. Forks — resolved (see `milestone5_spec.md` for the concrete design)

1. **Module name collision — resolved: absorb at the API level, not the file level.**
   `harness/config.py` keeps its name; its existing HITL `Config` dataclass is renamed
   `HitlSection` and nests inside a new `Settings` dataclass as `Settings.hitl`. The two **on-disk
   files** (`.harness-config.yaml` for HITL, `.harness-profile.yaml` for everything else) stay
   separate — the HITL parser is already a tested, non-trivial grammar not worth risking in a
   merge. `milestone5_spec.md` §1/§3/§4.
2. **`harness config` invocation ergonomics — resolved: no new wrapper script pair.** The wizard
   is a keyless subcommand of the existing `harness` entry point (`harness config`), same as
   `harness threads`/`harness past`/`harness doctor` — all already assume the harness venv is
   active. `run-docker` itself stays venv-independent (§3 of the spec): its new profile-file
   reads are a regex scrape, like the existing `.env` scrape, not a Python call.
3. **File-ownership rule — resolved:** `.harness-config.yaml` = HITL only (unchanged from M3);
   `.harness-profile.yaml` = everything else this milestone adds. One line each in
   `deepagent-image/CLAUDE.md`'s new "Unified config" section (`milestone5_spec.md` §11).

## 7. Removable contract

No profile file present, no new CLI flags passed ⇒ every knob resolves exactly as it does today
(env var → built-in default). `harness/config.py` (or its absorbed form) can be deleted along with
the profile-file support and `parse_args()`/`run-docker` revert to reading `os.environ` directly —
no residual coupling, same pattern as every prior milestone's removable contract.
