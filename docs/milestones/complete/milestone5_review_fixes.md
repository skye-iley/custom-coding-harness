# Milestone 5 — third-pass review remediation (pre-merge)

**Status: specified, not implemented.** This is the fix spec for the findings of the final review
of `feat/milestone5-unified-config` before it merges to `main`. It is a companion to
`milestone5.md` §0.1 (the *second*-pass fixes, which are built) — same shape, one round later.
Fold the applicable rows into `milestone5.md` §0.1's table and delete this file once F1–F10 land.

Review baseline: `main..feat/milestone5-unified-config`, 12 commits, 24 files. Test state at
review time: `tests/test_config.py` + `tests/test_config_cli.py` + `tests/test_doctor.py` =
**104 passed**; `scripts/check-parity.ps1` = **OK**. Both stay green after every fix below.

## 0. Priority

| ID | Finding | Severity | Blocks merge |
|----|---------|----------|--------------|
| F1 | Profile/flag/host-env `jail` applies the seccomp relaxation but never starts the jail | **high, security** | yes |
| F2 | `run-docker.ps1 -Autonomy` writes a UTF-8 BOM; harness refuses to start | **high** | yes |
| F3 | `.agentignore` quick-edit inverts under `mask_mode: allow` | **medium, security** | yes |
| F4 | `ENV_VARS.md` prose inserted mid-table; `DEEPAGENTS_JAIL_APPARMOR` duplicated | low | no |
| F5 | Root `CLAUDE.md:26` scope-down list stale | low | no |
| F6 | "Dependency-light" claim for `config_cli.py` is false at package level | low, accuracy | no |
| F7 | `/config set topic` / `set model` don't reach the `past.sqlite` session row | medium | no |
| F8 | `/config save` can silently evaporate, or crash the REPL under the jail | medium | no |
| F9 | Wizard `.agentignore` / NetJail edits apply outside the save confirmation | low | no |
| F10 | Assorted: dead `_env_int`, unnamed `_resolve` error, cwd-anchored profile path, stale docstring | low | no |

Explicitly **out of scope** — see §11.

---

## 1. F1 — forward `DEEPAGENTS_JAIL` into the container

### The bug

`harness config` → "hardened" posture writes `jail: true` to `.harness-profile.yaml`.
`run-docker` resolves that through `Resolve-HostSetting` / `_resolve_host_setting`
(`run-docker.ps1:397`, `run-docker.sh:424`) and applies
`--security-opt seccomp=seccomp/userns.json` plus the slice-J AppArmor selection.

**Neither launcher ever forwards the value as `-e`.** `DEEPAGENTS_JAIL` appears in `run-docker.ps1`
only at lines 397 and 419, both inside `Resolve-HostSetting` calls. Inside the container,
`jail.jail_enabled()` (`harness/jail.py:200`) reads `os.environ[JAIL_ENV]` directly and never
consults `Settings`. So when the value comes from the profile tier, the `-Jail` flag, or a host
env var — anything other than `project/.env`, which `--env-file` forwards — the container starts
with:

- no bwrap re-exec (`cli.py:1441` `jail.jail_enabled()` is False), so `/project` stays writable and
  the shell tool still reaches `/project/state/denials.jsonl`
- `nsguard` **off**, because `DEEPAGENTS_NS_GUARD` defaults to tracking `DEEPAGENTS_JAIL`
- the *relaxed* seccomp profile applied: `clone`, `unshare`, `mount`, `umount2`, `pivot_root`
  permitted container-wide

That is strictly worse than running with the jail off: added kernel attack surface, zero
containment, and the tripwire that exists to compensate for the relaxation disabled. `/config`
compounds it — `resolve_settings()` reads the *mounted profile*, so the read-only half prints
`jail = True (profile)` and confirms the false belief.

The host-env path is pre-existing (M4 read host env for `jail_setup` and never forwarded it
either). M5 widens it with two new tiers and, critically, makes the profile the advertised way to
turn the jail on.

### The fix

Forward the resolved value from inside the block that already decided the jail is on, so the `-e`
cannot drift from the `--security-opt`.

`deepagent-image/scripts/run-docker.ps1`, in the `if ($JailMode -and $JailMode -notin @(...))`
block, immediately after line 405 (`$JailArgs = @("--security-opt", "seccomp=...")`):

