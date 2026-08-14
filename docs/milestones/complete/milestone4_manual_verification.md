# Milestone 4 — Manual Verification & Leak-Hunting Guide

> Companion to `milestone4.md` (scope/design) and its §19 invariants (the checkable
> properties). This file is the **operator's playbook**: concrete commands you run by hand to prove
> the trust boundary actually holds and that no secret leaks through a tool, a commit, or a config
> mistake.
>
> **Audience:** someone technically fluent (Docker, shell, Python) who does *not* need to know the
> internals of `harness/mask.py`. Every check below is either a copy-paste command with an expected
> result, or a small fixture you build first.
>
> **Platform:** PowerShell examples are primary (this repo's dev machine is Windows + Docker
> Desktop). Bash equivalents are given where the mechanism differs. The Python/pytest checks are
> cross-platform.
>
> **Working directory:** all commands assume you are in the **repo root** (`holder/`) unless a
> command block explicitly starts with `cd`. The `$WS` and `$STATE` variables defined in §1.3 persist
> across sections — keep the same PowerShell session.

---

## 0. What "a leak" means here — the mental model

M4 adds a floor between the agent and the workspace filesystem. There are exactly **four** ways a
secret could leak, and every manual test below targets one of them:

1. **Read leak** — the agent reads a secret's *contents* through a file tool, the shell tool, or a
   subprocess it spawns. (Masked files must read **empty**.)
2. **Escape leak** — the agent reads/writes a path *outside* the workspace via `../`, an absolute
   path, or a symlink that points out. (Path guard must refuse.)
3. **Commit leak** — the git-pr workflow stages a masked file and pushes it, either exposing it or
   **blanking the user's real secret** on the branch. (Staging must exclude the mask set.)
4. **Config leak** — a misconfiguration (a `!`-negation of a floor path, a deleted floor, an
   `allow`-mode hole) silently disables protection. (`doctor` + the resolver must catch it.)

The boundary is the **Docker container + the docker mount-mask**, *not* a sandbox. Masked files are
**present-but-empty**, not absent. Keep that in mind: the correct result of a successful mask is a
file that exists, `ls` shows it, but `cat` yields nothing and its size is 0.

**Known gaps in the default posture — i.e. with the jail OFF, which is the default.** Slice H has
**shipped** (opt-in, `DEEPAGENTS_JAIL=1`); the items below describe the boundary you get *without* it,
which is what an ordinary run has. Do not treat them as bugs. Where H closes one when enabled, that is
noted inline; to exercise the closed version, see **"Slice H — the bwrap jail"** at the end of this
document.
- The `permission_denied` HITL interrupt is **audit-only** — a path-guard denial is always a plain
  refused tool result (never an approval prompt). Do not test for an approval flow; there is nothing
  to approve — every v1 denial is a true workspace escape, which is never approvable by design.
  What you *should* see on a denial:
  - a `[harness] path-guard DENIED — …` line on **stderr**, always, with or without HITL; and
  - when HITL is on and the interrupt is enabled, a JSON record in **`<state-dir>/denials.jsonl`**
    (`resolved_by: "system"`, `meta.audit_only: true`) — under `run-docker` that is
    `deepagent-image/project/state/<ws-key>/denials.jsonl` on the host, **not** anywhere in the
    workspace. It is deliberately outside the workspace mount so the agent cannot truncate the
    record of its own escape attempt.
- **The denial record is not shell-proof.** The state dir defeats the agent's *file* tools (the path
  guard refuses it), but the shell tool is bounded only by the container root, so `cat`/`>` on the
  absolute state-dir path still works. Don't report that as a bug. **Closed by `DEEPAGENTS_JAIL=1`**,
  where the shell's nested `sandbox-exec` jail binds only the workspace.
