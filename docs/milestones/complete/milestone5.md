# Milestone 5 — Unified Config Surface

## 0. Build status

**Built** — C1–C8 all landed on `feat/milestone5-unified-config`, plus two follow-ups added after
initial review (`-Autonomy`/`AUTONOMY` in C3, the NetJail list editor in C7 — both initially scoped
out for time, then built once asked "why not?"). `milestone5_spec.md` (same folder) is the
implementation-level spec this was built from; §8 below is the checkable-invariant companion,
folded in on completion per the repo's milestone lifecycle. One deliberate deviation from the
original spec sketch remains: C5 drops the planned arrow-key `/config` menu for typed commands
(see its §4 note — most settable fields are free text, not a fixed choice list, so a picker UI
doesn't fit most of them anyway). One real bug the build surfaced and fixed along the way: §4 C5's
`PauseMiddleware` caching note.

### 0.1 Second-pass review fixes

A review of the completed branch found the resolver and both wizards correct in isolation but
**under-plumbed at the edges** — six fields were written and read by nothing. All fixed on the
same branch; each carries a regression test.

| Bug | Why it mattered | Fix |
|---|---|---|
| `.harness-profile.yaml` never reached the container | Gitignored ⇒ not in the Dockerfile's enumerated `COPY`, and `run-docker` mounted only `workspace`/`state`/`.harness-config.yaml`. So the container's `resolve_settings()` **always** saw an empty profile tier: `topic`/`max_cost`/`max_tokens` silently ignored on every containerized run (`model` worked only via the explicit `-e` forward), and `/config save` wrote into the `--rm` container layer and printed success. | Bind-mount it into `/project`, read-write. `save_profile` gained an in-place fallback: a single-file bind mount can't be replaced by rename (`EBUSY`), so the atomic tmp+`replace` would have crashed in the exact deployment the mount exists for. |
| `cpus`/`memory`/`pids_limit`/`net_jail` resolved nowhere | `harness config security`'s custom posture prompts for all four, `save_profile` writes them, the `.example` documents them — and both launchers still read raw `${CPUS:-2}` / `[string]$Cpus = "2"`. `check-parity` even asserted a `cpus` profile fixture, so the resolver existed and only the call site was missing. Directly contradicted §8's "never silently accepted or no-op'd". | Routed through `_resolve_host_setting`/`Resolve-HostSetting`. The `.ps1` cap defaults moved to `""` (a literal default shadows the profile tier); `-NetJail` is a `[switch]`, so it consults the lower tiers only when `$PSBoundParameters` shows it genuinely absent. |
| `/config`'s pre-spinup half reported defaults for host-only knobs | Caps and NetJail are `docker run` flags, never env vars, so the read-only display showed `cpus = 2 (default)` under `-Cpus 8`. A read-only view whose job is "what will this run actually do" that reports the opposite is worse than omitting the field. | `run-docker` forwards them as informational `-e`. Nothing in the container acts on them. |
| `/config set model` left the cost tracker on the old model | `CostTrackerMiddleware` caches `_pricing`/`_rates`/`_bare_model` at construction; only the chat model was swapped. Every post-switch turn billed at the launch model's rates, reported under the launch model's name. | `CostTrackerMiddleware.reprice()`, called from the switch. Budgets and accumulated totals deliberately survive — a budget is a session ceiling, not a per-model one. A session with no tracker (launched unpriced) says so instead of starting a half-accurate one mid-run. |
| `/config` source tags were wrong for anything set by a CLI flag | `_handle_config` re-resolved with no `cli=` tier, so `--max-cost 5` displayed `(default)`. The provenance tags are the entire point of the display. | Thread `parse_args()`'s already-resolved `(Settings, SettingsSources)` pair through `run_repl`. |
| Wizard "default" posture and "off" HITL preset were silent no-ops | `_wizard_security_step` returned `{}` for "default", so a saved `jail: true` survived a run that reported "jail off"; `_wizard_hitl_step` returned `None` for "off", indistinguishable from "screen skipped", so an existing `.harness-config.yaml` stayed and HITL stayed on. | Both write their choice explicitly. "off" moves `.harness-config.yaml` aside to `.disabled` rather than deleting it — presence-of-file *is* the switch, but the file may carry a hand-written `review_triggers` block a config wizard has no business destroying. |

`check-parity.{sh,ps1}` gained markers for the profile mount, the cap/NetJail profile keys, and
the cap env forward, so a one-sided edit to either launcher fails CI instead of drifting.

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

### C1 — `harness/config.py`: the resolver — *foundation, everything else depends on it* — **built**

`Settings`/`SettingsSources` dataclasses covering every knob in §5, plus the renamed
`HitlSection` (was `Config`) nested at `Settings.hitl`. Precedence, highest wins:

```
CLI flag  >  env var (shell-exported or .env, indistinguishable once dotenv loads it)  >
profile file (.harness-profile.yaml)  >  built-in default
```

Rationale for env-above-profile: `.env` is already the ask's "record of defaults when run without
any other config" and dotenv loads it into `os.environ` before anything else runs, so treating
"env" as one tier (ambient) above "profile" (an explicit prior save) keeps the mental model to
three tiers an operator actually reasons about: *what I typed just now* > *what's ambient* >
*what I saved last time*. Pure host-runnable, stdlib only — same tier as `providers.py`/`loaders.py`;
tests extend the existing `tests/test_config.py` rather than a new file (the name collision in the
original plan turned out not to need one — one file, two sections).

### C2 — CLI flag parity (container side) — **built**

`parse_args()` routes through C1's `resolve_settings(cli=args)` instead of the deleted
`_env_defaults()` (behavior unchanged, just re-plumbed). Settings-covered flags default to `None`
on the parser (rather than argparse's implicit default) so "not passed" is distinguishable from
"passed as falsy" — the trick `--headless` (`action="store_true", default=None`) needs. No new
container-side flags were needed.

### C3 — CLI flag parity (host side, `run-docker.{ps1,sh}`) — **built**

Adds `-Model`, `-MaskMode`, `-Jail`, `-JailApparmor`, `-Autonomy` to `run-docker.ps1` (`.sh`'s
"flag" is already its env-var invocation, `VAR=x ./run-docker.sh`, so no new `.sh` flags were
needed — only the shared resolver). `-Autonomy`/`AUTONOMY` is shaped differently from the other
four: the HITL preset isn't a `Settings`/profile field, it's a whole-file swap-in
(`.harness-config.yaml`'s presence *is* the on/off switch, §6 fork 1), so it isn't resolved
through `Resolve-HostSetting`'s four-tier chain — it's a plain-text write/update of the
`autonomy_level:` line (creating the file if absent, preserving every other line if present),
mirroring `harness config`'s `write_hitl_preset` but in bash/PowerShell so `run-docker` stays
Python-independent. Necessarily turns HITL on for the run if it wasn't already (that's the
point, not a side effect — printed, never silent). `DEEPAGENTS_MASK` (mask on/off) deliberately
keeps no flag/profile tier either — it's a debugging escape hatch, not a saveable default, matching
`Settings.mask_enabled`'s exclusion from the profile (§5). A new `scripts/lib/config.{ps1,sh}`
(`Resolve-HostSetting` / `_resolve_host_setting`) replaces three near-duplicated scrape blocks per
script with one function; `check-parity.{ps1,sh}` gained a fixture-based section proving both
resolvers agree on the same inputs (no cross-language invocation attempted — `pwsh`/`bash`
availability on the other's CI runner isn't guaranteed).

### C4 — Profile file (`.harness-profile.yaml`) — **built**

New file, gitignored like `.env`/`.harness-config.yaml`; `.harness-profile.yaml.example` checked
in. Schema mirrors `Settings` field names (minus `thread_id`/`headless`/`mask_enabled`/`hitl` — see
§5's "Not in the profile"). `harness config set`/the wizard's save step (keyless) and `/config save`
(in-session) both write through the same `save_profile()`, so there's one writer, not two. Building
the example file surfaced a real parser bug — a `key:   # comment-only` line (used for every unset
key in the template) was read as the literal string `"# comment-only"` instead of unset, since the
comment-stripping helper only trimmed a *trailing* comment off a real value, not a value that IS a
comment. Fixed in `load_profile` (and mirrored in both `lib/config.{ps1,sh}` scrapers); regression
test added.

### C5 — `/config` REPL command (in-session, live subset only) — **built, differently shaped**

`/config` (view, source-tagged, live fields first then pre-spinup read-only), `/config set <field>
<value>` (one live field), `/config save` (persist session edits to the profile). **Dropped from
the original plan: the S6 arrow-select menu.** `/config set` is a typed command
(`field value`), not a picker — matching how `/topic`/`/recall` already work, and simpler to make
correct than reusing `cli._arrow_select`'s `InterruptRequest`-shaped machinery for a value it was
never built to carry. `model` rebuilds the agent through the same `validate_credentials` +
`build_agent` call `main()` makes at startup (via a closure `main()` passes into `run_repl`), so a
bad model fails the same way live as at launch. `max_cost`/`max_tokens` mutate the live
`CostTrackerMiddleware`'s budget attributes directly (refused if no tracker is active this
session — nothing to enforce a budget against). **A real bug the build surfaced:**
`hitl.autonomy_level`/`on_deny` are supposed to "mutate the live object PauseMiddleware already
holds a reference to... applies to the next gated call" — but `PauseMiddleware.__init__` cached
`_gate_all_tools`/`_on_deny` as plain attributes at construction, so a live edit to the (frozen,
`object.__setattr__`-mutated) `HitlSection` would have had no effect on already-gated behavior.
Fixed by reading both live off `self._config` on every call instead of caching; `review_triggers`
editing was never in scope (a trigger *list* needs add/remove semantics `/config set` doesn't have)
and stays file-only, unchanged from M3.

### C6 — `harness config` wizard (host side, keyless, pre-spinup) — **built, differently shaped**

`harness/config_cli.py`, wired into `cli.dispatch` as `argv[0] == "config"` (no new wrapper
script — same pattern as `threads`/`past`/`doctor`). `harness config` walks model + security
posture + HITL preset, then confirms (or `--save` skips the prompt) before writing through C4's
shared `save_profile()`; `harness config show` prints the resolved config with no prompts;
`harness config set <field> <value>` is a one-shot non-interactive write, validated by
round-tripping the merged file through `load_profile()` and rolling back on a bad value.
**Dropped from the original plan: the arrow-key `prompt_toolkit` dialog.** Plain numbered `input()`
choices instead, so the module stays fully dependency-light (stdlib + `harness.config` +
`harness.providers` only — no langchain/deepagents pulled in just to run the wizard) and
deterministically testable via a stubbed `input()`. The HITL preset needed a writer that didn't
exist (M3 never wrote `.harness-config.yaml`, only hand-copied the `.example`); `write_hitl_preset`
only touches the `autonomy_level` line, so a hand-edited `review_triggers` block survives.

### C7 — `harness config security` (host side, keyless, pre-spinup) — **built**

The tail of C6's same wizard with the model/HITL screens skipped ("already the same program, a
narrower entry point"): mask mode, jail/AppArmor toggle, resource caps, plus two quick-edit
loops:
- **`.agentignore`** — add a masked path or a floor entry, appending to the **workspace's**
  `.agentignore` — a convenience wrapper, not a new masking mechanism; M4 still owns the file's
  format.
- **NetJail allowlists** — add/delete entries in `netjail/host-services.txt` and
  `netjail/allowed-domains.txt` (list current entries, add one, or delete by number), editing the
  files in place while preserving their comments. Resolved relative to `config_cli.py`'s own file
  path (`netjail/` is a sibling of `project/`, not copied into the image), so this only works when
  `harness config security` runs on the host against a checked-out repo — the same pre-spinup
  context every other knob in this slice assumes. Inside a container it reports the directory as
  not found and skips, rather than erroring.

### C8 — `harness doctor` + docs integration — **built**

`doctor_main` prints one resolved-config summary line (`resolve_settings()`, no `cli=` override —
reflects what an *unflagged* run would do) ahead of its existing checks, which are otherwise
untouched. `ENV_VARS.md` gets a pointer to the profile file as the persisted alternative to
hand-editing `.env`, plus the four new launcher flags. `deepagent-image/CLAUDE.md` gets a "Unified
config" section (module list, precedence, the pre-spinup/in-session split, `/config`/`harness
config`, file ownership, removable contract) plus updates to the REPL command table, the admin
command block, and the Feature Toggles table.

## 5. Config surface (knobs this milestone brings under the resolver)

| Knob | Today | Tier |
|---|---|---|
| Model/provider (`DEEPAGENTS_MODEL`, `--model`) | env + CLI | in-session |
| Thread id / topic | env + CLI | in-session |
| Budgets (`--max-cost`/`--max-tokens`) | env + CLI | in-session |
| Headless (`--headless`) | env + CLI | pre-spinup (behavior fixed at process start) |
| HITL `autonomy_level`/`on_deny`/`interruption_policy` | `.harness-config.yaml` only | in-session (built); `review_triggers` stays file-only — a trigger *list* needs add/remove semantics `/config set` doesn't have |
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

## 8. Invariants (folded in from `milestone5_invariants.md` on completion)

The checkable assertions this milestone's build and tests were held to. Test references are in
`deepagent-image/project/tests/test_config.py`, `test_cli.py`, `test_config_cli.py`, and
`test_doctor.py` unless noted.

1. **Precedence holds per field.** For every scalar `Settings` field, `resolve_settings(cli=X,
   env=Y, profile_path=Z)` returns the CLI value when set, else the env value when set, else the
   profile value when set, else the built-in default — never any other order, with all four tiers
   populated simultaneously per field.
2. **Removable contract: no profile, no new flags ⇒ byte-for-byte unchanged.** With no
   `.harness-profile.yaml` and no new CLI flags, live-field sources are always `"env"` or
   `"default"`, never `"profile"`; `parse_args()` defaults are unchanged after the C2 re-plumb.
3. **`HitlSection` is a pure rename.** Field names, defaults, `gated_hooks()`,
   `system_interrupt_enabled()`, and every pre-existing `.harness-config.yaml` parse/match test
   pass unchanged against `HitlSection` (only the import name changed at call sites).
4. **`LIVE_FIELDS` is the single source of truth for the pre-spinup/in-session split.** Defined
   once in `config.py`; `cli.py`'s `_CONFIG_PRESPINUP_FIELDS` is *derived* from
   `dataclasses.fields(Settings)` minus `LIVE_FIELDS`, not hand-duplicated, asserted by
   `test_config_prespinup_fields_match_live_fields_complement`.
5. **Unknown profile key fails loud.** A `.harness-profile.yaml` top-level key outside
   `PROFILE_FIELDS` raises `SystemExit` at load time — same policy as `.harness-config.yaml`.
6. **`save_profile` merges, never overwrites.** Saving `{model: "x"}` when the on-disk profile
   already has `jail: true` leaves `jail: true` intact — read-modify-write, not replace.
7. **`/config set` only accepts live fields.** A pre-spinup field (e.g. `mask_mode`, `jail`,
   `cpus`) is refused with a pointer to `harness config`, never silently accepted or no-op'd.
   `harness config set` (the host-side one-shot) is the mirror-image restriction: it accepts any
   `PROFILE_FIELDS` key (both live and pre-spinup) since it writes the profile directly, not the
   in-session object — the REPL/host asymmetry is deliberate, not a gap.
8. **`/config` mid-turn cannot corrupt state.** `run_repl`'s loop is synchronous — a turn always
   completes or cancels before the `you>` prompt reappears — so no code path can read `/config`
   input while a turn is in flight. Structurally absent, not runtime-guarded (a planning-stage
   assumption of an explicit "unavailable mid-turn" refusal was dropped as dead code once this
   became clear — see §4 C5).
9. **`/config set model <spec>` re-validates credentials.** A model switch that would fail
   `validate_credentials` at launch fails the same way live, via the same call path
   (`_rebuild_agent` in `main()`); the switch is rejected and the prior model stays active.
10. **Secrets never enter `Settings`, `SettingsSources`, or the profile file.** No field in either
    dataclass, nor any `PROFILE_FIELDS` key, holds an API key or credential — structurally true by
    field-list construction, not a runtime check. `harness doctor`'s credential check keeps
    reading raw env directly, never through `Settings`.
11. **Host-side resolution is dependency-free.** `run-docker.{ps1,sh}` resolve every pre-spinup
    knob via `lib/config.{ps1,sh}` without requiring the harness venv or Python — only `harness
    config`'s wizard needs the venv (it's a `python3 -m harness` subcommand).
12. **`.ps1`/`.sh` host resolution parity.** For a fixture profile + env combo, both resolvers
    return identical values for every knob — asserted by `check-parity.{ps1,sh}`'s dedicated
    section (each independently checked against the same expected literals, not cross-invoked,
    since `pwsh`/`bash` availability on the other's CI runner isn't guaranteed).
13. **Profile file is gitignored and never baked into the image.** `.harness-profile.yaml` is in
    the repo `.gitignore` and absent from the Dockerfile's enumerated `COPY` list, same treatment
    as `.env` and `.harness-config.yaml`.
14. **`harness doctor` reports resolved config, not raw env.** Doctor's summary line is built from
    `resolve_settings()` with no `cli=` override, reflecting what an *unflagged* run would do.
15. **Full removable contract.** Deleting `config_cli.py`, the profile-file branch inside
    `config.py`'s resolvers, and `lib/config.{ps1,sh}` (reverting `run-docker` call sites to direct
    env reads) restores byte-for-byte pre-Milestone-5 behavior. `HitlSection`/
    `.harness-config.yaml` semantics (presence-of-file = HITL on) are untouched.

**Verification note:** `harness/cli.py` cannot be imported without the full deepagents/langgraph/
langchain stack (a pre-existing condition of the codebase, not introduced here — `dispatch()`
routes every keyless subcommand, including `doctor`/`threads`/`past`, through `harness/__init__.py`
→ `harness.cli`). Everything touching `cli.py` (C2, C5, C8) was verified by building a throwaway
venv with that stack installed and running the full suite against it (709 passed, 7 pre-existing
environment-only skips), not just by static review — `harness/config.py` and
`harness/config_cli.py` (C1, C3, C4, C6, C7) are dependency-light by design and were verified
directly on the bare host, including a real subprocess run of `main.py config show`/`config set`.
