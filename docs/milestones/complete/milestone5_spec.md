# Milestone 5 — Unified Config Surface: Full Spec

Companion to `milestone5.md` (scope/DoD/rationale, including §4's per-slice **as-built** notes and
§8's invariants — read that first). This file is the implementation-level spec this milestone was
built from: module layout, data shapes, precedence algorithm, file formats, CLI/REPL surfaces, and
the concrete diff to existing modules.

Written during planning, before code existed, so it reads in the past-planning tense throughout
("will", "gets", proposed line numbers). Three details it describes did not ship as written —
`milestone5.md` §4 (C3, C5, C7) has the as-built account and why; treat this file as the design
rationale and everything else as accurate. Line-number references below may also have drifted as
code moved; per `docs/README.md`, the code wins on any mismatch.

## 1. Resolved forks

- **Fork 1 (module absorption):** resolved as *absorb at the Python API level, not the file-format
  level*. One `Settings` object is the single thing code reads from; the on-disk **files** stay
  two (§4) because the HITL parser is already a tested, non-trivial grammar (`review_triggers`
  nested blocks) not worth risking in a rewrite. `harness/config.py` keeps its name and becomes the
  resolver module — the existing 5-field HITL `Config` dataclass is renamed `HitlSection` and
  nests inside the new `Settings` dataclass as `Settings.hitl`.
- **Spec location:** this file, alongside `milestone5.md`.
- **Fork 2 (wizard ergonomics)** and **fork 3 (file-ownership rule)**: resolved inline below (§4,
  §7) rather than left open.

## 2. Module layout

```
harness/config.py       # RENAMED IN PLACE: resolver + HitlSection (was: HITL-only Config)
harness/config_cli.py   # NEW: `harness config` / `harness config security` keyless subcommands
                         #      (wizard prompts, save-flow) — separate file so config.py stays
                         #      importable by anything without pulling in prompt_toolkit/argparse
                         #      wizard code (same split cost.py/sync_models.py already use).
```

No new top-level package. `config.py` stays dependency-light (stdlib only, like today) so
`hitl.py`, `cli.py`, and `doctor.py` can keep importing it cheaply; the wizard's interactive bits
(arrow-select, prompts) live in `config_cli.py`, which *may* import `prompt_toolkit` lazily the
same way `cli._arrow_select` does.

## 3. Data model (`harness/config.py`)

```python
@dataclass(frozen=True)
class HitlSection:                    # was: Config (renamed, fields unchanged)
    autonomy_level: str = "guided"
    review_triggers: tuple[Trigger, ...] = ()
    interruption_policy: str = "blocking"
    on_deny: str = "halt"
    system_interrupts: dict = field(default_factory=lambda: {k: True for k in SYSTEM_INTERRUPT_KEYS})
    # .gated_hooks() / .system_interrupt_enabled() methods carry over unchanged.

@dataclass(frozen=True)
class Settings:
    # --- in-session-live (can change via /config without a restart) ---
    model: str | None = None                  # None => providers.choose_model() auto-selects
    thread_id: str | None = None               # None => fresh session-<ts>, as today
    topic: str | None = None
    max_cost: float | None = None
    max_tokens: int | None = None
    hitl: HitlSection | None = None            # None => HITL off (removable contract, unchanged)

    # --- pre-spinup-only (fixed at container start; shown read-only in /config) ---
    headless: bool = False
    mask_enabled: bool = True
    mask_mode: str = "deny"                    # "deny" | "allow"
    jail: bool = False
    jail_apparmor: str | None = None            # None => auto (see deepagent-image/CLAUDE.md §AppArmor)
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: str = "512"
    net_jail: bool = False

@dataclass(frozen=True)
class SettingsSources:
    """Same field names as Settings; each value is one of "cli" | "env" | "profile" | "default".
    Powers /config's provenance display and `harness doctor`'s resolved-config report."""
    # ... one str field per Settings field ...

LIVE_FIELDS = frozenset({"model", "thread_id", "topic", "max_cost", "max_tokens", "hitl"})
```

`LIVE_FIELDS` is the single source of truth for the pre-spinup/in-session split in
`milestone5.md` §3's table — `/config`'s editor and `harness doctor`'s report both filter on it,
so the table in the milestone doc and the code can't drift apart silently.

## 4. Precedence & resolution

```
CLI flag  >  env var (os.environ — shell-exported or dotenv-loaded from .env, indistinguishable)
          >  profile file (.harness-profile.yaml)  >  built-in default
```

```python
def resolve_settings(
    *,
    cli: argparse.Namespace | dict | None = None,
    env: Mapping[str, str] = os.environ,
    profile_path: Path | None = None,      # default: Path.cwd() / PROFILE_NAME
    hitl_path: Path | None = None,         # default: Path.cwd() / CONFIG_NAME (unchanged)
) -> tuple[Settings, SettingsSources]:
```

Per-field resolution is one small helper reused for every scalar field:

```python
def _resolve(cli_val, env_name, profile_val, default, *, cast=str):
    if cli_val is not None: return cast(cli_val), "cli"
    raw = env.get(env_name)
    if raw: return cast(raw), "env"
    if profile_val is not None: return cast(profile_val), "profile"
    return default, "default"
```

`hitl` resolves differently (whole-object, not per-field): `hitl_path` is loaded via the
**existing, unchanged** `load_config`/`parse_config` — presence-of-file still means HITL-on,
untouched by this milestone. `Settings.hitl` is just that result relocated onto the composed
object; `SettingsSources.hitl` is `"profile"` when the file was present (there is no env/CLI
override path for whole-config swap-in — individual HITL fields (`autonomy_level` etc.) *do* get
CLI/env overrides layered on top in a later pass, described in §6).

Two on-disk files, one Python object — deliberate (§1). Consumers (`cli.py`, `hitl.py`,
`doctor.py`) import `Settings`/`resolve_settings` from `config.py`; nothing outside `config.py`
parses either file directly anymore (`hitl.py`'s `from harness.config import Config,
match_triggers` becomes `from harness.config import HitlSection, match_triggers`, one identifier
rename, call sites unchanged since `HitlSection` has the same shape `Config` had).

## 5. Profile file: `.harness-profile.yaml`

New, gitignored (join `.env` / `.harness-config.yaml` in `.gitignore`), `.harness-profile.yaml.example`
checked in under `project/`. Flat scalars only — reuses `config.py`'s existing tiny parser
primitives (`_scalar`, `_parse_bool`, `_strip_comment`, `_split_top_commas`) rather than a new
parser; no nested blocks needed since HITL detail stays in its own file (§4).

```yaml
# .harness-profile.yaml — persisted defaults for knobs not already in .env.
# Written by `harness config` / `harness config security` (--save) or `/config save`.
# Any key you don't set here falls through to env / .env / built-in default (see
# harness/config.py precedence). Delete this file to fall back to that chain entirely
# (removable contract — identical to no profile ever having existed).

model:                 # e.g. openai:gpt-5.5 — same spec as DEEPAGENTS_MODEL / --model
topic:
max_cost:
max_tokens:

mask_mode: deny        # deny | allow
jail: false
jail_apparmor:          # unset = auto; "unconfined"; or a host-loaded profile name
cpus: "2"
memory: 4g
pids_limit: "512"
net_jail: false
```

Unknown top-level keys fail loud (`SystemExit`), same policy as `.harness-config.yaml` — a typo'd
key must not silently no-op.

**Not in the profile:** `thread_id` (inherently per-run — resuming a specific thread is a
deliberate one-off, not a saved default), `headless` (a mode you pick per-invocation, not a
standing preference), `mask_enabled` (`DEEPAGENTS_MASK=0` is a debugging escape hatch documented
as env-only; promoting it to a saveable default risks someone saving "masking off" and forgetting).

## 6. `harness/config.py` public API (consumer-facing)

```python
def resolve_settings(*, cli=None, env=os.environ, profile_path=None, hitl_path=None) -> tuple[Settings, SettingsSources]: ...
def save_profile(path: Path, values: dict) -> None: ...   # merges into existing file, writes atomically
def load_profile(path: Path) -> dict: ...                  # raw dict, pre-Settings (used by save + resolve)
PROFILE_NAME = ".harness-profile.yaml"
CONFIG_NAME = ".harness-config.yaml"   # unchanged, re-exported for call-site convenience
```

`save_profile` merges (read-modify-write) rather than overwrites, so `/config save` after editing
only `model` doesn't clobber a `jail: true` a wizard set earlier. Comments in the file are not
preserved across a save (same limitation the tiny parser already accepts for `.harness-config.yaml`
— no round-trip requirement was ever a design goal there).

## 7. CLI flags

### 7a. Container side (`harness/cli.py:parse_args`)

Existing flags (`--model`, `--workspace`, `--thread-id`, `--topic`, `--stream`, `--headless`,
`--max-cost`, `--max-tokens`) are re-plumbed to flow through `resolve_settings(cli=args, ...)`
instead of `_env_defaults()` — **behavior unchanged**, this is a re-plumb not a new surface.
`_env_defaults()` is deleted; its logic moves into `config.py`'s per-field resolution.

No new container-side flags are needed — everything else in `Settings` is pre-spinup and has no
meaning to change after the container is already running via a flag (that's what `/config` and
the host wizard are for).

### 7b. Host side (`run-docker.{ps1,sh}`)

New params/env, added to the existing `param()`/arg-parsing block, mirroring the launcher-env
table convention:

| Env (`.sh`) | `.ps1` param | Default | Purpose |
|---|---|---|---|
| `DEEPAGENTS_MODEL` *(already forwarded via --env-file; no new flag needed for the container)* | `-Model` | unset | Also write/override the **host-side** resolution so the mask-scan pre-flight and any future host-side model-aware logic see it without requiring `.env` to be edited. Forwarded into the container the same way `-e DEEPAGENTS_MODEL=...` already could be. |
| `MASK_MODE` | `-MaskMode` | from profile/.env | deny\|allow — currently `.env`/env-only per the existing scrape (`run-docker.ps1:298-307`); promoted to a first-class flag. |
| `JAIL` | `-Jail` | from profile/.env | On/off — currently `DEEPAGENTS_JAIL` env/.env-only (`run-docker.ps1:359-363`); promoted to a flag. |
| `JAIL_APPARMOR` | `-JailApparmor` | from profile/.env | Mirrors `DEEPAGENTS_JAIL_APPARMOR`. |

`-Cpus`/`-Memory`/`-PidsLimit`/`-NetJail`/`-Ephemeral`/`-SaveWorkspace` already exist and are
unchanged — they gain profile-file fallback (§7c) but no new flag surface.

### 7c. Shared host-side resolution helper

`run-docker.{ps1,sh}` currently duplicate the CLI-flag > env > `.env`-scrape pattern per variable
(see `run-docker.ps1:278-334` for mask, `:359-388` for jail/AppArmor) — three near-identical
blocks. This milestone factors that into one function per launcher, used for every pre-spinup
knob, profile file added as the new lowest-before-default tier:

```powershell
# scripts/lib/config.ps1 (NEW — same pattern as the existing lib/hostmap.sh split)
function Resolve-HostSetting {
    param([string]$Value, [string]$EnvVarName, [string]$ProfileKey, [string]$Default)
    if ($Value) { return $Value }                                    # explicit -Flag wins
    if ($env:$EnvVarName) { return $env:$EnvVarName }                 # host env
    $envFileHit = Select-String -Path $EnvFile -Pattern "^\s*$EnvVarName\s*=" ... | Select-Object -Last 1
    if ($envFileHit) { return <parsed value> }                        # .env
    $profileHit = <scrape .harness-profile.yaml for $ProfileKey>      # profile file (NEW tier)
    if ($profileHit) { return $profileHit }
    return $Default
}
```

Bash gets the equivalent function in `scripts/lib/config.sh`. Profile-file scraping is a regex
line-match against `.harness-profile.yaml` (same technique already used for `.env` — no YAML
parser needed on the host, since the file is deliberately flat scalars only, §5). This keeps
`run-docker` dependency-free (no requirement that Python/the harness venv be available on the
host just to launch — only `harness config`'s wizard needs the venv, §9).

**Test:** `scripts/check-parity.ps1`/`.sh` (existing parity checker) gets a new section asserting
both `lib/config.ps1` and `lib/config.sh` resolve the same value for a fixture profile + env combo.

## 8. In-session `/config` REPL command

Added to `_SLASH_META_BASE` in `cli.py` (always available, unlike `/recall`/`/topic` which gate on
features). Syntax:

```
/config                       # print resolved Settings, one line per field, source-tagged
/config set <field> <value>   # edit ONE live field (model/thread_id/topic/max_cost/max_tokens/
                               #   hitl.autonomy_level/hitl.on_deny/hitl.interruption_policy)
/config save                  # write current in-session Settings' live fields to the profile
                               #   (pre-spinup fields are NOT touched by an in-session save —
                               #   they didn't change this session, nothing to persist)
```

Example transcript:

```
you> /config
[harness] model         = openai:gpt-5.5           (env: DEEPAGENTS_MODEL)
[harness] thread_id     = session-20260810-141200   (default)
[harness] topic         = (unset)                   (default)
[harness] max_cost      = (unset)                   (default)
[harness] max_tokens    = (unset)                   (default)
[harness] hitl.autonomy_level = guided              (profile: .harness-config.yaml)
[harness] --- pre-spinup (fixed for this container; edit via `harness config` before next launch) ---
[harness] mask_mode     = deny
[harness] jail          = off
[harness] cpus          = 2

you> /config set hitl.autonomy_level strict
[harness] hitl.autonomy_level: guided -> strict (this session only; /config save to persist)

you> /config save
[harness] wrote .harness-profile.yaml: hitl.autonomy_level=strict
```

`/config set model <spec>` rebuilds the agent via the existing `build_agent` path (same call
`main()` makes at startup) — `validate_credentials` runs again so a missing key fails the same way
a bad `--model` does at launch, not mid-turn. Mid-turn `/config` (while a turn is in flight) is
refused with `[harness] /config unavailable mid-turn` — same pattern as other REPL commands that
require the idle prompt.

`/config set hitl.*` mutates the live `HitlSection` object `PauseMiddleware` already holds a
reference to (it's read per-tool-call, not cached at construction), so `autonomy_level`/`on_deny`
changes apply to the *next* gated call with no rebuild needed.

## 9. Host-side wizard: `harness config` / `harness config security`

New keyless subcommands wired into `cli.dispatch` (`argv[0] == "config"`, same pattern as
`threads`/`past`/`doctor`), implemented in `harness/config_cli.py::config_main(argv)`. Uses
`argparse` with subparsers, mirroring `memadmin.py`'s `--yes`-guard convention for anything that
overwrites the profile non-interactively.

```
harness config                      # full interactive wizard (model + security posture), then
                                     #   prompts to save; --save skips the prompt and saves.
harness config show                 # print resolved Settings + sources, no prompts (same output
                                     #   /config produces, for use without a running container)
harness config set <field> <value>  # one-shot, non-interactive: `harness config set jail true`
harness config security             # narrower wizard: mask mode, .agentignore quick-edit,
                                     #   jail/AppArmor, resource caps, NetJail lists
```

Wizard flow (`harness config`, interactive TTY, `prompt_toolkit` available — reuses
`cli._arrow_select`'s menu widget, factored out to `config_cli._menu(options)` since it no longer
needs an `InterruptRequest`):

```
$ harness config
Model — pick a provider (keys detected: OPENAI_API_KEY, GOOGLE_API_KEY):
❯ openai:gpt-5.5
  google_genai:gemini-3.5-pro
  (keep current: unset — auto-select)

Security posture:
❯ default (mask on, jail off — matches current .env)
  hardened (mask on, jail on — requires the seccomp profile; see doctor)
  custom (answer each knob)

HITL preset:
❯ off (no .harness-config.yaml)
  guided (approve PR + flagged tool calls)
  strict (approve everything)

Save to .harness-profile.yaml? [Y/n]
```

No `prompt_toolkit` / non-TTY (e.g. piped into a script): falls back to numbered-choice `input()`
prompts, same degrade pattern `_make_prompt_session` already uses for the REPL.

`harness config security` asks only the security-tier questions (mask mode, jail, AppArmor,
caps, NetJail) — literally the tail of the same wizard function with the model/HITL screens
skipped, not a separate implementation, per `milestone5.md` §4 C7's framing ("already the same
program, narrower entry point").

**`.agentignore` quick-edit**: `harness config security` offers "add a path to
`.agentignore`" / "add a floor entry" as menu actions that append to the **workspace's**
`.agentignore` (not the profile — that file's format/location is unchanged, M4-owned). This
subcommand is a convenience wrapper, not a new masking mechanism.

## 10. `harness doctor` integration

`doctor_main` gains one new report block before the existing checks, built from
`resolve_settings()` with no `cli=` override (doctor reflects what an *unflagged* run would do):

```
[doctor] [info] resolved config: model=openai:gpt-5.5 (env), mask_mode=deny (default),
                 jail=off (profile: .harness-profile.yaml), hitl=guided (.harness-config.yaml present)
```

This replaces doctor's current raw `os.environ.get(...)` reads for the fields `Settings` now
covers (credentials check in §-quoted `doctor.py:112-121` stays raw-env, since API keys are
deliberately outside `Settings`/the profile — secrets stay `.env`-only per the repo's hard rule).

## 11. Files touched (concrete diff surface)

| File | Change |
|---|---|
| `harness/config.py` | `Config` → `HitlSection` (rename only); add `Settings`, `SettingsSources`, `resolve_settings`, `save_profile`, `load_profile`, `PROFILE_NAME`. |
| `harness/config_cli.py` | **NEW** — `config_main`, wizard prompts, `_menu` (factored from `cli._arrow_select`). |
| `harness/cli.py` | `parse_args` routes through `resolve_settings`; `_env_defaults` deleted; `hitl_config.load_config(...)` → reads `Settings.hitl` off the resolved object instead of a separate call; `dispatch` gains `argv[0] == "config"`; `/config` REPL command + `_SLASH_META_BASE` entry. |
| `harness/hitl.py` | Import rename `Config` → `HitlSection` (one line, `harness/hitl.py:35`). |
| `harness/doctor.py` | New resolved-config report block (§10). |
| `scripts/run-docker.ps1` / `.sh` | New `-Model`/`-MaskMode`/`-Jail`/`-JailApparmor` params; existing per-var scrape blocks replaced by calls into `lib/config.ps1`/`.sh` (§7c). |
| `scripts/lib/config.ps1` / `.sh` | **NEW** — `Resolve-HostSetting` / equivalent (§7c). |
| `scripts/check-parity.ps1` / `.sh` | New parity section for `lib/config.*`. |
| `project/.harness-profile.yaml.example` | **NEW** (§5). |
| `project/.gitignore` | Add `.harness-profile.yaml`. |
| `deepagent-image/ENV_VARS.md` | Note the profile file as the persisted alternative to hand-editing `.env`. |
| `deepagent-image/CLAUDE.md` | New "Unified config" section (module list, precedence, `/config`, `harness config`). |
| `tests/test_config.py` | Extend for `Settings`/`resolve_settings`/`save_profile`; existing `HitlSection` (ex-`Config`) parse tests unchanged in substance. |
| `tests/test_cli.py` | `/config` command tests (host-runnable subset: `_completion_candidates`, slash menu entry); `parse_args` still asserts the same defaults it does today (regression coverage for the re-plumb). |
| `tests/test_config_cli.py` | **NEW** — wizard prompt sequencing with a fake input/menu (host-runnable, no real TTY/prompt_toolkit needed, same fake-channel pattern `test_hitl.py` uses for `ReplChannel`). |

## 12. Removable contract (unchanged from milestone5.md, restated precisely)

- No `.harness-profile.yaml` present, no new CLI flags passed ⇒ `resolve_settings()` returns
  exactly what `_env_defaults()` + `hitl_config.load_config()` return today — every field's
  `source` is `"env"` or `"default"`, never `"profile"`.
- Delete `config_cli.py`, the profile-file branch inside `config.py`'s resolvers, and the
  `run-docker` `lib/config.*` helpers (reverting its call sites to direct env-var reads) and the
  harness is byte-for-byte pre-milestone-5.
- `HitlSection`/`.harness-config.yaml` semantics (presence-of-file = HITL on) are **untouched** —
  this milestone does not change M3's removable contract, only where the dataclass lives.

## 13. Test plan additions

- `tests/test_config.py`: precedence order for every `Settings` field (cli > env > profile >
  default) via `resolve_settings(cli=..., env=fake_env, profile_path=tmp_profile)`; unknown-key
  rejection in the profile parser; `save_profile` merge-not-overwrite behavior; `HitlSection`
  rename is a pure rename — existing parse/match tests carry over unchanged (just the import).
- `tests/test_config_cli.py` (new): wizard question sequencing against a fake menu/input channel
  (no real terminal); `harness config set field value` one-shot path; `harness config show`
  output format; `--save` vs. interactive-confirm paths.
- `tests/test_cli.py`: `/config`, `/config set`, `/config save` REPL command parsing +
  slash-completion menu entries (host-runnable pure-parsing parts); `parse_args` defaults
  regression (unchanged behavior after the re-plumb).
- `tests/test_doctor.py` (if not already present) or extend the doctor test file: resolved-config
  report line appears and reflects `Settings` sources correctly.
- `scripts/check-parity.*`: new section for `lib/config.ps1`/`.sh` (§7c).
- Image-only (smoke): `/config set model ...` actually rebuilds and the new model answers the next
  turn; `harness config` wizard runs keyless in the harness venv without needing the full
  deepagents/langchain stack (it must not accidentally import `providers.resolve_chat_model` at
  wizard-build time, only at save/apply time — mirrors the acyclic-import discipline `cost.py`
  already follows relative to `providers.py`).

## 14. Explicitly out of scope (deferred, not forgotten)

- Live-reload of pre-spinup knobs without a container restart (would need the jail/mask/resource
  caps to become re-appliable to a running container, which Docker doesn't support for most of
  them — e.g. `--cpus` can be changed live via `docker update`, but the bwrap jail and mask
  overlays cannot).
- A GUI/web config surface — CLI flags + REPL menu + host wizard only, per the original ask.
- Per-workspace profile presets / named profiles (`--profile ci`, `--profile local`) — one
  profile file per workspace is the v1 shape; multiple named profiles is a natural follow-up once
  the single-profile shape is proven, not built now.
