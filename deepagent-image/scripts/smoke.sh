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
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NETJAIL_DIR="$ROOT/netjail"
NET_JAIL="${NET_JAIL:-}"
KEEP_ARTIFACTS="${KEEP_ARTIFACTS:-}"

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

# Full suite via pytest discovery on the test image. -v names every test case
# (file::test PASSED/FAILED); -ra recaps non-passing tests at the end. Failures
# print the failing test id, file:line, and asserted values by default.
docker run --rm ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${PROXY_ENV[@]+"${PROXY_ENV[@]}"} \
  ${ARTIFACT_ARGS[@]+"${ARTIFACT_ARGS[@]}"} \
  deepagent-harness-test python3 -m pytest tests/ -v -ra

[[ -n "$ARTIFACT_HOST_DIR" ]] && echo "Test artifacts saved under $ARTIFACT_HOST_DIR"
