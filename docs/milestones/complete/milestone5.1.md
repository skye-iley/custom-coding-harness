# Milestone 5.1 — Config Field Registry

## 0. Build status

**Built — all seven slices (R1–R7) landed** on `feat/milestone5.1-config-field-registry`, merged to
`main` in PR #43. Follows
`milestone5.md` (Unified Config Surface, complete), which this refactors without changing any
resolved value. Named `5.1` on the `milestone4.1.md` precedent: a follow-on slice of a shipped
milestone, not a new capability. Checkable properties: §9 (folded in from
`milestone5.1_invariants.md` on completion).

| Slice | What landed |
|---|---|
| **R1** | `FieldSpec` + `FIELD_SPECS` in `config.py` (19 entries: 15 `Settings` scalars + `hitl` + 3 dotted `hitl.*`). `resolve_settings`'s hand-listed `field(...)` block is one loop over the registry. `Settings`/`SettingsSources` stay explicit frozen dataclasses (fork 1) with a test asserting their field names **and order** equal the registry's. |
| **R2** | `PROFILE_FIELDS`, `_PROFILE_WRITE_ORDER`, `LIVE_FIELDS`, and the four `_PROFILE_{BOOL,FLOAT,INT,STR}_FIELDS` cast buckets are all derived. The buckets are gone entirely — `_parse_profile_value` selects the strict file parser from the spec's own `cast`. The four M5 exclusions are `profile_key=None` with the reason in each entry's comment. |
| **R3** | One renderer: `config.format_config_lines(settings, sources, *, prefix, width, prespinup_header, overrides, edited)`. `cli._config_display_lines` and `config_cli.format_settings_lines` are wrappers; both call sites' output is byte-identical. |
| **R4** | `_handle_config`'s if-chain is a `LiveContext` + `_LIVE_APPLIERS` dispatch. `_CONFIG_HITL_VALIDATORS` is now a view of `choices`, and validation is one check for **every** enum field rather than a hitl-only branch. |
| **R5** | The wizard's custom-posture branch is a loop over `WIZARD_PRESPINUP_SPECS`; `_ask_field` renders enum → numbered menu, bool → off/on menu, `wizard="confirm"` → y/N, else a text prompt carrying its default. Prompts are byte-identical (the M5 input-sequence test passes unchanged). The posture shortcuts stay hand-written — opinions about *combinations*, which a per-field registry can't express. |
| **R6** | `_arrow_select` takes a plain options list + optional header. `/config set <field>` with no value opens the picker for any field with `choices`; Esc / no `prompt_toolkit` / non-TTY returns `None` and falls back to today's `usage:` error. |
| **R7** | `test_prespinup_profile_keys_are_consumed_by_both_launchers` asserts every `tier="prespinup"` spec with a `profile_key` appears in **both** launchers. The ad-hoc `pids_limit`/`net_jail` markers M5 added to `check-parity.{sh,ps1}` are removed — the test covers all seven, and a new knob for free. |

### 0.1 Deviations from the plan above

Two, both recorded rather than quietly taken:

1. **`FieldSpec.apply` is not on the spec** (§5 sketched it there). An applier mutates the
   `CostTrackerMiddleware`, the `past.sqlite` connection, and the rebuilt agent — `config.py`
   imports none of those and must not (the acyclic import rule `test_import_isolation` enforces
   for `cost.py` is the same layering instinct). Instead `FieldSpec.settable` is the *declaration*
   and `cli._LIVE_APPLIERS` holds the *behaviour*, keyed by the registry's own field names, with a
   test asserting the two agree exactly **in both directions**. A settable field with no applier
   fails CI; an applier naming no field is caught as dead code.
2. **`_handle_config` keeps its `(model, agent, topic)` signature as an adapter.** R4's point —
   the function stops growing a return-tuple slot per field — is achieved by the `LiveContext` the
   appliers mutate; the three-value return is computed off the context at the end. Changing the
   *public* shape would have meant editing ~20 M5 tests that call it, which §8's oracle rule
   forbids for anything that isn't a now-derived constant. New fields are threaded through the
   context, not the tuple, so the growth problem is gone either way.

### 0.2 Result against §8's safety net

