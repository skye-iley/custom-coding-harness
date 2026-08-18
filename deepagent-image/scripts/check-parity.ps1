# Check .ps1 <-> .sh script pairs for drift.
#
# Reports line-count deltas and fails on a MISSING pair member. Deep content
# parity is NOT auto-checked: the two scripts express the same docker invocation
# in different shell syntax (${MountWorkspace} vs $MOUNT_WORKSPACE, /home/agent vs
# $HOME_DIR, Linux-only `-e HOME=/tmp`), so any text-level cross-shell diff yields
# false positives. Keep the pairs in sync by review. Mirror of check-parity.sh.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Failed = $false

$pairs = @(
    @("build.ps1", "build.sh"),
    @("run-docker.ps1", "run-docker.sh"),
    @("smoke.ps1", "smoke.sh"),
    @("verify.ps1", "verify.sh"),
    @("sync-models.ps1", "sync-models.sh"),
    @("dev-setup.ps1", "dev-setup.sh"),
    @("lib\config.ps1", "lib\config.sh")
)

foreach ($pair in $pairs) {
    $ps1 = Join-Path (Join-Path $Root "scripts") $pair[0]
    $sh = Join-Path (Join-Path $Root "scripts") $pair[1]
    if (-not (Test-Path $ps1)) { Write-Host "MISSING: $ps1"; $Failed = $true; continue }
    if (-not (Test-Path $sh)) { Write-Host "MISSING: $sh"; $Failed = $true; continue }
    $ps1Lines = (Get-Content $ps1).Count
    $shLines = (Get-Content $sh).Count
    $diff = $ps1Lines - $shLines
    Write-Host "$($pair[0]) ($ps1Lines lines) vs $($pair[1]) ($shLines lines) - diff $diff lines"
}

# Semantic parity (M4 trust boundary): markers that MUST appear in BOTH
# run-docker.{ps1,sh}. Line-count diff can't catch a fail-closed guard or a
# mask pre-flight dropped from one script only - this does. Mirror of check-parity.sh.
# M4 slice J adds the AppArmor preflight markers: a one-sided edit means one
# platform silently launches a jail that will die inside the container, or skips
# the fail-closed abort. install-apparmor-profile.sh is Linux-only and so has no
# .ps1 twin - deliberately absent from the pairs list above.
# M5 adds: the profile file must be MOUNTED (it is gitignored, so it is not in
# the image's COPY list - without the mount the container's resolve_settings()
# never sees a profile tier and `/config save` writes to a throwaway layer), and
# the caps must be forwarded as env since they are docker flags the container
# cannot otherwise observe.
# M5.1 R7: the "every pre-spinup profile key is actually READ by both launchers"
# half used to be two hand-picked markers here (pids_limit/net_jail). It is now
# derived from the field registry -
# test_config.py::test_prespinup_profile_keys_are_consumed_by_both_launchers
# checks ALL of them, so a new knob is covered without editing this list.
# The DEEPAGENTS_JAIL=1 / DEEPAGENTS_MASK_MODE= markers guard the third-pass F1 fix:
# the seccomp relaxation and the in-container jail must be turned on by the SAME
# decision (jail.jail_enabled() reads the env, not Settings), so a one-sided removal
# would relax five syscalls container-wide, start no jail, and turn nsguard off.
$markers = @("mask-scan", "refusing to launch unmasked", "DEEPAGENTS_MASK", "DEEPAGENTS_MASK_MODE",
             "deepagent-userns", "install-apparmor-profile", "DEEPAGENTS_JAIL_APPARMOR",
             # M4.1 fork J5: the third gate. Dropped from one launcher only, that
             # platform's jail dies at `--proc` with an EPERM naming neither profile.
             "systempaths=unconfined",
             # M8 B3: the benchmark driver pins the host state dir per instance so
             # it can read that instance's usage.jsonl back. Dropped from one
             # launcher only, the sweep on that platform silently joins against
             # the wrong telemetry, or none.
             "STATE_HOST_DIR",
             # M8 B3: a bench instance must be exactly what its dataset says. Dropped
             # from one launcher only, every prediction on that platform carries three
             # seeded harness files alongside the fix.
             "SEED_WORKSPACE",
             "DEEPAGENTS_JAIL_SYSTEMPATHS",
             "/project/.harness-profile.yaml",
             # M8 self-test findings: the bench driver selects the pytest-enabled
             # bench image via this var. Dropped from one launcher only, a sweep
             # on that platform silently runs the pytest-less runtime image and
             # no instance can ever verify its own patch.
             "DEEPAGENTS_IMAGE",
             "PIDS_LIMIT=", "DEEPAGENTS_JAIL=1", "DEEPAGENTS_MASK_MODE=")
