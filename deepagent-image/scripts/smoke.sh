#!/usr/bin/env bash
# Post-build smoke. Builds both targets (self-contained — no ordering dependency
# on build.sh), runs a bare-runtime import check against the shippable image,
# then runs the whole suite via pytest discovery on the test image.
#
# Usage:
#   ./smoke.sh                  # normal (bridge networking)
#   NET_JAIL=1 ./smoke.sh       # run the import check + pytest INSIDE the NetJail
#                               # (--internal net + allowlisted egress proxy). Proves
#                               # the harness boots and the suite passes with no direct
#                               # egress, and that the jail plumbing stands up fail-closed.
#   KEEP_ARTIFACTS=1 ./smoke.sh # ship files that tests write via the `artifact_dir`
#                               # fixture out to test-artifacts/<timestamp>/ on the host
#                               # (default: they go to the container's tmp and vanish).
#   JAIL_CHECK=1 ./smoke.sh     # REQUIRE the M4 slice H jail gate to pass. By default
#                               # the gate runs but self-skips on a host that cannot
#                               # nest user namespaces; =1 turns that skip into a
#                               # failure (use in CI to pin the boundary).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NETJAIL_DIR="$ROOT/netjail"
NET_JAIL="${NET_JAIL:-}"
KEEP_ARTIFACTS="${KEEP_ARTIFACTS:-}"
JAIL_CHECK="${JAIL_CHECK:-}"

# ---------------------------------------------------------------------------
# NetJail plumbing — mirror of run-docker.sh's NET_JAIL path. Kept in sync with
# that script (and with run-docker.ps1 / smoke.ps1). Only used when NET_JAIL=1;
# netjail_up installs an EXIT trap that tears the sidecars down.
JAIL_NET="${NETJAIL_JAIL_NET:-deepagent-jail}"
EGRESS_NET="${NETJAIL_EGRESS_NET:-deepagent-egress}"
SOCAT_IMAGE="${NETJAIL_SOCAT_IMAGE:-alpine/socat:latest}"
PROXY_IMAGE="${NETJAIL_PROXY_IMAGE:-kalaksi/tinyproxy:latest}"
SIDECARS=()
FILTER_TMP=""
NET_ARGS=()
PROXY_ENV=()