The M5 suite is the oracle and it passed **with zero edits to any existing test**: the host tier was
green at its pre-M5.1 count (630 passed / 13 skipped) after R1–R6 landed and before a single new
test was added. Every test this milestone adds is either a derivation guard (for a structure that
used to be hand-written) or a regression test for §3.1's validation gap. Final counts: **655 host
(13 skipped), 785 in the `test` image (19 skipped)** — the one new skip is R7's launcher guard,
which correctly skips in the image, where `scripts/` is not present, and runs in the host tier CI
uses.

Verified against a real container, not only the host:
- `harness config show` renders byte-identically to M5.
- `harness config set mask_mode alow` exits 1 with
  `mask_mode must be one of ('deny', 'allow'), got 'alow'` and writes nothing;
  `... mask_mode allow` exits 0 and the key lands in the profile.
- The **live-model tier passes** (4/4 against a host `ollama serve`, not skipped), so the changed
  startup path — `resolve_settings`'s loop now builds every `Settings` field — actually boots a real
  agent and completes real turns, not just stubbed ones.

## 1. Goal & Definition of Done

Milestone 5 put every run knob behind one precedence chain. It did **not** put them behind one
*declaration* — a knob is currently spelled out in ten places, and the code that renders a knob
carries no idea what kind of thing it is. Two consequences, one for maintainers and one for users:

- Adding a config field is a ten-site edit where nine sites are silent if you miss them (§3).
- `/config set` can only be a typed command, because no field knows its own valid values. The
  arrow-key menu M5 scoped out (`milestone5.md` §4 C5) is blocked on exactly this, not on UI work.

Goal: **one `FieldSpec` table is the single declaration of a knob**, and everything else — the
`Settings` dataclass, profile I/O, the resolver loop, both display renderers, `/config set`
validation and dispatch, the wizard's screens, and the picker — derives from it.

Done when:
- `harness/config.py` holds a `FIELD_SPECS` tuple. Adding a live field is **one entry**, and a test
  fails if any derived structure was hand-maintained instead of derived.
- The M5 test suite passes **unchanged** except where a test asserts a hand-written constant that
  this milestone makes derived. Resolved values, precedence, file formats, and stderr text are
  byte-identical — this is a refactor plus exactly **two** deliberate additions: the picker (§4 R6)
  and enum validation (below). Anything else that moves is a bug.
- `/config set <field>` with **no value** opens an arrow-key picker for any field carrying
  `choices`; free-text fields keep today's typed path. `/config set <field> <value>` is unchanged.
- **Every enum-valued knob rejects an invalid value at the point of entry**, closing §3.1's
  validation gap — `harness config set mask_mode alow` fails loudly instead of persisting a string
  that silently resolves to the opposite mode. This is the one place the milestone is *allowed* to
  change behavior, and it needs its own regression test.
- `harness config`'s wizard screens are generated from the registry rather than hand-written
  `_numbered_choice` calls, so a new choice-typed knob appears in the wizard for free.
- One display renderer, not the two that render the same data in two formats today.

## 2. Why this is its own slice, not part of M5

Two reasons it was deliberately **not** folded into M5's review-fix commit:

1. **It changes no behavior.** M5's second pass (`milestone5.md` §0.1) fixed six real
   plumbing bugs. Mixing a behavior-preserving restructure of every M5 module into that diff would
   have made the part that *does* change behavior unreviewable.
2. **It is not pure cleanup.** The user-facing win — the picker — is gated on it. A refactor with a
   feature behind it earns its own doc; a refactor without one usually doesn't.

## 3. The problem — count the edit sites

Adding one **live** field to M5 as it stands today:

| # | Site | File |
|---|---|---|
| 1 | `Settings` dataclass field | `config.py:320` |
| 2 | `SettingsSources` dataclass field (same name, `str` type) | `config.py:342` |
| 3 | `LIVE_FIELDS` frozenset | `config.py:367` |
| 4 | One of `_PROFILE_{BOOL,FLOAT,INT,STR}_FIELDS`, by cast | `config.py:304-307` |
| 5 | `_PROFILE_WRITE_ORDER` tuple | `config.py:313` |
| 6 | A `field(...)` call in `resolve_settings` | `config.py:524-543` |
| 7 | `_CONFIG_SETTABLE_FIELDS` tuple | `cli.py:677` |
| 8 | A hardcoded `fmt(...)` line in `_config_display_lines` | `cli.py:722` |
| 9 | A branch in `_handle_config`'s if-chain | `cli.py:790` |
| 10 | `_DISPLAY_LIVE_FIELDS` tuple | `config_cli.py:47` |

