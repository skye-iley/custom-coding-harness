#!/usr/bin/env bash
# Check .ps1 ↔ .sh script pairs for drift.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

pairs=(
  "build.ps1 build.sh"
  "run-docker.ps1 run-docker.sh"
  "smoke.ps1 smoke.sh"
  "verify.ps1 verify.sh"
  "sync-models.ps1 sync-models.sh"
  "dev-setup.ps1 dev-setup.sh"
  "lib/config.ps1 lib/config.sh"
)

for pair in "${pairs[@]}"; do
  read -r ps1 sh <<< "$pair"
  ps1_path="$ROOT/scripts/$ps1"
  sh_path="$ROOT/scripts/$sh"
  if [[ ! -f "$ps1_path" ]]; then
    echo "MISSING: $ps1_path"
    FAILED=1
    continue
  fi
  if [[ ! -f "$sh_path" ]]; then
    echo "MISSING: $sh_path"
    FAILED=1
    continue
  fi
  # Line-count delta only. Deep content parity is NOT auto-checked: the two
  # scripts express the same docker invocation in different shell syntax
  # (${MountWorkspace} vs $MOUNT_WORKSPACE, /home/agent vs $HOME_DIR, Linux-only
  # `-e HOME=/tmp`), so any text-level cross-shell diff yields false positives.
  # Keep the pairs in sync by review. Mirror of check-parity.ps1.
  ps1_lines=$(wc -l < "$ps1_path")
  sh_lines=$(wc -l < "$sh_path")
  diff=$((ps1_lines - sh_lines))
  echo "$ps1 ($ps1_lines lines) vs $sh ($sh_lines lines) - diff $diff lines"
done

# Semantic parity (M4 trust boundary): markers that MUST appear in BOTH
# run-docker.{ps1,sh}. Line-count diff can't catch a fail-closed guard or a
# mask pre-flight dropped from one script only — this does. Mirror of check-parity.ps1.
markers=(
  "mask-scan"
  "refusing to launch unmasked"
  "DEEPAGENTS_MASK"
  "DEEPAGENTS_MASK_MODE"
  # M4 slice J: the AppArmor preflight. A one-sided edit here means one platform
  # silently launches a jail that will die inside the container, or (worse) skips
  # the fail-closed abort. install-apparmor-profile.sh itself is Linux-only and so
  # has no .ps1 twin — deliberately absent from `pairs` above.
  "deepagent-userns"
  "install-apparmor-profile"
  "DEEPAGENTS_JAIL_APPARMOR"
  # M4.1 fork J5: the third gate. Dropped from one launcher only, that platform's
  # jail dies at `--proc` with an EPERM that names neither profile — the exact
  # dead end §13.7 cost a measurement round to diagnose.
  "systempaths=unconfined"
  # M8 B3: the benchmark driver pins the host state dir per instance so it can
  # read that instance's usage.jsonl back. Dropped from one launcher only, the
  # sweep on that platform silently joins against the wrong (or no) telemetry.
  "STATE_HOST_DIR"
  # M8 B3: a bench instance must be exactly what its dataset says. Dropped
  # from one launcher only, every prediction on that platform carries three
  # seeded harness files alongside the fix.
  "SEED_WORKSPACE"
  "DEEPAGENTS_JAIL_SYSTEMPATHS"
  # M5: the profile file must be MOUNTED (it is gitignored, so it is not in the
  # image's COPY list -- without the mount the container's resolve_settings()
  # never sees a profile tier and `/config save` writes to a throwaway layer).
  #
  # M5.1 R7: the "every pre-spinup profile key is actually READ by both
  # launchers" half of this used to be two hand-picked markers here
  # (pids_limit/net_jail). It is now derived from the field registry --
  # test_config.py::test_prespinup_profile_keys_are_consumed_by_both_launchers
  # checks ALL of them, so a new knob is covered without editing this list.
  "/project/.harness-profile.yaml"
  # The caps are docker flags, not env vars, so the container can only report
  # them truthfully in `/config` if they are forwarded explicitly.
  "PIDS_LIMIT="
  # The seccomp relaxation and the in-container jail must be turned on by the same
  # decision: jail.jail_enabled() reads the env, not Settings, so dropping this -e
  # on one platform relaxes five syscalls container-wide, starts no jail, and turns
  # nsguard off. Strictly worse than jail-off, and silent. The real property
  # (relaxation ⇒ jail) is only observable in a live container; this is the cheap
  # guard against a one-sided removal.
  "DEEPAGENTS_JAIL=1"
  "DEEPAGENTS_MASK_MODE="
)
rd_ps1="$ROOT/scripts/run-docker.ps1"
rd_sh="$ROOT/scripts/run-docker.sh"
for m in "${markers[@]}"; do
  if ! grep -qF "$m" "$rd_ps1" || ! grep -qF "$m" "$rd_sh"; then
    echo "PARITY: marker missing from one of run-docker.{ps1,sh}: '$m'" >&2
    FAILED=1
  fi