netjail_down() {
  [[ ${#SIDECARS[@]} -gt 0 ]] && docker rm -f "${SIDECARS[@]}" >/dev/null 2>&1 || true
  [[ -n "$FILTER_TMP" && -f "$FILTER_TMP" ]] && rm -f "$FILTER_TMP" || true
}

netjail_up() {
  command -v docker >/dev/null || { echo "NET_JAIL: docker not found" >&2; exit 1; }
  trap netjail_down EXIT INT TERM

  # Networks (idempotent). jail = --internal (no gateway → no host/internet route).
  docker network inspect "$JAIL_NET"   >/dev/null 2>&1 || docker network create --internal "$JAIL_NET" >/dev/null
  docker network inspect "$EGRESS_NET" >/dev/null 2>&1 || docker network create "$EGRESS_NET" >/dev/null

  NET_ARGS=(--network "$JAIL_NET")
  PROXY_ENV=()
  local no_proxy="localhost,127.0.0.1"

  # Host-service forwarders. Each is attached to the egress net (has the host
  # route) plus the jail net (agent-visible), and relays exactly one TCP port.
  local services="$NETJAIL_DIR/host-services.txt"
  if [[ -f "$services" ]]; then
    local name port
    while read -r name port _; do
      [[ -z "${name:-}" || "$name" == \#* ]] && continue
      local cname="deepagent-fwd-$name"
      docker rm -f "$cname" >/dev/null 2>&1 || true
      docker run -d --rm --name "$cname" \
        --network "$EGRESS_NET" \
        --add-host=host.docker.internal:host-gateway \
        --cap-drop=ALL --security-opt=no-new-privileges \
        "$SOCAT_IMAGE" \
        "TCP-LISTEN:$port,fork,reuseaddr" "TCP:host.docker.internal:$port" >/dev/null
      docker network connect "$JAIL_NET" "$cname" >/dev/null
      SIDECARS+=("$cname")
      no_proxy="$no_proxy,$cname"
      # Convenience: point the agent's OLLAMA_HOST at the ollama forwarder.
      [[ "$name" == "ollama" ]] && PROXY_ENV+=(-e "OLLAMA_HOST=http://$cname:$port")
    done < "$services"
  fi

  # Egress proxy: domain-allowlisted HTTP(S) forward proxy for git/pip/npm. Only
  # started when the allowlist has at least one real entry.
  local domains="$NETJAIL_DIR/allowed-domains.txt"
  if [[ -f "$domains" ]] && grep -qvE '^[[:space:]]*(#|$)' "$domains"; then
    # Generate the tinyproxy Filter file: anchor each plain domain so it matches
    # the domain and its subdomains (and only those), not arbitrary substrings.
    FILTER_TMP="$(mktemp)"
    local d
    while read -r d _; do
      [[ -z "${d:-}" || "$d" == \#* ]] && continue
      printf '(^|\\.)%s$\n' "$(printf '%s' "$d" | sed 's/[.[\*^$]/\\&/g')" >> "$FILTER_TMP"
    done < "$domains"

    local pname="deepagent-proxy"
    docker rm -f "$pname" >/dev/null 2>&1 || true
    docker run -d --rm --name "$pname" \
      --network "$EGRESS_NET" \
      --cap-drop=ALL --security-opt=no-new-privileges \
      -v "$NETJAIL_DIR/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:ro" \
      -v "$FILTER_TMP:/etc/tinyproxy/filter:ro" \
      "$PROXY_IMAGE" >/dev/null
    docker network connect "$JAIL_NET" "$pname" >/dev/null
    SIDECARS+=("$pname")
    # Fail CLOSED: assert the proxy actually loaded our allowlist config. If the
    # conf mount silently failed to land, a stock proxy image falls back to its
    # default (filtering disabled) and would allow ALL egress — refuse to run the
    # smoke in that state rather than hand it an open proxy.
    sleep 2
    if ! docker exec "$pname" grep -q '^FilterDefaultDeny Yes' /etc/tinyproxy/tinyproxy.conf 2>/dev/null; then
      echo "NET_JAIL: egress proxy did not load the allowlist config (would fail open) — aborting." >&2
      docker logs "$pname" 2>&1 | tail -20 >&2 || true
      exit 1
    fi
    local purl="http://$pname:8888"
    PROXY_ENV+=(-e "HTTP_PROXY=$purl"  -e "HTTPS_PROXY=$purl" \
               -e "http_proxy=$purl"  -e "https_proxy=$purl" \
               -e "NO_PROXY=$no_proxy" -e "no_proxy=$no_proxy")
  fi
}

# Build the runtime (shippable) image and the test image (FROM runtime + pytest +
# tests/). The second build reuses the cached runtime layers. Builds run BEFORE
# any jail is stood up: image builds need pip/PyPI egress, which the jail denies.
docker build --target runtime -t deepagent-harness "$ROOT"
docker build --target test -t deepagent-harness-test "$ROOT"

# Under NET_JAIL=1 the two smoke runs below execute on the --internal jail network
# with the allowlisted egress proxy in front. Neither run makes outbound calls, so
# this proves the harness boots + the suite passes with zero direct egress, and
# that the jail plumbing (networks, forwarders, fail-closed proxy) stands up.
if [[ -n "$NET_JAIL" ]]; then
  netjail_up
  echo "NetJail: on (deny-all egress + allowlist)"
fi

# Test-artifact capture: when KEEP_ARTIFACTS=1, bind-mount a fresh host folder to
# /artifacts (OUTSIDE /project, so the conftest artifact-guard leaves it alone) and
# point DEEPAGENTS_TEST_ARTIFACTS_DIR at it, so files tests write via the
# `artifact_dir` fixture survive the disposable container. Off = the fixture falls
# back to the container's tmp_path and everything is deleted with the container.
ARTIFACT_ARGS=()
ARTIFACT_HOST_DIR=""
if [[ -n "$KEEP_ARTIFACTS" ]]; then
  ARTIFACT_HOST_DIR="$ROOT/test-artifacts/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$ARTIFACT_HOST_DIR"
  ARTIFACT_ARGS=(-v "$ARTIFACT_HOST_DIR:/artifacts" -e "DEEPAGENTS_TEST_ARTIFACTS_DIR=/artifacts")
  echo "KeepArtifacts: on -> $ARTIFACT_HOST_DIR"
fi

# Bare-runtime import smoke: third-party deps + the harness package (incl. the
# cost tracker, so a providers<->cost import cycle fails here). Runs against the
# plain runtime image — NO test layer — so a runtime import the test layer would
# mask still fails here.
docker run --rm ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
  deepagent-harness python3 -c "import deepagents, langgraph, langchain_openai; from harness.cli import main; from harness.cost import CostTrackerMiddleware; print('runtime import ok')"

# M4 smoke: verify mask resolution works end-to-end by running mask-scan
docker run --rm ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
  deepagent-harness python3 -c "from harness.mask import resolve; r = resolve('/tmp', '/tmp'); print(f'mask OK: {len(r.masked)} entries')"

# M4 fail-closed: a mask-scan failure MUST abort the launch, never run unmasked.
# (1) mask-scan signals failure via a nonzero exit on a poisoned config; (2) both
# launchers key their abort on that. Regression guard for the run-docker.{sh,ps1}
# fail-closed contract (a scan error must not degrade to a maskless launch).
scan_fail_dir="$(mktemp -d)"
printf 'secret\n#!mode: allow\n' > "$scan_fail_dir/.agentignore"   # directive-after-pattern -> SystemExit
if docker run --rm ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
     -v "$scan_fail_dir:/project/workspace:ro" \
     -e AGENT_WORKSPACE=/project/workspace -e DEEPAGENTS_STATE_DIR=/tmp/mask-state \
     deepagent-harness python3 -m harness mask-scan >/dev/null 2>&1; then
  rm -rf "$scan_fail_dir"
  echo "M4 fail-closed: mask-scan exited 0 on a poisoned .agentignore (expected nonzero)" >&2
  exit 1
fi
rm -rf "$scan_fail_dir"
for launcher in run-docker.sh run-docker.ps1; do
  grep -qF 'refusing to launch unmasked' "$ROOT/scripts/$launcher" \
    || { echo "M4 fail-closed: $launcher lost its fail-closed guard" >&2; exit 1; }
done
echo "M4 fail-closed: mask-scan aborts on failure + launchers guard against unmasked launch — ok"

# M4 slice H: the bwrap fs jail actually holds in the built image. This is the §3
# hard gate ("bwrap --unshare-all must really run here"), plus the boundary
# properties the jail is supposed to buy — asserted against the harness's own
# jail.bwrap_args, with NO docker mask applied, so the jail is the only enforcer.
# Needs the vendored narrow seccomp profile: Docker's default blocks unprivileged
# userns creation by design, which is exactly why DEEPAGENTS_JAIL is opt-in.
SECCOMP_PROFILE="$ROOT/seccomp/userns.json"
if [[ ! -f "$SECCOMP_PROFILE" ]]; then
  echo "M4 jail: $SECCOMP_PROFILE missing — run 'python3 -m harness seccomp-sync'" >&2
  exit 1
fi
set +e
# The script is piped in on stdin (`python3 -`) rather than bind-mounted. A bind
# needs a container-side absolute path, and under Git Bash on Windows MSYS
# rewrites a lone leading `/` into the host prefix (`/jail-check.py` ->
# `C:/Program Files/Git/jail-check.py`), breaking the mount target. Neither
# blanket fix works: MSYS_NO_PATHCONV=1 also un-converts the *host*-side seccomp
# path the daemon needs, and a `//` escape gets mangled inside the `-v src:dst`
# triple. stdin has no path to convert, so it is portable by construction.
docker run --rm -i ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
  --security-opt "seccomp=$SECCOMP_PROFILE" \
  -e DEEPAGENTS_JAIL=1 \
  deepagent-harness python3 - < "$ROOT/scripts/jail-check.py"
jail_rc=$?
set -e
if [[ $jail_rc -eq 77 ]]; then
  # Environmental, not a regression: some kernels/runtimes refuse nested userns
  # outright. Only a hard failure when the caller pinned it (CI).
  if [[ -n "$JAIL_CHECK" ]]; then
    echo "M4 jail: JAIL_CHECK=1 was set but this host cannot nest user namespaces — failing." >&2
    exit 1
  fi
  echo "M4 jail: SKIPPED (host cannot nest user namespaces). Set JAIL_CHECK=1 to require it."
elif [[ $jail_rc -ne 0 ]]; then
  echo "M4 jail: boundary check FAILED (rc=$jail_rc)" >&2
  exit 1
else
  echo "M4 jail: bwrap gate + masked/unmasked/write/ro boundary checks — ok"
fi

# Full suite via pytest discovery on the test image. -v names every test case
# (file::test PASSED/FAILED); -ra recaps non-passing tests at the end. Failures
# print the failing test id, file:line, and asserted values by default.
docker run --rm ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
  ${ARTIFACT_ARGS[@]+"${ARTIFACT_ARGS[@]}"} \
  deepagent-harness-test python3 -m pytest tests/ -v -ra

# NOT `[[ -n "$X" ]] && echo …`: as the *last* statement that idiom makes the
# script's exit status the status of the test, so a clean run with
# KEEP_ARTIFACTS unset exited 1 and reported the whole smoke as failed. `set -e`
# doesn't catch it (errexit skips the left side of a `&&` list) and it only bites
# on the success path, so it stayed invisible until CI ran the script directly.
if [[ -n "$ARTIFACT_HOST_DIR" ]]; then
  echo "Test artifacts saved under $ARTIFACT_HOST_DIR"
fi

# Explicit: the script succeeded iff it reached here (set -e aborts otherwise).
exit 0