```powershell
    # The seccomp relaxation and the in-container jail must be turned on by the SAME
    # decision. jail.jail_enabled() reads DEEPAGENTS_JAIL from the environment and does
    # not consult Settings, so a value resolved from the -Jail flag / host env / profile
    # tier would apply the relaxation here and never start the jail inside - relaxed
    # syscalls, no containment, and nsguard (which defaults to tracking DEEPAGENTS_JAIL)
    # off too. Normalized to "1": the container only tests truthiness.
    $JailArgs += "-e", "DEEPAGENTS_JAIL=1"
```

`deepagent-image/scripts/run-docker.sh`, in `jail_setup()`, immediately after line 431
(`JAIL_ARGS=(--security-opt "seccomp=$profile")`):

```bash
  # Same decision must turn on the relaxation AND the in-container jail - see the
  # comment in run-docker.ps1. jail.jail_enabled() reads the env, not Settings.
  JAIL_ARGS+=(-e "DEEPAGENTS_JAIL=1")
```

Both `JAIL_ARGS`/`$JailArgs` are already spliced into the agent `docker run` invocation, so no
call-site change is needed. Verify: `.ps1` line 421 `$dockerArgs = @("run","--rm") + $TtyFlags +
$JailArgs + @(...)`; `.sh` `build_agent_run` already expands `JAIL_ARGS`.

Normalizing to the literal `1` (rather than passing `$JailMode` through) is deliberate: the host
side accepts `true`/`yes`/`on`, `jail.jail_enabled()` has its own truthiness set, and there is no
reason to make the two agree on a vocabulary when only the boolean matters.

### Also forward `DEEPAGENTS_MASK_MODE` (same class, lower stakes)

`run-docker.ps1:343` / `run-docker.sh:379` forward the resolved mask mode to the **scan** container
only. The agent container never receives it. Consequences are limited — the bwrap jail overmounts
read the frozen `mask-snapshot.txt` (`jail.masked_from_snapshot`, deliberately *not* a fresh
`mask.resolve`), so enforcement is unaffected — but in-container `harness doctor` re-runs
`mask.resolve` (`mask.py:379` reads the env) and will report `deny` on an `allow` launch.

Add alongside `$ModelArgs` (`.ps1` line ~466) and `MODEL_ARGS` (`.sh` line ~528):

```powershell
$MaskModeArgs = @()
if (-not [string]::IsNullOrEmpty($ScanMode)) {
    $MaskModeArgs = @("-e", "DEEPAGENTS_MASK_MODE=$ScanMode")
}
```

```bash
MASK_MODE_ARGS=()
[[ -n "${RESOLVED_MASK_MODE:-}" ]] && MASK_MODE_ARGS=(-e "DEEPAGENTS_MASK_MODE=$RESOLVED_MASK_MODE")
```

Note `$ScanMode` is currently scoped inside the `if ($MaskEnabled ...)` block in the `.ps1` and
`scan_mode` is `local` to `mask_scan()` in the `.sh`. Hoist the resolution out of both (resolve
once at the top, near the cap resolution) and let the scan *and* the agent container consume the
same variable — one resolution, two consumers, which is the point of `lib/config`.

### Tests

- `tests/test_config.py` — new: `resolve_settings` with `jail: true` in a `tmp_path` profile and no
  `DEEPAGENTS_JAIL` env yields `Settings.jail is True` with source `"profile"`. Documents the
  container-side half of the contract.
- `scripts/check-parity.{sh,ps1}` — add markers `DEEPAGENTS_JAIL=1` and
  `DEEPAGENTS_MASK_MODE=` to the `$markers` / `markers` list, so a one-sided removal fails CI. This
  is the only cheap automated guard: the real property (relaxation ⇒ jail) is only observable in a
  live container.
- `scripts/smoke.{sh,ps1}` `-JailCheck` — no change needed; it already exercises the in-container
  gate. Worth one manual confirmation before merge: `harness config set jail true` with
  `DEEPAGENTS_JAIL` **absent** from `.env`, then `run-docker` and check `jail: re-exec` appears on
  stderr rather than the container coming up unjailed.

---

## 2. F2 — `-Autonomy` writes a UTF-8 BOM that the config parser rejects

### The bug