- The floor's **3rd independent leg** is jail-gated. With the jail off, a floor file is protected by
  the docker overlay reading empty + the resolver dropping negations — **two** legs. With
  `DEEPAGENTS_JAIL=1` the jail overmounts masked paths empty *inside* the namespace, which is a third
  leg independent of the docker overlay. (There is no 4th leg — an earlier draft's "jailed worker
  refuses a floor path" belonged to a design that was not shipped.)
- The **shell tool is not routed through the path guard.** Shell path-escape is bounded only by the
  container root. A masked file the shell `cat`s still reads empty (the mask covers every process),
  but the guard itself covers the *file* tools only. **With the jail on**, shell and file tools share
  one namespace, so the shell is bounded by the bind set rather than the container root — the guard
  stays in front as cheap defense-in-depth.

---

## 1. One-time setup

### 1.1 Build the image

**Run from:** repo root (`holder/`)

```powershell
cd deepagent-image
.\scripts\build.ps1            # docker build --target runtime -t deepagent-harness
```

For the automated image-tier tests you also need the `test` stage:

```powershell
docker build --target test -t deepagent-harness:test .
```

### 1.2 Host Python for the fast/stdlib tests

**Run from:** repo root (`holder/`)

The resolver, path guard, and doctor are pure stdlib and run on a bare host with just `pytest`. No
API keys, no network, no Docker needed.

```powershell
cd deepagent-image\project
python -m pip install pytest         # once
```

### 1.3 Build a "poisoned" test workspace

**Run from:** repo root (`holder/`) — the `$WS` and `$STATE` variables persist for all later sections

Several checks need a workspace that *contains* secrets so you can watch them get masked. Build one
throwaway tree you will point the harness at (never your real repo):

```powershell
$WS = "$env:TEMP\m4-testws"
Remove-Item -Recurse -Force $WS -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$WS\src", "$WS\.ssh", "$WS\.aws", "$WS\secrets" | Out-Null

Set-Content "$WS\.env"                 "SECRET_TOKEN=leak-me-123"
Set-Content "$WS\server.pem"           "-----BEGIN PRIVATE KEY-----`nLEAK`n-----END PRIVATE KEY-----"
Set-Content "$WS\id_rsa"               "PRIVATE-SSH-KEY-LEAK"
Set-Content "$WS\.aws\credentials"     "aws_secret_access_key=LEAK"
Set-Content "$WS\.ssh\id_ed25519"      "SSH-LEAK"
Set-Content "$WS\secrets\password.txt" "hunter2"
Set-Content "$WS\secrets\README.md"    "this file is NOT secret"
Set-Content "$WS\src\app.py"           "print('this is normal source, must stay visible')"
```

This gives you: pattern-default hits (`.env`, `*.pem`, `id_rsa`, `.aws/credentials`, `.ssh/`), a
user-policy target (`secrets/`), a negation candidate (`secrets/README.md`), and a control file
(`src/app.py`) that must remain byte-for-byte visible.

---

## 2. Layer 1 — Run the automated suite first (fastest confidence)

Before any manual poking, the built-in tests already assert most invariants. A green suite is your
baseline; if it's red, stop and fix that first.

### 2.1 Host tier (stdlib — resolver, guard, doctor)

**Run from:** `deepagent-image\project`

```powershell
python -m pytest tests\test_mask.py tests\test_pathguard.py tests\test_doctor.py -v
```

What this proves (map to invariants):
- `test_mask.py` — gitignore parity, **floor invariant** (2, 5), symlink-out masking (6), emission
  grammar/minimization (9.3), snapshot/protection-reduction (24), `mask_add` raise-only (21).
- `test_pathguard.py` — `commonpath` not `startswith` sibling-escape (11), traversal/absolute/
  symlink-out refusal (11, 12), in-bounds pass (13).
- `test_doctor.py` — floor-negation → non-zero (22), clean registry → zero, keyless (25).

### 2.2 Image tier (needs deepagents runtime)

**Run from:** any directory (uses pre-built image)

```powershell
docker run --rm deepagent-harness:test python3 -m pytest tests\ -q
```

This runs the same host tests **plus** the image-only ones (`test_agent.py` backend guard wiring,
etc.). If a test is skipped on the host with `importorskip`, this is where it actually executes.

### 2.3 Script parity

**Run from:** repo root (`holder/`)

```powershell
.\scripts\check-parity.ps1        # or bash: ./scripts/check-parity.sh
```

Confirms `run-docker.ps1` and `run-docker.sh` both carry the mask markers (`mask-scan`, the
`refusing to launch unmasked` fail-closed guard, `DEEPAGENTS_MASK`, `DEEPAGENTS_MASK_MODE`). This is
invariant 29 — a mask step dropped from one shell only would ship a Windows-vs-Linux leak.

**If everything above is green, the logic is sound. The rest of this guide proves it end-to-end in a
real container — which is where a real leak would actually surface.**

---

## 3. Layer 2 — Inspect the resolver output (`mask-scan`)

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` variables from §1.3 must be set.

Before trusting the overlay, look at exactly *what* the resolver decided to mask. This is the single
source of truth every enforcement layer consumes.

```powershell
$WS    = "$env:TEMP\m4-testws"
$STATE = "$env:TEMP\m4-state"
Remove-Item -Recurse -Force $STATE -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $STATE | Out-Null

docker run --rm `
  -v "${WS}:/project/workspace:ro" `
  -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state `
  deepagent-harness python3 -m harness mask-scan
```

**Expected stdout** — one line per masked node, grammar `<mode> <type> <tier> <relpath>`:

```
mask file default .env
mask file default id_rsa
mask file default server.pem
mask dir  default .ssh
mask file default .aws/credentials
```

Read it critically:
- Every seeded secret appears. If `.env` or `id_rsa` is **missing**, the pattern-defaults regressed —
  a read leak is one launch away.
- `tier` is `default` (pattern-default) or `user` (from `.agentignore`) or `floor`. 
- `type` is `dir` for a whole masked directory (like `.ssh`), `file` otherwise.

### 3.1 Verify a user policy + negation

Add an in-workspace `.agentignore` and re-scan:

```powershell
Set-Content "$WS\.agentignore" "secrets/`n!secrets/README.md"
docker run --rm -v "${WS}:/project/workspace:ro" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness mask-scan
```

**Expected:** `secrets/password.txt` is masked (tier `user`), but **not** the whole `secrets` dir as
one `dir` line — because `README.md` is negated (visible), the resolver must emit the masked *leaf*
`secrets/password.txt` individually and leave the dir un-overlaid (docker overlay is all-or-nothing).
You should see something like `mask file user secrets/password.txt` and **no** `mask dir user
secrets` line. If you see the whole `secrets` dir masked as a `dir`, the negated `README.md` would be
blanked — that is a minimization bug.

### 3.2 Verify the floor is un-negatable

Write an authoritative floor into the **state dir** (this is where `<state>/agentignore` lives —
outside the workspace, agent-unreachable), then try to negate it from inside the workspace:

```powershell
Set-Content "$STATE\agentignore" "#!floor:`nid_rsa`n.aws/credentials"
Set-Content "$WS\.agentignore" "!id_rsa"
docker run --rm -v "${WS}:/project/workspace:ro" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness mask-scan
```

**Expected:** `id_rsa` is still masked, now tier `floor`, and **stderr** carries a warning like
`negation for floor path 'id_rsa' dropped — floor invariant`. This is invariants 2 + 13. If `id_rsa`
disappears from the output, the floor is negatable — a critical leak.

---

## 4. Layer 3 — The real end-to-end leak test (most important)

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` from §1.3 must be set.

This is the check that actually matters: mount the workspace **exactly as `run-docker` does**, apply
the overlays, then read the files as a **plain process** (not the agent). Reading as `cat`/`bash`
proves the mask changes the *real mounted filesystem* — so it covers the file tools, the shell tool,
and any subprocess (invariants 1 and 7), and it needs **no API key**.

Paste this whole block (PowerShell). It mirrors the launcher's scan→overlay→run sequence:

```powershell
$WS    = "$env:TEMP\m4-testws"
$STATE = "$env:TEMP\m4-state"

# 1. resolve the mask set
$scan = docker run --rm -v "${WS}:/project/workspace:ro" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness mask-scan

# 2. build the empty overlay sources + -v args (same logic as run-docker.ps1)
$ef = New-TemporaryFile
$ed = New-Item -ItemType Directory -Path $env:TEMP -Name ([IO.Path]::GetRandomFileName())
$mask = @()
foreach ($l in $scan) {
    $p = $l -split '\s+', 4
    if ($p.Length -lt 4) { continue }
    $src = if ($p[1] -eq 'dir') { $ed.FullName } else { $ef.FullName }
    $rel = $p[3] -replace '%20', ' '
    $mask += '-v', "${src}:/project/workspace/${rel}:ro"
}

# SANITY CHECK before trusting the read below: if $mask is empty here, the
# overlay silently no-ops and every file will read through UNMASKED — that
# looks exactly like a leak but is actually just this variable being lost
# (e.g. you pasted this in pieces across separate shell sessions instead of
# one block, per §1.3). Confirm you see 10 lines (2 per masked path) before
# reading the "FAIL = leak" verdict below.
if ($mask.Count -eq 0) { Write-Warning "MASK IS EMPTY — results below are meaningless, re-paste the whole block in one session" }
$mask

# 3. read the files back as a PLAIN process (agent-independent, keyless)
# NOTE: avoid here-string (@'...'@) — PS embeds literal CRLF which bash sees as \r
docker run --rm -v "${WS}:/project/workspace" @mask deepagent-harness bash -lc "echo '== ls =='; ls -la /project/workspace; echo '== .env (must be EMPTY) =='; cat /project/workspace/.env; echo '[size] '; wc -c < /project/workspace/.env; echo '== id_rsa (must be EMPTY) =='; cat /project/workspace/id_rsa; echo '[size] '; wc -c < /project/workspace/id_rsa; echo '== .aws/credentials (must be EMPTY) =='; cat /project/workspace/.aws/credentials; echo '[size] '; wc -c < /project/workspace/.aws/credentials; echo '== src/app.py (must be UNCHANGED) =='; cat /project/workspace/src/app.py"

Remove-Item -Force $ef; Remove-Item -Recurse -Force $ed
```

**Pass criteria:**
- `.env`, `id_rsa`, `.aws/credentials` all print **nothing** and report `[size] 0`. (Invariants 1, 7.)
- They still **appear** in `ls` — present-but-empty, not absent. (This is `mask` mode; absence is the
  deferred `hide` mode.)
- `src/app.py` prints its original line verbatim. (Invariant 8 — unmasked byte-identical.)

**Fail = leak:** if any secret prints its contents or shows a non-zero size, the overlay for that
path did not land. Check that the scan emitted the path, that the `-v` target matches exactly, and
that `type` (file vs dir) picked the right empty source.

### 4.1 Strengthen invariant 8 (byte-identical) with a hash

```powershell
$hostHash = (Get-FileHash "$WS\src\app.py" -Algorithm SHA256).Hash.ToLower()
$inHash = docker run --rm -v "${WS}:/project/workspace" @mask deepagent-harness `
  bash -lc "sha256sum /project/workspace/src/app.py | cut -d' ' -f1"
"host=$hostHash`ncontainer=$inHash"    # must match exactly
```

### 4.2 The one-command sanity version (with keys)

If you have API keys in `project\.env`, the real launcher does all of the above automatically. Run:

```powershell
.\scripts\run-docker.ps1 -WorkspacePath $env:TEMP\m4-testws "read the file id_rsa and tell me its exact contents"
```

**Don't target `.env` here.** `project\.harness-config.yaml`'s shipped example config gates any
`*.env` path (`review_triggers: - { on: path, pattern: "*.env" }`) under `autonomy_level: guided`.
In `--headless` mode that pause auto-denies (fail-closed HITL, by design — see
`deepagent-image/CLAUDE.md`), so the agent never reaches the read at all — you'd be testing HITL
gating, not the mask. `id_rsa` isn't covered by that trigger, so it exercises the actual read.

Watch for the `Mask: N path(s) masked` line at startup, then confirm the agent reports the file as
**empty** — not the key. This is the true adversarial test (the agent is *trying* to read it), but
it costs tokens and needs a key, so use the keyless §4 block for routine checks.

---

## 5. Layer 4 — Path-guard traversal (escape leak)

**Run from:** repo root (`holder/`) for §5.2; §5.1 has its own working dir.

The path guard is defense-in-depth on the **file** tools. Two ways to exercise it.

### 5.1 Direct unit exercise (keyless, fastest)

**Run from:** `deepagent-image\project`

```powershell
python -c "import sys; sys.path.insert(0,'harness'); from harness import pathguard as pg; import tempfile,os; b=tempfile.mkdtemp(); print('in-bounds:', pg.validate_path(os.path.join(b,'a.txt'), b));"
python -c "import sys; from harness import pathguard as pg; pg.validate_path('/etc/passwd','/tmp/workspace')"   # must raise PathGuardDenied
```

The second line must raise `PathGuardDenied`. The critical case is the **sibling escape**: a base of
`/workspace` must reject `/workspace-evil` (which a naive `startswith` would allow). `test_pathguard.py`
covers it; to eyeball it:

```powershell
python -c "from harness import pathguard as pg; pg.validate_path('/workspace-evil/x','/workspace')"  # must raise
```

### 5.2 In-container via the agent's file tool (with keys)

**Run from:** repo root (`holder/`)

```powershell
.\scripts\run-docker.ps1 -WorkspacePath $env:TEMP\m4-testws "use your file read tool to read ../../etc/passwd"
```

**Expected:** the read is refused (surfaces as a `PathGuardDenied` tool error), the agent reports it
could not read it, and the session **does not crash** and **does not** dump `/etc/passwd`. Note:
off-HITL (no `.harness-config.yaml`) this is a plain refused tool result — there is no approval
prompt in v1 (invariant 18; 15–17 deferred).

> **Scope reminder (invariant 14):** the guard covers the file tools only. If you instead ask the
> agent to `cat ../../etc/passwd` via the **shell** tool, the guard does not intercept it — the shell
> is bounded by the container root. That is expected v1 behavior, not a guard bug.

---

## 6. Layer 5 — git-pr never commits/blanks a masked secret (commit leak)

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` from §1.3 must be set.

This is the hazard the earlier design draft missed: a masked `.env` reads empty **inside the
container**, so a naive `git add -A` would stage the emptied file and the PR would **blank the user's
real secret** on the branch.

The staging step (`workflows/git-pr/stage-commit-push.sh`) reads the **frozen**
`<state>/mask-snapshot.txt` written by the pre-flight scan at launch and `git reset`s every masked
path out of the index — it does **not** re-run `mask-scan` at commit time. That distinction matters:
a live rescan would reflect whatever `.agentignore` currently says, and the workspace `.agentignore`
is agent-writable, so an agent (or prompt injection) deleting a mask entry mid-session would fool a
live rescan into no longer excluding a path whose content is still frozen-empty in the real mounted
fs — letting the empty version get committed over the real secret. The snapshot is the one thing that
can't be tampered with post-launch (state dir is agent-unreachable). To verify by hand, simulate the
staging inside a container against a git workspace, using the snapshot the same way the script does:

```powershell
# make the test workspace a git repo with a committed .env, write the frozen
# snapshot the way the launch-time scan would, then run the actual staging
# logic (reads the snapshot, does not rescan)
# NOTE: avoid PS here-string (@'...'@) — use a temp script to avoid CRLF in the bash -c arg
docker run --rm -v "${WS}:/project/workspace" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness bash -c "cd /project/workspace; git init -q; git add -A; git -c user.email=t@t -c user.name=t commit -qm init; printf '' > .env; git add -A; if [ \"\${DEEPAGENTS_MASK:-1}\" != \"0\" ] && [ -f \"\$DEEPAGENTS_STATE_DIR/mask-snapshot.txt\" ]; then while IFS=' ' read -r tier rel; do [ -n \"\$rel\" ] || continue; git reset -q -- \"\$rel\" 2>/dev/null || true; done < \"\$DEEPAGENTS_STATE_DIR/mask-snapshot.txt\"; fi; echo '== staged files =='; git diff --cached --name-only"
```

**Pass:** `.env` (and every other masked path) is **absent** from the staged file list. `src/app.py`
would be present if changed. (Invariant 19.) If `.env` appears staged, the emptied secret would be
committed — a commit leak.

### 6.1 Tamper resistance — the snapshot survives `.agentignore` deletion

The check above proves the happy path; it doesn't prove the snapshot resists tampering. Confirm the
frozen snapshot — not a live rescan — is what the script actually reads, by deleting the
`.agentignore` entry *after* the snapshot is written and confirming the exclusion still holds:

```powershell
docker run --rm -v "${WS}:/project/workspace" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness bash -c "cd /project/workspace; git init -q; git add -A; git -c user.email=t@t -c user.name=t commit -qm init; echo 'user .env' > \"\$DEEPAGENTS_STATE_DIR/mask-snapshot.txt\"; rm -f .agentignore; printf '' > .env; git add -A; while IFS=' ' read -r tier rel; do [ -n \"\$rel\" ] || continue; git reset -q -- \"\$rel\" 2>/dev/null || true; done < \"\$DEEPAGENTS_STATE_DIR/mask-snapshot.txt\"; echo '== staged files =='; git diff --cached --name-only"
```

**Pass:** `.env` is still absent from the staged list even though `.agentignore` no longer mentions it
— the exclusion came from the frozen snapshot, not a rescan of the (now-edited) workspace config. This
is exactly what `tests/test_workflows.py::test_git_pr_exclusion_survives_agentignore_tampering` asserts
automatically (§2.1 covers it in the fast pass).

---

## 7. Layer 6 — Frozen-at-launch, `mask_add`, state isolation

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` from §1.3 must be set.

### 7.1 Frozen at launch (invariant 9)

The mask set is computed **once** host-side before `docker run`. An agent editing an in-workspace
`.agentignore` mid-session cannot unmask the current run. To confirm the mechanism: the overlays in
§4 are `-v` bind mounts fixed at container start; there is no code path that re-scans mid-session.
You can sanity-check that no runtime re-scan exists:

```powershell
Select-String -Path deepagent-image\project\harness\*.py -Pattern "mask.resolve|mask-scan"
```

Expected callers: `mask_scan.py` (the pre-flight CLI), `doctor.py`, and the git-pr staging step —
**none** on a per-turn/agent-runtime path. `refresh_workspace` (ephemeral live-pull) also cannot
unmask, because the overlays sit *on top* of whatever it pulls in.

### 7.2 `mask_add` is raise-only (invariant 21)

```powershell
python -c "from harness import mask; import tempfile; s=tempfile.mkdtemp(); mask.append_deny(s,'newsecret.txt'); print(open(s+'/agentignore').read())"
```

The helper only ever **appends** to `<state>/agentignore`; there is no `mask_remove`. It writes the
**state dir** (agent-unreachable) and takes effect **next run** — it cannot unmask the current
session. Confirm there is no removal API:

```powershell
Select-String -Path deepagent-image\project\harness\mask.py -Pattern "def append_|def.*remove"
```

You should see `append_deny` / `append_floor` and **no** remove function.

### 7.3 State dir is outside the workspace (invariant 20)

The authoritative config, floor, and snapshot live in `<state>/…`, mounted at `/project/state` —
*not* under `/project/workspace`. The agent's file/shell tools are rooted at the workspace, so they
cannot reach it. Verify the mount separation:

```powershell
docker run --rm -v "${WS}:/project/workspace" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness bash -c "echo 'workspace sees:'; ls -a /project/workspace | grep -i agentignore; echo 'state (out of reach of workspace-rooted tools):'; ls /project/state"
```

The `<state>/agentignore` and `mask-snapshot.txt` must live under `/project/state`, and the agent —
rooted at `/project/workspace` — has no path to them.

---

## 8. Layer 7 — The removable contract (invariant 26)

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` from §1.3 must be set.

`DEEPAGENTS_MASK=0` must turn the whole feature off: no scan, no overlays, no floor, no snapshot —
byte-for-byte Milestone 3.

```powershell
# with masking OFF, the launcher must NOT emit any overlay
$env:DEEPAGENTS_MASK = "0"
.\scripts\run-docker.ps1 -WorkspacePath $env:TEMP\m4-testws "echo hello"   # observe: no "Mask: N path(s) masked" line
Remove-Item Env:\DEEPAGENTS_MASK
```

Or keyless, prove the files read normally when the overlays are absent (just don't apply `$mask`):

```powershell
docker run --rm -v "${WS}:/project/workspace" deepagent-harness bash -c "cat /project/workspace/.env"
# => prints SECRET_TOKEN=leak-me-123  (correct — masking is off, this is M3 behavior)
```

**Pass:** with `DEEPAGENTS_MASK=0` the secret reads through (that *is* the M3 baseline), and the
launcher prints no mask line. This confirms the escape hatch works and the feature is genuinely
removable.

---

## 9. Layer 8 — Protection-reduction warning is loud (invariant 24)

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` from §1.3 must be set.

The resolver snapshots the mask set each launch and shouts if a previously-masked path is no longer
masked between runs (tamper/regression signal).

```powershell
$STATE2 = "$env:TEMP\m4-state2"
Remove-Item -Recurse -Force $STATE2 -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Force $STATE2 | Out-Null

# run 1: full secrets present -> snapshot written
docker run --rm -v "${WS}:/project/workspace:ro" -v "${STATE2}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness mask-scan | Out-Null

# run 2: point at a workspace missing .env (simulate a path no longer masked)
$WS2 = "$env:TEMP\m4-testws2"; Copy-Item -Recurse -Force $WS $WS2; Remove-Item "$WS2\.env"
docker run --rm -v "${WS2}:/project/workspace:ro" -v "${STATE2}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness mask-scan
```

**Expected stderr on run 2:** `protection reduced — 1 path(s) no longer masked: .env`. Never silent.

---

## 10. Layer 9 — `harness doctor` catches config leaks (invariants 22, 23)

**Run from:** repo root (`holder/`) — `$WS` and `$STATE` from §1.3 must be set.

```powershell
# clean floor -> exit 0
docker run --rm -v "${WS}:/project/workspace" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness doctor /project/workspace /project/state
echo "exit: $LASTEXITCODE"   # 0
```

Now poison it — negate a floor path — and confirm doctor turns that resolver warning into an **error**
and exits non-zero:

```powershell
Set-Content "$STATE\agentignore" "#!floor:`nid_rsa"
Set-Content "$WS\.agentignore" "!id_rsa"
docker run --rm -v "${WS}:/project/workspace" -v "${STATE}:/project/state" `
  -e DEEPAGENTS_STATE_DIR=/project/state deepagent-harness python3 -m harness doctor /project/workspace /project/state
echo "exit: $LASTEXITCODE"   # non-zero, with a [doctor] [error] line about the floor
```

**Known limit (invariant 22, partial):** doctor errors on floor **negation**, but a floor **deletion**
(the whole `#!floor:` block removed) is only a **warning** with exit 0 — doctor cannot know a floor
"should" exist. A hard "floor is required" gate belongs in a seeded regression test, not doctor. Do
not expect deletion to fail CI.

Because doctor reuses the real `mask.resolve` (invariant 23), its verdict cannot drift from what the
launcher actually enforces — that's the whole point of running the same resolver in both places.

---

## 11. Invariant → test coverage matrix

Use this to confirm you've exercised each property. "Auto" = covered by the pytest suite (§2);
"Manual" = the section here that demonstrates it live.

| # | Invariant (short) | Auto | Manual |
|---|-------------------|------|--------|
| 1 | Floor never readable (every process) | — | §4 |
| 2 | Floor never negatable | ✅ test_mask | §3.2 |
| 3 | Floor never approvable | ✅ (structural) | — (no approval path in v1) |
| 4 | Floor always emitted | ✅ test_mask | §3.2 |
| 5 | Floor redundant (2 legs v1) | ✅ test_mask | §3.2 + §4 |
| 6 | Floor unreachable via alias (symlink) | ✅ test_mask | — |
| 7 | Masked reads empty | — | §4 |
| 8 | Unmasked byte-identical | — | §4 / §4.1 |
| 9 | Frozen at launch | ✅ (no re-scan path) | §7.1 |
| 10 | Mask matches what agent sees (ephemeral copy) | — | §4 (uses MountWorkspace) |
| 11 | commonpath not startswith | ✅ test_pathguard | §5.1 |
| 12 | Guard on resolved path (symlink-out) | ✅ test_pathguard | §5 |
| 13 | Legit ops never blocked | ✅ test_pathguard | §5.1 |
| 14 | Honest scope (file tools only) | — | §5.2 note |
| 15–17 | permission_denied interrupt | **deferred v1** | — |
| 18 | Off-HITL = plain refusal | ✅ test_agent | §5.2 |
| 19 | Masked never committed | ✅ test_workflows | §6 |
| 20 | State config agent-unreachable | — | §7.3 |
| 21 | mask_add raise-only | ✅ test_mask | §7.2 |
| 22 | Weakened floor fails CI (partial) | ✅ test_doctor | §10 |
| 23 | doctor reuses runtime resolver | ✅ | §10 |
| 24 | Protection reduction is loud | ✅ test_mask | §9 |
| 25 | CI keyless | ✅ (CI) | §2 |
| 26 | Removable seam (MASK=0 = M3) | ✅ | §8 |
| 27 | Two-stack (harness venv, stdlib) | ✅ import tests | §2.1 (runs on bare host) |
| 28 | Acyclic imports | ✅ test_import_isolation | — |
| 29 | Script parity | ✅ check-parity | §2.3 |
| 30 | Every behaviour has a regression test | ✅ (suite) | — |

---

## 12. Fast leak-hunt checklist (the 90-second version)

When you just want a quick "is anything leaking right now" pass:

```powershell
# 1. logic green?  (run from deepagent-image\project)
python -m pytest tests\test_mask.py tests\test_pathguard.py tests\test_doctor.py -q
# 2. both shells masked?  (run from repo root)
.\scripts\check-parity.ps1
# 3. end-to-end: run the §4 keyless block against $env:TEMP\m4-testws
#    -> .env/id_rsa/.aws-credentials size 0, src/app.py unchanged
#    (run from repo root with $WS, $STATE from §1.3)
```

If (1) is green, (2) says OK, and (3) shows every seeded secret at size 0 while the source file is
intact, the boundary is holding for read, escape, and config leaks. Add §6 when you touch anything in
`workflows/git-pr/`.

---

## Slice H — the bwrap jail (opt-in)

Everything above describes the **default** posture (jail off). This section verifies the boundary you
get with `DEEPAGENTS_JAIL=1`. It is opt-in because enabling it relaxes the *outer* container's seccomp
filter to permit unprivileged user namespaces (`milestone4.md` §16 fork 7, `seccomp/README.md`).

**Automated — run this first.** It is the whole section in one command:

```bash
JAIL_CHECK=1 ./scripts/smoke.sh            # bash
.\scripts\smoke.ps1 -JailCheck             # PowerShell
```

Green means: bwrap unshares under the vendored profile, a masked path reads **empty inside the jail
with the docker mask switched off**, an unmasked file is byte-identical, the workspace is writable,
and `/project` is read-only. Red on the *first* check usually means the seccomp profile is stale or
missing — run `python3 -m harness seccomp-sync --check`.

**By hand**, if you want to see it directly:

```bash
# 1. the gate itself — expect a namespace refusal WITHOUT the profile ...
docker run --rm deepagent-harness bwrap --unshare-all true
#    -> bwrap: No permissions to create new namespace   (this failure is CORRECT)

# 2. ... and a clean run WITH it
docker run --rm --security-opt seccomp=deepagent-image/seccomp/userns.json \
  deepagent-harness bwrap --unshare-all --ro-bind /usr /usr \
  --symlink usr/lib64 /lib64 --symlink usr/lib /lib --symlink usr/bin /bin \
  /usr/bin/echo JAIL_OK
#    -> JAIL_OK
```

> **Trap when hand-rolling a fixture:** stage the workspace and state dir **outside `/tmp`**.
> `jail.bwrap_args` emits `--tmpfs /tmp` *after* the binds, so a fixture under `/tmp` gets
> overmounted and every check silently reports a false negative (a "masked" file reads empty because
> the whole tree vanished, not because the mask worked). Use `/project/workspace` and
> `/project/state`, as the real launcher does.

**What the jail does NOT change** — do not go looking for these:

- A masked read still **succeeds and returns empty**; it does not become an explicit denial. So there
  is still no approvable `permission_denied` case (invariant 16 stays deferred to `hide` mode or the
  overlayfs view). Every `PathGuardDenied` is still a true escape.
- The **state dir is still bound** in the harness's own namespace — `checkpoints.sqlite` needs it.
  What changes is that the *shell tool* gets a nested jail binding only the workspace, so the shell
  loses its `cat`/`>` reach into `denials.jsonl`. Verify that from the shell tool, not from the
  harness process.

---

**Cross-refs:** `milestone4.md` (§10 resolver, §11 enforcement, §11.4 jail, §13 knobs, §14 threat
model, §15 gotchas), `milestone4.md` §19 (the numbered properties), `deepagent-image/CLAUDE.md`
(Workspace visibility / secret masking section), and the code seams: `harness/mask.py`,
`harness/mask_scan.py`, `harness/pathguard.py`, `harness/doctor.py`, `agent.py`
(`_WorkspaceShellBackend._resolve_path`), `scripts/run-docker.{ps1,sh}` (mask pre-flight),
`workflows/git-pr/stage-commit-push.sh` (staging exclusion), and for slice H `harness/jail.py`,
`harness/seccomp.py`, `seccomp/userns.json`, `scripts/sandbox-exec.sh`.
