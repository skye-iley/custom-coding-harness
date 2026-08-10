# Milestone 5 — Invariants

Checkable-assertion companion to `milestone5.md` (scope/DoD/why) and `milestone5_spec.md`
(implementation). Drives the test plan (`milestone5_spec.md` §13). Folds into `milestone5.md` on
completion, per the repo's milestone lifecycle (`docs/README.md`).

1. **Precedence holds per field.** For every scalar `Settings` field, `resolve_settings(cli=X,
   env=Y, profile_path=Z)` returns the CLI value when set, else the env value when set, else the
   profile value when set, else the built-in default — in that order, never any other. Covered by
   `tests/test_config.py` with all four tiers populated simultaneously per field.
2. **Removable contract: no profile, no new flags ⇒ byte-for-byte unchanged.** With no
   `.harness-profile.yaml` present and no new CLI flags passed, `resolve_settings()`'s live-field
   values and sources equal what today's `_env_defaults()` + `hitl_config.load_config()` produce
   (every source is `"env"` or `"default"`, never `"profile"`). `parse_args()` defaults are
   unchanged after the re-plumb (regression coverage in `tests/test_cli.py`).
3. **`HitlSection` is a pure rename.** Field names, defaults, `gated_hooks()`,
   `system_interrupt_enabled()`, and every existing `.harness-config.yaml` parse/match test pass
   unchanged against `HitlSection` (only the import name changes at call sites).
4. **`LIVE_FIELDS` is the single source of truth for the pre-spinup/in-session split.** The set
   `{"model", "thread_id", "topic", "max_cost", "max_tokens", "hitl"}` is defined once in
   `config.py` and is what `/config`'s editor, `harness config`'s wizard, and `harness doctor`'s
   report all filter on — no second hardcoded list anywhere that could drift from
   `milestone5.md` §3's table.
5. **Unknown profile key fails loud.** A `.harness-profile.yaml` with a top-level key not in
   `Settings`'s field set raises `SystemExit` at load time — same policy as
   `.harness-config.yaml`'s unknown-key rejection. A typo never silently no-ops.
6. **`save_profile` merges, never overwrites.** Saving `{model: "x"}` when the on-disk profile
   already has `jail: true` leaves `jail: true` intact after the write — read-modify-write, not
   replace.
7. **`/config set` only accepts `LIVE_FIELDS`.** Attempting to set a pre-spinup field (e.g.
   `mask_mode`, `jail`, `cpus`) via the in-session `/config set` is refused with a message pointing
   at `harness config` — it is never silently accepted and never silently no-op'd.
8. **`/config` mid-turn is refused, not queued or silently dropped.** Issuing `/config` (any
   subcommand) while a turn is in flight prints `[harness] /config unavailable mid-turn` and makes
   no state change — same pattern as other prompt-only REPL commands.
9. **`/config set model <spec>` re-validates credentials.** A model switch that would fail
   `validate_credentials` at launch fails the same way via `/config set model` — no path where a
   live model switch silently degrades to a broken model.
10. **Secrets never enter `Settings` or the profile file.** No `Settings` field, `SettingsSources`
    field, or `.harness-profile.yaml` key ever holds an API key or other credential — those stay
    `.env`-only (existing hard rule, unchanged). `harness doctor`'s credential check keeps reading
    raw env directly, never through `Settings`.
11. **Host-side resolution is dependency-free.** `run-docker.{ps1,sh}` resolve every pre-spinup
    knob (mask mode, jail, AppArmor, caps, NetJail, `MAP_HOST_USER`) via `lib/config.{ps1,sh}`
    without requiring the harness venv or Python — only `harness config`'s wizard needs the venv.
12. **`.ps1`/`.sh` host resolution parity.** For a fixture profile + env combo,
    `lib/config.ps1`'s `Resolve-HostSetting` and `lib/config.sh`'s equivalent return identical
    values for every knob — asserted by `scripts/check-parity.{ps1,sh}`.
13. **Profile file is gitignored and never baked into the image.** `.harness-profile.yaml` is in
    `.gitignore` and absent from the Dockerfile's `COPY` list, same treatment as `.env` and
    `.harness-config.yaml`.
14. **`harness doctor` reports resolved config, not raw env.** Doctor's new report block is built
    from `resolve_settings()` with no `cli=` override, so its output reflects what an *unflagged*
    run would actually do — not a grep of `os.environ`.
15. **Full removable contract.** Deleting `config_cli.py`, the profile-file branch inside
    `config.py`'s resolvers, and `lib/config.{ps1,sh}` (reverting `run-docker` call sites to direct
    env reads) restores byte-for-byte pre-milestone-5 behavior. `HitlSection`/
    `.harness-config.yaml` semantics (presence-of-file = HITL on) are untouched by this milestone.