`run-docker.ps1:524` and `:526` write `.harness-config.yaml` via `Set-Content -Encoding utf8`. On
**Windows PowerShell 5.1** (this repo's documented primary shell; measured
`5.1.26100.8972`) that emits a BOM. Measured:

```
first bytes: 239,187,191,97,117
parse_config SystemExit: ...a.yaml: unknown key '﻿autonomy_level'
```

`config.parse_config` reads with `encoding="utf-8"`, which preserves `﻿`; the first key
becomes `﻿autonomy_level`, hits the unknown-key branch (`config.py:192`), and raises
`SystemExit`. `run-docker` then mounts that file, and the container dies at startup. So
`-Autonomy guided` — a flag added by this branch — bricks the run it was asked to configure.

`check-parity.ps1`'s own fixtures also write BOMs and pass, because `Select-String` strips the BOM
during decode. Only the Python readers see it. That asymmetry is why the parity check did not
catch this.

### The fix — both halves

**(a) Stop writing the BOM.** `run-docker.ps1`, replacing both `Set-Content ... -Encoding utf8`
calls in the `-Autonomy` block:

```powershell
    # NOT Set-Content -Encoding utf8: on Windows PowerShell 5.1 that writes a BOM, and
    # config.parse_config reads with encoding="utf-8" (not utf-8-sig), so the first key
    # parses as "﻿autonomy_level" and the harness exits on an unknown key.
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($HitlConfigPath, ($updated -join "`n") + "`n", $Utf8NoBom)
```

Applies to both branches (the update path, where `$updated` is a string array from
`Get-Content`/`-replace`, and the create path, where it is the single line
`"autonomy_level: $Autonomy"`). Normalize to `` `n `` line endings for parity with the `.sh` path
and with `config.py`'s own writers — `parse_config` splits on `splitlines()` so `\r\n` also works,
but there is no reason for the two launchers to produce different bytes.

**(b) Make the readers BOM-tolerant.** `harness/config.py` — this is the durable half, since a
`.harness-config.yaml` or `.harness-profile.yaml` hand-edited in Notepad hits the identical failure
and no launcher is involved:

- `load_config` (`config.py:142`): `path.read_text(encoding="utf-8-sig")`
- `load_profile` (`config.py:408`): `path.read_text(encoding="utf-8-sig")`

`utf-8-sig` strips a leading BOM when present and is a no-op otherwise, so this cannot regress a
BOM-free file. Leave the **writers** on plain `utf-8` — the harness should not emit BOMs, only
tolerate them.

Also audit `save_profile`'s in-place `OSError` fallback (`config.py:485`) and
`config_cli.write_hitl_preset` / `disable_hitl`: all already write plain `utf-8`. No change.

### Tests

- `tests/test_config.py` — `test_load_config_tolerates_utf8_bom`: write
  `"﻿autonomy_level: strict\n"` with `encoding="utf-8"`, assert `load_config` returns a
  `HitlSection` with `autonomy_level == "strict"` rather than raising `SystemExit`.
- `tests/test_config.py` — `test_load_profile_tolerates_utf8_bom`: same shape, `"﻿model: x\n"`,
  assert `{"model": "x"}`.

Both fail on current code (`SystemExit: unknown key '﻿...'`) and pass after. Per
`deepagent-image/CLAUDE.md`'s "every bug fix ships with a regression test", these target the
behavior (file parses) not the patch (which encoding string is used).

---

## 3. F3 — `.agentignore` quick-edit inverts under `mask_mode: allow`

### The bug

`config_cli._wizard_agentignore_step` (`config_cli.py:328`) offers **"add a path to mask"**, which
calls `agentignore_add_pattern` → appends a bare pattern to the workspace `.agentignore`.

Under M4's allow mode a bare pattern is the **allow-list entry**, not a mask. Per
`deepagent-image/CLAUDE.md`: *"In allow mode a plain pattern match IS the allow-list entry (the
resolver flips it visible); `!`-negation has no special meaning here"* — confirmed at
`mask.py:484-490` (`matched_tier == TIER_USER and is_masked` → `explicitly_visible.add(relpath)`).

This runs inside `harness config security`, **immediately after** `_wizard_security_step`, which is
where the operator may have just selected `mask_mode: allow` in the custom posture. So: operator
types `config/secrets.yaml` intending to hide it, and instead makes it one of the few visible
paths in the workspace. Unlike F1 and the M5.1 enum gap, this one **fails open**.

### The fix

Make the step mode-aware. In `_wizard_agentignore_step`, before the menu:

1. Resolve the effective mode — prefer the workspace file's own `#!mode:` header (M4 owns that
   grammar; read it, don't reimplement it — reuse `mask.py`'s parsing if it is exposed, else the
   literal `#!mode:allow` line check), falling back to `resolve_settings().mask_mode`.