A **pre-spinup** field adds an eleventh: a `_resolve_host_setting` / `Resolve-HostSetting` call in
each launcher — and that one is load-bearing, since skipping it is precisely the bug M5's second
pass found (caps and NetJail written by the wizard and read by nothing, `milestone5.md` §0.1).

Miss #3 and the field is silently classified pre-spinup. Miss #5 and `/config save` drops it. Miss
#7 and `/config set` rejects it as unknown. Miss #8 and it never displays. None of these fail loud.

Two symptoms are **already in the file**, not hypothetical:

- `_CONFIG_PRESPINUP_FIELDS` (`cli.py:684`) is correctly *derived* from `LIVE_FIELDS`, with a test
  asserting so — while `_CONFIG_SETTABLE_FIELDS` two lines above it is hand-written. The right
  instinct was applied to one of a matched pair.
- `cli._config_display_lines` and `config_cli.format_settings_lines` render the same data with two
  different widths and one `[harness] ` prefix. Same logic, twice, drifting independently.

### 3.1 The missing-`choices` hole is not just cosmetic

`_CONFIG_HITL_VALIDATORS` (`cli.py:687`) is the **only** place any field's valid values are written
down, and it covers three of them — the three `hitl.*` sub-fields. Every other enum-valued knob has
its legal values either hardcoded inline in one wizard prompt (`mask_mode`'s `["deny", "allow"]`,
`config_cli.py:298`) or nowhere at all.

That is what blocks the menu — a picker needs a value list, and no field carries one. It is also a
**live validation gap**, verified against the branch:

```
$ harness config set mask_mode definitely-not-a-mode
[harness] wrote .harness-profile.yaml: mask_mode=definitely-not-a-mode     # rc=0
```

`_cmd_set` validates by round-tripping through `load_profile`, which only checks the *cast* — and
`mask_mode` is a `str` field, so any string parses. The garbage persists and resolves straight into
`Settings.mask_mode`. `mask.resolve` then compares `mode == MODE_ALLOW` and takes the `else` branch,
so a typo'd `alow` silently yields **deny** mode.

It fails safe, which is why this is a bug and not an incident. But it fails *silently*: an operator
who typos the mode gets the opposite of what they asked for, with a success message and no warning —
the same shape as the M5 §0.1 bugs (a knob written and then not honored), one layer up. Giving the
field a `choices` tuple closes it at the point of entry, for every enum knob at once, which is why
this milestone is not purely a cleanup.

## 4. Slices (in build order)

### R1 — `FieldSpec` + `FIELD_SPECS`, and the resolver loop

Define the spec type (§5) and the table. Rewrite `resolve_settings`'s hand-listed `field(...)` block
as a loop over `FIELD_SPECS`. Keep `Settings`/`SettingsSources` as explicit frozen dataclasses (see
fork 1) and add a test asserting their fields **exactly** equal the registry's names — the guard that
makes every later slice's derivation safe.

### R2 — Profile I/O derives

`PROFILE_FIELDS`, the four `_PROFILE_*_FIELDS` cast buckets, and `_PROFILE_WRITE_ORDER` all become
derived: a field is in the profile iff its spec sets `profile_key`, its parse cast is its spec's
`cast`, and write order is registry order. `LIVE_FIELDS` derives from `tier`. The M5 exclusions
(`thread_id`, `headless`, `mask_enabled`, `hitl`) become `profile_key=None` **with the reason in the
spec's own comment**, so the rationale sits next to the field instead of in a module-level comment
three hundred lines away.

### R3 — One renderer

Collapse `_config_display_lines` and `format_settings_lines` into one function over the registry,
parameterised by prefix and column width. Both call sites keep their current output.

### R4 — `_handle_config` becomes a dispatch

Replace the if-chain and the `(model, agent, topic)` return with a `LiveContext` the handler mutates
(§5). Each spec's `apply` does its own field's work. This is the slice that makes the function stop
growing a return-tuple slot per field, and it subsumes `_CONFIG_HITL_VALIDATORS` into `choices`.

### R5 — Wizard screens from the registry