done


# Milestone 5, C3/§7c: lib/config.{ps1,sh} resolution parity. True cross-language
# execution isn't attempted here (pwsh availability on a bash CI runner is not
# guaranteed) — instead each script asserts its OWN resolver against the same
# fixture + the same expected literal, so a precedence change in either
# resolver breaks its own CI run instead of silently drifting from the other.
# Mirror block in check-parity.ps1.
CONFIG_LIB_SH="$ROOT/scripts/lib/config.sh"
if [[ -f "$CONFIG_LIB_SH" ]]; then
  FIXTURE_DIR="$(mktemp -d)"
  printf 'DEEPAGENTS_MASK_MODE=allow\n' > "$FIXTURE_DIR/env"
  printf 'jail: true\ncpus: "6"\njail_apparmor:   # unset -- comment-only value\n' > "$FIXTURE_DIR/profile"

  ENV_FILE="$FIXTURE_DIR/env"
  PROFILE_FILE="$FIXTURE_DIR/profile"
  # shellcheck source=lib/config.sh
  source "$CONFIG_LIB_SH"

  got_mask_mode="$(_resolve_host_setting "" DEEPAGENTS_MASK_MODE mask_mode "")"      # .env fixture wins
  got_jail="$(_resolve_host_setting "" DEEPAGENTS_JAIL jail "0")"                     # profile fixture wins
  got_cpus="$(_resolve_host_setting "" CPUS cpus "2")"                                # profile fixture wins
  got_apparmor="$(_resolve_host_setting "" DEEPAGENTS_JAIL_APPARMOR jail_apparmor "")" # comment-only => default
  got_cli_wins="$(_resolve_host_setting "explicit" DEEPAGENTS_JAIL jail "0")"          # value arg always wins

  [[ "$got_mask_mode" == "allow" ]]    || { echo "PARITY: lib/config.sh mask_mode got '$got_mask_mode' want 'allow'" >&2; FAILED=1; }
  [[ "$got_jail" == "true" ]]          || { echo "PARITY: lib/config.sh jail got '$got_jail' want 'true'" >&2; FAILED=1; }
  [[ "$got_cpus" == "6" ]]             || { echo "PARITY: lib/config.sh cpus got '$got_cpus' want '6'" >&2; FAILED=1; }
  [[ "$got_apparmor" == "" ]]          || { echo "PARITY: lib/config.sh jail_apparmor got '$got_apparmor' want '' (comment-only)" >&2; FAILED=1; }
  [[ "$got_cli_wins" == "explicit" ]]  || { echo "PARITY: lib/config.sh explicit value did not win, got '$got_cli_wins'" >&2; FAILED=1; }

  rm -rf "$FIXTURE_DIR"
fi

if [[ $FAILED -ne 0 ]]; then
  echo "PARITY CHECK FAILED" >&2
  exit 1
fi
echo "PARITY CHECK OK"
