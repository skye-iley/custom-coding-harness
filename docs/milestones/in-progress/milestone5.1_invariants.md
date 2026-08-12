# Milestone 5.1 — Invariants

> Test-facing companion to `milestone5.1.md` (same folder). Kept **separate** while M5.1 is
> in-progress so these checkable properties drive testing without the planning prose around them.
> On completion this folds into `milestone5.1.md` as a section and the standalone file is dropped
> (see the milestone lifecycle in `docs/README.md`).

M5.1 = one `FieldSpec` table is the single declaration of a knob. The invariants split three ways:
**derivation** (nothing that should be derived is hand-written), **behaviour preservation** (a
refactor whose diff is mostly deletions must change nothing an operator can observe), and the
**one sanctioned behaviour change** (enum validation, §3.1).

The general rule the whole milestone rests on, stated once: **if adding a config field requires
editing anything other than `FIELD_SPECS` and — for a settable field — `cli._LIVE_APPLIERS`, a
derivation invariant below is broken.**

## Derivation (the registry is the only declaration)

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

## Behaviour preservation (the refactor changes nothing observable)

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

## The one sanctioned behaviour change — enum validation (§3.1)

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

## The picker (R6, the additive UI affordance)

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

## Non-goals, restated as invariants

24. **No new knobs.** If this milestone adds a config field, the refactor was not
    behaviour-preserving. *(Checked by invariant 1: `Settings` gained no field.)*

25. **The two config files stay separate.** `.harness-config.yaml` (HITL) and
    `.harness-profile.yaml` (everything else) are one *registry*, not one *file* — M5 fork 1's
    reasoning (the HITL parser is a tested, non-trivial grammar not worth risking in a merge) is
    unaffected.

26. **`review_triggers` list editing stays file-only.** A list needs add/remove semantics neither
    `/config set` nor a single-value picker has; `choices` does not help, so no spec claims it does.