`_wizard_security_step`'s custom branch is a hand-written sequence of `_numbered_choice` /
`input()` calls that happens to match the pre-spinup field list. Generate it: a `choices` field
renders as a numbered menu, a free-text field as a prompt with the default in brackets. The posture
shortcuts (`default`/`hardened`) stay hand-written — they are opinions about *combinations*, which
is exactly what a registry can't express and shouldn't try to.

### R6 — The picker (the additive part)

Generalize `_arrow_select` (`cli.py:292`) to take a plain options list instead of an
`InterruptRequest`. M5 declined to reuse it "for a value it was never built to carry"
(`milestone5.md` §4 C5) — correct then, since no field had a value list; with `choices` it now does.
`/config set <field>` with no value opens the picker; Esc or no `prompt_toolkit` falls back to
today's `usage: /config set <field> <value>` error, so the non-TTY and bare-host paths are unchanged.

### R7 — Host-side coverage guard

The launchers stay hand-written (fork 3). Add a test asserting every spec with `profile_key` and
`tier="prespinup"` appears in **both** `run-docker.ps1` and `run-docker.sh`, so the M5 §0.1 bug class
— a profile key nothing consumes — fails CI rather than review. This replaces the ad-hoc
`pids_limit`/`net_jail` string markers M5's fix added to `check-parity`.

## 5. The `FieldSpec` shape

**As shipped** (`harness/config.py`; the original sketch differed in two places, see §0.1):

```python
@dataclass(frozen=True)
class FieldSpec:
    name: str                     # "model", "hitl.autonomy_level" (dotted => nested)
    tier: str                     # "live" | "prespinup"
    env_var: str | None = None    # None => not env-settable, and not walked by the resolver
    profile_key: str | None = None  # None => deliberately not persisted; why, in a comment
    cast: object = str            # str | int | float | _to_bool | _mask_enabled_cast
    default: object = None
    default_factory: object = None  # thread_id's per-run `session-<ts>`
    choices: tuple[str, ...] | None = None   # the picker's reason to exist, and the validator's
    label: str = ""               # wizard/menu prompt text
    wizard: str = "auto"          # "auto" (menu if enum/bool, else text) | "confirm"
    settable: bool = False        # editable via /config set; behaviour in cli._LIVE_APPLIERS
    nullable: bool = False        # a bare `/config set <field>` clears it (topic)
```

`cast` does double duty on purpose: it is the env/CLI cast **and** it selects the profile-file
parse strategy (`_parse_profile_value`), which is strict where the env tier is lenient. Keying the
strict parser off the same attribute is what removed the four `_PROFILE_*_FIELDS` bucket sets —
there is no second list to forget.

`choices` means *exactly these strings are legal*, so it drives validation at every tier as well as
the picker and the wizard menu. It is therefore **only** on `str`-cast fields: a bool carrying
`("off","on")` would reject `DEEPAGENTS_JAIL=1`, which is what both launchers pass. Bool fields get
their off/on menu from `cast is _to_bool` instead. A test asserts the invariant
(`test_registry_entries_are_internally_coherent`).

`_LIVE_APPLIERS[name](ctx, spec, value)` takes a `LiveContext` holding what the REPL already has —
the LangGraph `config` dict, the `CostTrackerMiddleware`, the live `HitlSection`, the
`rebuild_agent` closure, the archive handles, and the mutable `current_model` / `topic` / `new_agent`.
Mutating a context is what lets `_handle_config` stop growing a tuple slot per field.

Dotted names cover the `hitl.*` subfields without a second registry: `resolve_settings` walks only
specs carrying an `env_var`, which excludes `hitl` (a whole-object file tier) and its three
subfields, while `/config`'s display and dispatch include them.

## 6. Forks

1. **Generate `Settings` from the registry, or keep the dataclass and assert coverage?**
   → **Keep the dataclass.** A dynamically built class loses static field types, IDE completion, and
   `dataclasses.fields()` introspection that `cli.py` already depends on, and buys only the
   deletion of a field list a test can guard for free. Assert equality; don't generate.
2. **`hitl.*` in the same registry, or its own?** → **Same**, via dotted names. They are separate
   *files* (a deliberate M5 decision that stands), not separate *concepts* to a user typing
   `/config set`.