$rdPs1 = Join-Path (Join-Path $Root "scripts") "run-docker.ps1"
$rdSh  = Join-Path (Join-Path $Root "scripts") "run-docker.sh"
foreach ($m in $markers) {
    $inPs1 = Select-String -Path $rdPs1 -Pattern ([regex]::Escape($m)) -Quiet
    $inSh  = Select-String -Path $rdSh  -Pattern ([regex]::Escape($m)) -Quiet
    if (-not $inPs1 -or -not $inSh) {
        Write-Host "PARITY: marker missing from one of run-docker.{ps1,sh}: '$m'"
        $Failed = $true
    }
}


# Milestone 5, C3/§7c: lib/config.{ps1,sh} resolution parity. True cross-language
# execution isn't attempted here (bash availability on a Windows dev machine is
# not guaranteed) -- instead each script asserts its OWN resolver against the
# same fixture + the same expected literal, so a precedence change in either
# resolver breaks its own run instead of silently drifting from the other.
# Mirror block in check-parity.sh.
$ConfigLibPs1 = Join-Path (Join-Path $Root "scripts") "lib\config.ps1"
if (Test-Path $ConfigLibPs1) {
    . $ConfigLibPs1
    $FixtureDir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Force -Path $FixtureDir | Out-Null
    $FixtureEnv = Join-Path $FixtureDir "env"
    $FixtureProfile = Join-Path $FixtureDir "profile"
    Set-Content -Path $FixtureEnv -Value "DEEPAGENTS_MASK_MODE=allow" -Encoding utf8
    Set-Content -Path $FixtureProfile -Value @(
        "jail: true",
        'cpus: "6"',
        "jail_apparmor:   # unset -- comment-only value"
    ) -Encoding utf8

    $GotMaskMode = Resolve-HostSetting -Value "" -EnvVarName "DEEPAGENTS_MASK_MODE" -ProfileKey "mask_mode" -Default "" -EnvFile $FixtureEnv -ProfileFile $FixtureProfile
    $GotJail     = Resolve-HostSetting -Value "" -EnvVarName "DEEPAGENTS_JAIL" -ProfileKey "jail" -Default "0" -EnvFile $FixtureEnv -ProfileFile $FixtureProfile
    $GotCpus     = Resolve-HostSetting -Value "" -EnvVarName "CPUS" -ProfileKey "cpus" -Default "2" -EnvFile $FixtureEnv -ProfileFile $FixtureProfile
    $GotApparmor = Resolve-HostSetting -Value "" -EnvVarName "DEEPAGENTS_JAIL_APPARMOR" -ProfileKey "jail_apparmor" -Default "" -EnvFile $FixtureEnv -ProfileFile $FixtureProfile
    $GotCliWins  = Resolve-HostSetting -Value "explicit" -EnvVarName "DEEPAGENTS_JAIL" -ProfileKey "jail" -Default "0" -EnvFile $FixtureEnv -ProfileFile $FixtureProfile

    if ($GotMaskMode -ne "allow")   { Write-Host "PARITY: lib/config.ps1 mask_mode got '$GotMaskMode' want 'allow'"; $Failed = $true }
    if ($GotJail -ne "true")        { Write-Host "PARITY: lib/config.ps1 jail got '$GotJail' want 'true'"; $Failed = $true }
    if ($GotCpus -ne "6")           { Write-Host "PARITY: lib/config.ps1 cpus got '$GotCpus' want '6'"; $Failed = $true }
    if ($GotApparmor -ne "")        { Write-Host "PARITY: lib/config.ps1 jail_apparmor got '$GotApparmor' want '' (comment-only)"; $Failed = $true }
    if ($GotCliWins -ne "explicit") { Write-Host "PARITY: lib/config.ps1 explicit value did not win, got '$GotCliWins'"; $Failed = $true }

    Remove-Item -Recurse -Force $FixtureDir -ErrorAction SilentlyContinue
}

if ($Failed) {
    Write-Host "PARITY CHECK FAILED"
    exit 1
}
Write-Host "PARITY CHECK OK"
