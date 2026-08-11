# Milestone 5.1 — Config Field Registry

## 0. Build status

**Planned — docs only.** No code. Follows `milestone5.md` (Unified Config Surface, complete), which
this refactors without changing any resolved value. Named `5.1` on the `milestone4.1.md` precedent:
a follow-on slice of a shipped milestone, not a new capability.

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
  byte-identical — this is a refactor plus one additive UI affordance (§4 R6).
- `/config set <field>` with **no value** opens an arrow-key picker for any field carrying
  `choices`; free-text fields keep today's typed path. `/config set <field> <value>` is unchanged.
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
| 1 | `Settings` dataclass field | `config.py:319` |
| 2 | `SettingsSources` dataclass field (same name, `str` type) | `config.py:341` |
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

And the thing that blocks the menu: `_CONFIG_HITL_VALIDATORS` (`cli.py:687`) is the *only* place any
field's valid values are written down, and it covers three of them. `mask_mode` has exactly two legal
values; nothing in the code knows that.

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

Sketch, not final:

```python
@dataclass(frozen=True)
class FieldSpec:
    name: str                     # "model", "hitl.autonomy_level" (dotted => nested)
    tier: str                     # "live" | "prespinup"
    env_var: str | None           # None => not env-settable (hitl.* subfields)
    profile_key: str | None       # None => deliberately not persisted; say why in a comment
    cast: Callable = str          # str | int | float | _to_bool | _mask_enabled_cast
    default: object = None
    choices: tuple[str, ...] | None = None   # the picker's reason to exist
    label: str = ""               # wizard/menu prompt text
    apply: Callable | None = None # live mutation; None => not settable in-session
```

`apply` takes a `LiveContext` holding what the REPL already has — the LangGraph `config` dict, the
`CostTrackerMiddleware`, the live `HitlSection`, the `rebuild_agent` closure, and the mutable
`current_model` / `topic`. Mutating a context is what lets `_handle_config` stop returning a tuple
that grows with each field.

Dotted names cover the `hitl.*` subfields without a second registry: `resolve_settings` skips any
spec whose name contains a dot (they live in `.harness-config.yaml`, resolved as a whole object),
while `/config`'s display and dispatch include them.

## 6. Forks

1. **Generate `Settings` from the registry, or keep the dataclass and assert coverage?**
   → **Keep the dataclass.** A dynamically built class loses static field types, IDE completion, and
   `dataclasses.fields()` introspection that `cli.py` already depends on, and buys only the
   deletion of a field list a test can guard for free. Assert equality; don't generate.
2. **`hitl.*` in the same registry, or its own?** → **Same**, via dotted names. They are separate
   *files* (a deliberate M5 decision that stands), not separate *concepts* to a user typing
   `/config set`.
3. **Should the launchers read the registry?** → **No.** `run-docker` is deliberately
   Python-independent — M5 wrote `-Autonomy`'s YAML edit in bash and PowerShell rather than shell
   out to the harness for exactly this reason. Emitting a build-time JSON manifest for the shell to
   parse trades a hand-written call for a generated-artifact staleness problem. Guard the coverage
   with a test (R7) instead of removing the duplication.
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

- **The M5 suite is the oracle.** 604 host tests + 85 image `test_cli` tests pass unchanged. A test
  edit is allowed **only** where the test asserts a hand-written constant this milestone derives;
  any other edited test is a behavior change wearing a refactor's clothes.
- **A resolution snapshot test lands first, before R1**: a matrix over every field × every tier
  (cli / env / profile / default) asserting the resolved value and source tag. M5's
  `test_resolve_settings_precedence_every_field` is most of this already; extend it to pin defaults
  and casts explicitly, so R1–R2 have something to be wrong against.
- **The removable contract is unchanged and still checkable**: no profile file, no flags ⇒ every
  knob resolves as it did pre-M5.