3. **Should the launchers read the registry?** → **No.** `run-docker` needs **no host Python** —
   verified: its only `python3` references are inside a `docker run` (the mask scan,
   `run-docker.sh:387` / `.ps1:351`) or inside an error-message string. Every knob it resolves, it
   resolves in pure shell. M5 wrote `-Autonomy`'s YAML edit twice, in bash and PowerShell, rather
   than shell out to the harness for exactly this reason. Reading the registry would mean either a
   host Python dependency the launcher has never had, or a generated JSON manifest that can go
   stale against the source it mirrors. Guard the coverage with a test (R7) instead of removing the
   duplication.
4. **Picker for free-text fields (history / recent values)?** → **Out of scope.** A picker over
   arbitrary prior strings is a different feature with its own storage question.

## 7. Non-goals

- **No precedence, file-format, or on-disk schema change.** `.harness-profile.yaml` and
  `.harness-config.yaml` parse and serialize exactly as they do today.
- **The two config files stay separate.** M5 fork 1's reasoning (the HITL parser is a tested,
  non-trivial grammar not worth risking in a merge) is unaffected by this.
- **`review_triggers` list editing stays file-only.** A list needs add/remove semantics that neither
  `/config set` nor a single-value picker has; `choices` does not help here.
- **No new knobs.** If this milestone adds a config field, the refactor was not behavior-preserving.

## 8. Risk & the safety net

A registry refactor that quietly changes one field's default, cast, or tier is worse than the
duplication it removes — and it would be invisible in review, since the diff is mostly deletions.
The controls:

- **The M5 suite is the oracle.** 604 host tests + 85 image `test_cli` tests pass unchanged
  (counts verified on this branch). A test edit is allowed **only** where the test asserts a
  hand-written constant this milestone derives, or where it pins the pre-fix behavior of §3.1's
  validation gap; any other edited test is a behavior change wearing a refactor's clothes.
- **A resolution snapshot test lands first, before R1**: a matrix over every field × every tier
  (cli / env / profile / default) asserting the resolved value and source tag. M5's
  `test_resolve_settings_precedence_every_field` is most of this already; extend it to pin defaults
  and casts explicitly, so R1–R2 have something to be wrong against.
- **The removable contract is unchanged and still checkable**: no profile file, no flags ⇒ every
  knob resolves as it did pre-M5.

## 9. Invariants (folded in from `milestone5.1_invariants.md` on completion)

The checkable assertions this milestone's build and tests were held to.

M5.1 = one `FieldSpec` table is the single declaration of a knob. The invariants split three ways:
**derivation** (nothing that should be derived is hand-written), **behaviour preservation** (a
refactor whose diff is mostly deletions must change nothing an operator can observe), and the
**one sanctioned behaviour change** (enum validation, §3.1).

The general rule the whole milestone rests on, stated once: **if adding a config field requires
editing anything other than `FIELD_SPECS` and — for a settable field — `cli._LIVE_APPLIERS`, a
derivation invariant below is broken.**

### Derivation (the registry is the only declaration)

1. **`Settings` matches the registry exactly, in order.** `dataclasses.fields(Settings)` and
   `dataclasses.fields(SettingsSources)` both equal the registry's non-dotted names, as an ordered
   tuple. Order is load-bearing, not cosmetic: both display renderers iterate the registry, so a
   silent reorder there reorders `/config`'s output. *(Tested:
   `test_settings_dataclass_exactly_matches_the_registry`.)*
   > Deliberately an assertion, not codegen (§6 fork 1): a dynamically built class loses static
   > field types, IDE completion, and the `dataclasses.fields()` introspection `cli.py` depends on.

2. **The profile file's field set and write order are derived.** A field is in the profile **iff**
   its spec sets `profile_key`; the written order is registry order. The four M5 exclusions
   (`thread_id`, `headless`, `mask_enabled`, `hitl`) are `profile_key=None` with the reason in the
   spec's own comment. *(Tested: `test_profile_field_set_and_write_order_are_derived`.)*

3. **The profile's parse strategy is derived from `cast`.** No `_PROFILE_{BOOL,FLOAT,INT,STR}_FIELDS`
   bucket sets exist. `_parse_profile_value` selects the strict file parser from the spec's own
   `cast`, so the env cast and the file cast cannot disagree about a field's type. *(Tested: the M5
   `test_load_profile_typed_fields` / `test_cmd_set_invalid_value_rolls_back` cases still pass
   unchanged, which is the point — the strictness the file tier had is preserved.)*