2. In **deny** mode, behavior is unchanged.
3. In **allow** mode:
   - relabel option 2 to `"add a path to ALLOW (this workspace is in allow mode)"`, and
   - print a one-line warning naming the inversion, e.g.
     `[harness] note: .agentignore is in allow mode - a plain pattern makes a path VISIBLE, not masked. To hide something here, add it to the floor block (option 3).`
   - keep the floor option (option 3) unchanged and unqualified: the floor is mode-independent and
     is the correct answer for "hide this" in allow mode.

Do **not** silently rewrite the operator's intent (e.g. auto-routing a "mask" request to the floor).
The floor can never be negated; promoting an ordinary mask to a permanent floor entry behind the
operator's back is its own surprise. Name the situation, offer the right option, let them choose.

### Tests

`tests/test_config_cli.py`:
- `test_agentignore_step_warns_and_relabels_in_allow_mode` — `tmp_path` workspace whose
  `.agentignore` starts with `#!mode:allow`; stub `input` to pick option 2 then decline "add
  another"; assert the captured stdout contains the allow-mode note and the `VISIBLE` wording.
- `test_agentignore_step_unchanged_in_deny_mode` — no `#!mode:` header; assert the note is absent
  and the appended line lands exactly as today (guards against the fix leaking into the deny path).

---

## 4. F4 — `ENV_VARS.md` table broken by mid-table prose

`deepagent-image/ENV_VARS.md`: the M5 paragraph (`(Milestone 5, C3: -Model/-MaskMode/...)`) is
inserted between table rows. The two rows after it — `| NET_JAIL | -NetJail | ... |` and
`| DEEPAGENTS_JAIL_APPARMOR | — | ... |` — fall outside the table and render as literal pipe text.
`DEEPAGENTS_JAIL_APPARMOR` is additionally listed **twice**: once in the new M5 block
(`| DEEPAGENTS_JAIL_APPARMOR | -JailApparmor | AppArmor stance for the jail | from profile/.env |`)
and once in the pre-existing long-form row.

Fix: move the M5 prose paragraph **below** the final table row, and delete the new short
`DEEPAGENTS_JAIL_APPARMOR` row in favour of the existing long-form one — but move the long-form row
up so the `-JailApparmor` `.ps1` param column is populated on it (the old row has `—` in the param
column, which is now wrong: this branch added the flag).

Result: one contiguous table, one `DEEPAGENTS_JAIL_APPARMOR` row carrying both the `-JailApparmor`
param and the full auto/`unconfined`/profile-name explanation, prose after.

---

## 5. F5 — root `CLAUDE.md` scope-down list is stale

`CLAUDE.md:26` still reads:

> §4 records three deliberate scope-downs from the original plan (no `-Autonomy` host flag, no
> arrow-key `/config` menu, no NetJail list editor in `harness config security`)

Commit `6c4392c` shipped two of the three. `milestone5.md:5-7` was updated; the root summary was
not. Replace with the one remaining deviation:

> §4 records one deliberate deviation from the original plan (no arrow-key `/config` menu — most
> settable fields are free text, so a picker doesn't fit them) and a real `PauseMiddleware` caching
> bug the build surfaced and fixed along the way; §0.1 records the second-pass review fixes.

---

## 6. F6 — the "dependency-light" claim doesn't hold

`config_cli.py`'s module docstring (lines 12-18) and `deepagent-image/CLAUDE.md`'s Unified config
section both state it pulls no langchain/deepagents and that "importing it doesn't need the runtime
stack the way `cli.py` does."

`harness/__init__.py:27` is an unconditional `from harness.cli import main`, so **any** `harness.*`
import loads `cli.py` and therefore langgraph. Measured:

```
File "harness/cli.py", line 22, in <module>
    from langgraph.checkpoint.sqlite import SqliteSaver
ModuleNotFoundError: No module named 'langgraph'
```

In practice `harness config` reaches `config_cli` via `cli.dispatch`, so nothing is broken today —
but the claim is what would justify running the wizard on a host without the runtime stack, and it
is false.

Pick one:

- **(a) Make it true.** Drop the eager import from `harness/__init__.py`, exposing `main` via
  `__getattr__` (PEP 562) so `python3 -m harness` and `main.py` still work while
  `import harness.config_cli` stays stdlib-only. Then move the `config` branch in `cli.dispatch`
  ahead of the module-level langgraph import — which it cannot be, since `dispatch` lives *in*
  `cli.py`. So (a) also requires a small entry-point change: `__main__.py` routes `config`/`doctor`
  to their modules before importing `cli`. Real work; genuinely useful if host-side wizard use is
  intended.
- **(b) Drop the claim.** Reword both the docstring and CLAUDE.md to say `config_cli` adds no
  langchain/deepagents dependency *of its own* (true, and the reason it's a separate module), and
  state plainly that importing it through the `harness` package still loads the runtime stack.

**Recommend (b) for this branch**, and file (a) as an M5.1 item if host-side wizard use is a real
goal. This repo has already been bitten once by a shipped capability claim that outran the code
(M4 slice H / AppArmor); the cheap correct move is to make the doc match the code now and widen
the code deliberately later.

---

## 7. F7 — mid-session `/config set` doesn't reach the archive row

Two paths change the same knob and persist differently:

- `/topic <name>` → `_handle_topic` (`cli.py:648`) calls `archive.set_topic(conn, run_id, name)`.
- `/config set topic <name>` → `_handle_config` (`cli.py:~838`) rebinds `current_topic` only.

So `/config set topic` updates the recall lane for the rest of the session but leaves the
`past.sqlite` session row tagged with the launch topic, and a later `harness past list --topic` puts
the run in the wrong lane. Same class for the model: `bare_model` is computed once at `cli.py:1508`
and passed to `archive.start_session` before `run_repl`; a `/config set model` mid-session leaves
the whole run attributed to the launch model, which undercuts the spend-ledger role M2 gives
`past.sqlite`.

Fix:

- **topic** — give `_handle_config` the archive connection and `run_id` (it already receives
  `config`, `tracker`, `hitl_conf`; add `archive_conn=None, run_id=None` keyword-only, optional so
  the host-side tests keep calling it bare). In the `topic` branch, when `archive_conn is not None`,
  call `archive.set_topic(archive_conn, run_id, value)` — reuse the same call `_handle_topic` makes
  rather than duplicating it.
- **model** — needs a new archive setter, `archive.set_model(conn, run_id, provider, bare_model)`,
  called from the model branch after a successful rebuild. Keep `archive.py`'s acyclic rule: it
  imports neither `providers` nor `cost`, so `cli._handle_config` resolves
  `provider_for(value)`/`prefix`/`bare` and passes plain strings, exactly as `cli.py:1506-1508`
  already does.

Alternative, if a schema addition is unwanted before merge: record only the launch model but make
the switch say so — `_stage("config: model ... (the archive row keeps the launch model)")`. Weaker,
but honest, and a one-line change. **Recommend the real fix**; the ledger is the reason the archive
exists.

Tests: `tests/test_archive.py` for `set_model` round-trip; `tests/test_cli.py`
`test_handle_config_set_topic_updates_archive_row` and
`test_handle_config_set_model_updates_archive_row` using the existing in-memory-sqlite archive
fixtures.

---

## 8. F8 — `/config save` can evaporate silently, or take down the REPL

Two failure modes on the same line (`cli.py:~830`, `save_profile(Path.cwd() / PROFILE_NAME, values)`):

**(a) No mount ⇒ silent loss.** If the host has no `.harness-profile.yaml`, `run-docker` does not
mount one (`run-docker.ps1:552`, `run-docker.sh:584` are both `if exists`). `/config save` then
writes into the `--rm` container layer and prints
`[harness] config: wrote .harness-profile.yaml: model=...`. Success message, no file.
`deepagent-image/CLAUDE.md` documents the mechanism; the code gives no warning. This is the same
bug §0.1's first row fixed for the *mounted* case — the unmounted case was left.

Fix: in `_handle_config`'s `save` branch, before writing, detect that the target is not a bind
mount and warn rather than claim success. Cheapest reliable signal in-container: the file does not
exist **and** `DEEPAGENTS_IN_CONTAINER == "1"` (run-docker always mounts when the host file exists,
so a missing file in-container means no mount). Emit:

```
[harness] config: WARNING - no .harness-profile.yaml is mounted, so this write lands in the
          throwaway container layer and is lost on exit. Create it on the host first
          (cp project/.harness-profile.yaml.example project/.harness-profile.yaml) and relaunch.
```

and **do not** write. Refusing beats writing-and-warning: a write that cannot persist has no upside,
and the file it creates changes nothing about the current run either (`Settings` was resolved at
startup).

**(b) Read-only `/project` ⇒ uncaught `OSError`.** Under `DEEPAGENTS_JAIL=1` the re-exec binds
`/project` read-only (M4 slice H). `save_profile`'s atomic path raises `OSError` on
`tmp.write_text`, the `except OSError` fallback calls `path.write_text`, which raises **again** —
and this second raise is not caught. The `/config` branch in `run_repl` (`cli.py:1176-1190`) sits
*outside* the per-turn `try`, so the exception propagates out of the REPL loop and ends the session.

Fix: wrap the fallback's `path.write_text` in `config.save_profile` and re-raise as a typed error
(`ProfileNotWritable(OSError)` or plain `SystemExit` is wrong here — it must be catchable), then
have `_handle_config` catch `OSError` around the `save_profile` call and `_stage` it:

```
[harness] config: could not write .harness-profile.yaml (<err>) - /project is read-only under
          DEEPAGENTS_JAIL=1; save from the host with `harness config set` instead.
```

Belt and braces: also wrap the whole `/config` dispatch in `run_repl` in a `try/except Exception`
that `_stage`s and continues, matching how `run_repl` already refuses to let a turn failure kill the
container (`cli.py:1127`). A REPL command should never be able to end the session.

Tests: `tests/test_cli.py` — `test_handle_config_save_without_mount_refuses` (monkeypatch
`DEEPAGENTS_IN_CONTAINER=1`, `tmp_path` cwd with no profile, assert no file created and the warning
printed) and `test_handle_config_save_readonly_target_is_reported_not_raised` (monkeypatch
`save_profile` to raise `OSError`, assert `_handle_config` returns normally and stages the message).

---

## 9. F9 — wizard side-effect edits sit outside the save confirmation

`_wizard_agentignore_step` and `_wizard_netjail_step` write immediately
(`agentignore_add_pattern` / `netjail_add_entry` / `netjail_remove_entry` all touch disk as soon as
the operator answers). The wizard then prints a **Summary** listing only the profile `values`, and
asks `Save to .harness-profile.yaml?`. Answering **no** prints `[harness] not saved.` while the
`.agentignore` and NetJail edits are already on disk and unmentioned.

Fix, smallest honest version — do not restructure into a deferred-write transaction:

1. Have both steps return a list of the actions they performed (they already print each one).
2. Include them in the Summary block under a heading that states they are already applied, e.g.
   `Already applied (not part of the profile save):` followed by each action line.
3. Change the decline message from `[harness] not saved.` to
   `[harness] profile not saved. (The .agentignore / NetJail edits listed above were applied immediately.)`
   — emitted only when there were such edits.

This keeps one writer per file and makes the summary honest, which is the actual defect.

Test: `tests/test_config_cli.py` — extend `test_wizard_netjail_step_add_then_delete`'s sibling to
run `_run_wizard(security_only=True, auto_save=False)` with `input` declining the save, and assert
the captured output names the applied NetJail edit.

---

## 10. F10 — minor cleanups

| Item | Location | Action |
|---|---|---|
| `_env_int` is dead | `cli.py:159` | Removed with `_env_defaults` in C2; `_env_float` is still used (`cli.py:506-507`), `_env_int` is not. Delete. |
| `_resolve` error names no field | `config.py:392` | `raise SystemExit(f"invalid value {env_raw!r}")` — add the field and env var: `f"{env_name}: invalid value {env_raw!r}"`. Requires threading `env_name` into `_resolve` (it is already in scope at the `field()` closure). |
| Profile path is cwd-anchored | `config.py:504`, `doctor.py`, `config_cli._cmd_set` / `_run_wizard` | `harness config` run from the repo root writes `.harness-profile.yaml` where `run-docker` never reads it, while `config_cli.netjail_dir()` correctly anchors on `__file__`. Either anchor the profile path the same way when not in-container, or have `config_cli` refuse to run in a cwd with no `providers/` directory and name the expected cwd. Prefer the refusal — it's one check and it doesn't change the documented "reads `Path.cwd()`" contract `cli.main` relies on. |
| `harness/__init__.py` inventory stale | `__init__.py` docstring | Add `config_cli.py`; update `config.py`'s line — it reads "Milestone 3 S2: .harness-config.yaml + review_triggers matching" and now also owns the M5 `Settings` resolver. |
| `/config set topic` cannot unset | `cli.py` `_parse_config_set_args` | `len(args) < 2` rejects `/config set topic` with no value, so a topic can be set but never cleared. Accept a bare field for the nullable fields (`topic`) and treat it as unset, or document the limitation. Lowest priority here. |

---

## 11. Deliberately NOT fixed in this pass

**Enum values are not validated** (`harness config set mask_mode alow` is accepted, persisted, and
silently resolves to `deny`). Already documented as a known gap in `milestone5.md` §0.2 and
`deepagent-image/CLAUDE.md`, deferred to M5.1's per-field `choices` registry rather than adding a
twelfth hand-maintained per-field constant. It **fails safe** (the restrictive mode is the
fallback), which is what earns it the deferral — contrast F3 above, which fails *open* and
therefore does not get one. Do not patch it here without reading M5.1 §3.1.

Two clarifications worth folding into §0.2 when M5.1 lands, both verified during this review:

- **`harness config set` is not the only writer.** A hand-edited `.harness-profile.yaml` reaches the
  same unvalidated read — `load_profile` casts and does not validate — and
  `.harness-profile.yaml.example` ships `mask_mode: deny        # deny | allow`, inviting exactly
  that edit. Separately, `DEEPAGENTS_MASK_MODE` in `.env` or host env has been unvalidated since M4:
  `mask.py:379` takes the value verbatim and never checks it against `MODES`, the frozenset already
  defined and unused at `mask.py:49`. The underlying hole is M4's read path; M5 adds two write paths
  into it. §0.2 currently reads as M5-introduced.
- **The typo reaches the enforcement layer, not just storage.** `run-docker.ps1:343` /
  `run-docker.sh:379` forward the resolved value as `-e DEEPAGENTS_MASK_MODE=alow` into the *scan*
  container — the process that computes the actual docker overlay set. Still deny, still fail-safe,
  but "persisted and resolved" undersells where it lands.

**Dangling doc pointer.** `milestone5.md:56` and `deepagent-image/CLAUDE.md:845` both cite
`docs/milestones/planned/milestone5.1.md`; that directory does not exist on this branch (git does
not track empty directories, and root `CLAUDE.md` still describes `planned/` as "Currently empty").
Not worth creating a stub for. If M5.1 lands on its own branch, reword both citations to name the
branch; otherwise leave them as forward references.

---

## 12. Done-when

1. F1: `harness config set jail true` with `DEEPAGENTS_JAIL` absent from `.env`, then `run-docker` —
   the container re-execs into bwrap (stderr shows the jail line), `nsguard` is active, and
   `/config` reports `jail = True`. The relaxation and the jail turn on together or not at all.
2. F2: `run-docker.ps1 -Autonomy guided` on Windows PowerShell 5.1 produces a BOM-free
   `.harness-config.yaml` and the container starts. A BOM-prefixed config written by any other tool
   still parses.
3. F3: `harness config security` under `mask_mode: allow` names the inversion before the operator
   can add a pattern that would make a secret visible.
4. F4–F6, F10: docs and code agree; no claim in `CLAUDE.md` or a module docstring outruns what the
   code does.
5. F7: a mid-session `/config set topic` / `set model` is reflected in the `past.sqlite` row, or the
   switch says explicitly that it is not.
6. F8: `/config save` never prints success for a write that cannot persist, and no `/config`
   subcommand can end the session.
7. F9: the wizard's decline path names every edit already applied.
8. `python3 -m pytest tests/` green in the `test` image stage; `check-parity.{sh,ps1}` green on both
   platforms; `smoke.ps1 -JailCheck` unchanged.
9. Applicable rows folded into `milestone5.md` §0.1; this file deleted.