4. **`LIVE_FIELDS` is derived from `tier`.** The pre-spinup/in-session split (`milestone5.md` §3's
   table) has one source. *(Tested: `test_live_fields_is_derived_from_tier`, plus M5's
   `test_config_prespinup_fields_match_live_fields_complement`, which still passes.)*

5. **The resolver walks the registry, not a hand-list.** Every spec carrying an `env_var` is
   resolved through CLI > env > profile > default; the only specs without one are `hitl` and its
   three dotted subfields (the whole-object file tier). *(Tested:
   `test_every_scalar_spec_is_walked_by_the_resolver`.)*

6. **`/config`'s four lists are derived.** `_CONFIG_SETTABLE_FIELDS` from `settable`,
   `_CONFIG_PRESPINUP_FIELDS` from `tier`, `_CONFIG_HITL_VALIDATORS` from `choices`,
   `_CONFIG_UNSETTABLE_FIELDS` from `nullable`. *(Tested:
   `test_config_settable_fields_derived_from_registry`,
   `test_config_hitl_validators_derived_from_choices`,
   `test_config_unsettable_fields_derived_from_nullable`.)*

7. **Applier coverage is exact, both directions.** `set(cli._LIVE_APPLIERS)` equals the set of
   `settable` spec names. This is the one hand-maintained pairing M5.1 leaves (§0.1 deviation 1):
   an applier touches the tracker / archive / agent, which `config.py` must not import. Guarding it
   both ways means a settable field with no applier fails CI instead of `KeyError`-ing at dispatch,
   and an applier naming no field is caught as dead code. *(Tested:
   `test_every_settable_field_has_an_applier_and_vice_versa`.)*

8. **One renderer.** `cli._config_display_lines` and `config_cli.format_settings_lines` both call
   `config.format_config_lines`; the only difference between the two presentations is
   `prefix`/`width`/`prespinup_header` passed as arguments. Field set, order, and source tags are
   identical between them. *(Tested: `test_format_config_lines_prefix_and_width_are_parameters`,
   `test_format_settings_lines_is_the_shared_renderer`.)*

9. **The wizard's custom screen is generated.** It asks about exactly
   `WIZARD_PRESPINUP_SPECS` — every `tier="prespinup"` spec with a `profile_key` — in registry
   order. Adding such a spec adds a wizard question with no edit to `_wizard_security_step`.
   *(Tested: `test_custom_posture_asks_every_persisted_prespinup_knob` — which adds a spec at
   runtime and asserts the screen follows — plus `test_wizard_prespinup_specs_are_...`.)*
   > The posture shortcuts (`default`/`hardened`) stay hand-written on purpose: they are opinions
   > about *combinations* of knobs, which a per-field registry cannot express and shouldn't try to.

10. **Every persisted pre-spinup key is read by both launchers.** A `profile_key` that no launcher
    consumes is precisely the M5 §0.1 bug class (the wizard writes `cpus:`/`net_jail:` and
    `docker run` never sees them). The launchers stay hand-written (§6 fork 3 — `run-docker` needs
    no host Python), so the duplication is *guarded*, not removed. *(Tested:
    `test_prespinup_profile_keys_are_consumed_by_both_launchers`; skips in the image, where
    `scripts/` is not present, and therefore runs in the host tier CI actually uses.)*

### Behaviour preservation (the refactor changes nothing observable)

11. **Resolved values, precedence, and source tags are unchanged for every field × every tier.**
    M5's `test_resolve_settings_precedence_every_field` and
    `test_resolve_settings_removable_contract_matches_pre_m5_defaults` pass **unedited**.

12. **On-disk formats are unchanged.** `.harness-profile.yaml` and `.harness-config.yaml` parse and
    serialize byte-identically — same key order, same header comment block, same BOM tolerance,
    same in-place-write fallback. *(Tested: M5's `test_save_profile_*` and
    `test_example_profile_file_parses_cleanly`, unedited.)*

13. **Every stderr/stdout string `/config` and the wizard emit is unchanged.** Including the
    `[harness] ` prefix and column widths, the pre-spinup header, the per-field confirmation lines,
    and each wizard prompt's exact text. *(Tested: the whole M5 `test_cli` / `test_config_cli`
    surface, unedited — notably `test_wizard_security_step_custom_collects_all_fields`, whose input
    sequence only works if the generated prompts match the hand-written ones one for one.)*

14. **The removable contract still holds.** No profile file and no flags ⇒ every knob resolves as it
    did pre-M5. *(Tested: `test_resolve_settings_removable_contract_matches_pre_m5_defaults`.)*

15. **The oracle rule.** A test edit is legitimate **only** where it asserted a hand-written
    constant this milestone derives, or where it pinned §3.1's pre-fix behaviour. Any other edited
    test is a behaviour change wearing a refactor's clothes. *(Result: **zero** existing tests were
    edited — see `milestone5.1.md` §0.2.)*

### The one sanctioned behaviour change — enum validation (§3.1)

16. **An enum knob rejects an invalid value at every point of entry.** Profile file (hand-edited or
    written by `harness config set`), env var, and CLI all raise `SystemExit` naming the field and
    its legal values, instead of persisting a string that silently resolves to the opposite mode.
    *(Tested: `test_profile_rejects_an_invalid_enum_value`, `test_env_rejects_an_invalid_enum_value`,
    `test_cli_rejects_an_invalid_enum_value`, `test_cmd_set_rejects_an_invalid_enum_value`.)*

17. **A rejected write leaves no trace.** `harness config set <enum> <garbage>` exits 1 and either
    removes the file it would have created or restores the prior bytes exactly. Stronger for enums
    than for a bad cast: `save_profile` rejects an out-of-`choices` value **before writing**, so no
    writer — the wizard, `/config save`, or `harness config set` — can produce a file
    `load_profile` would then refuse. *(Tested: `test_cmd_set_rejects_an_invalid_enum_value`,
    `test_cmd_set_invalid_enum_rolls_back_a_prior_profile`,
    `test_save_profile_rejects_an_invalid_enum_before_writing`.)*

18. **Every declared choice is accepted.** The validator cannot be tightened past the values the
    registry itself declares legal. *(Tested: `test_profile_accepts_every_declared_choice`,
    `test_cmd_set_accepts_every_declared_choice`.)*

19. **Validation never reaches a bool knob.** `DEEPAGENTS_JAIL` still accepts `1`, `true`, `TRUE`,
    `yes`, `on` — the spellings the launchers, `.env` files, and the wizard respectively produce.
    This is the specific way a plausible "fix" for §3.1 (give every knob a `choices` tuple) would
    break the harness, so it is pinned. *(Tested:
    `test_bool_knobs_still_accept_every_launcher_spelling`, and structurally by
    `test_registry_entries_are_internally_coherent`, which requires `cast is str` wherever `choices`
    is set.)*

### The picker (R6, the additive UI affordance)

20. **`/config set <field>` with no value opens a picker for any field carrying `choices`**, seeded
    with exactly those choices and the field's `label` as the header. *(Tested:
    `test_config_set_bare_enum_field_opens_the_picker`.)*

21. **The picker is never the only path.** Esc/Ctrl-C, a host without `prompt_toolkit`, and a
    non-TTY all yield `None`, which falls through to the identical
    `usage: /config set <field> <value>` error the typed path has always printed. *(Tested:
    `test_config_set_bare_enum_field_falls_back_when_picker_declines`.)*

22. **A free-text field does not open a picker.** A picker over arbitrary prior strings is a
    different feature with its own storage question (§6 fork 4), so `/config set model` with no
    value stays a usage error. *(Tested:
    `test_config_set_bare_free_text_field_does_not_open_the_picker`.)*

23. **`_arrow_select` still serves the HITL `choose` path.** Widening it from an `InterruptRequest`
    to a plain options list must not break M3 S6 PR-b; the caller passes `req.options`. *(Tested:
    `test_arrow_select_takes_a_plain_options_list` for the shape, and the unedited `test_hitl`
    channel cases for the behaviour.)*

### Non-goals, restated as invariants

24. **No new knobs.** If this milestone adds a config field, the refactor was not
    behaviour-preserving. *(Checked by invariant 1: `Settings` gained no field.)*

25. **The two config files stay separate.** `.harness-config.yaml` (HITL) and
    `.harness-profile.yaml` (everything else) are one *registry*, not one *file* — M5 fork 1's
    reasoning (the HITL parser is a tested, non-trivial grammar not worth risking in a merge) is
    unaffected.

26. **`review_triggers` list editing stays file-only.** A list needs add/remove semantics neither
    `/config set` nor a single-value picker has; `choices` does not help, so no spec claims it does.
