#!/usr/bin/env bash
# Create the OPTIONAL host dev venv at deepagent-image/.venv.
#
# Why this exists: deepagent-image/CLAUDE.md documents the keyless admin commands
# (`harness past list`, `harness config`, ...) as host-side and tells you to
# `source deepagent-image/.venv/bin/activate` -- but nothing created that venv, so
# every one of them, plus the image-tier tests and any langchain-touching probe,
# was reachable only through Docker.
#
# What it is NOT:
#   * Not a third Python stack. It mirrors the IMAGE's harness venv (/opt/venv)
#     from the same project/requirements.txt. The two-stack rule is unchanged:
#     harness deps here, workspace deps in <workspace>/.conda/env, never mixed.
#   * Not required. CI installs pytest and nothing else and runs the host tier
#     that way (.github/workflows/ci.yml). That property -- the suite runs with
#     nothing installed -- is load-bearing; this venv must stay opt-in and no
#     `pytest.importorskip` guard may be dropped because it exists locally.
#   * Not the authority. There is no lockfile, so this venv can drift from the
#     image (platform wheels, resolution date). `smoke` builds clean and stays
#     the check before a PR -- same caveat the bind-mount dev loop carries.
#
# Usage:
#   ./scripts/dev-setup.sh              # create (or reuse) and install
#   ./scripts/dev-setup.sh --recreate   # delete and rebuild from scratch
#
# Mirror of dev-setup.ps1 -- keep the pair in sync.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
REQ="$ROOT/project/requirements.txt"
# The image is ubuntu:24.04, whose python3 is 3.12. A host on a different minor
# resolves different wheels, which is the drift this warns about (not fatal --
# the harness supports a range, and smoke is what actually gates a PR).
IMAGE_PY_MINOR="3.12"

RECREATE=0
for arg in "$@"; do
  case "$arg" in
    --recreate) RECREATE=1 ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "dev-setup: unknown argument '$arg' (try --recreate)" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || {
  echo "dev-setup: no python3 on PATH" >&2
  exit 1
}
[ -f "$REQ" ] || {
  echo "dev-setup: requirements not found at $REQ" >&2
  exit 1
}

host_py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$host_py" != "$IMAGE_PY_MINOR" ]; then
  echo "dev-setup: NOTE host python is $host_py, image is $IMAGE_PY_MINOR." >&2
  echo "dev-setup:      Resolved wheels may differ from the image. smoke is the authority." >&2
fi

if [ "$RECREATE" = "1" ] && [ -d "$VENV" ]; then
  echo "dev-setup: removing $VENV"
  rm -rf "$VENV"
fi

if [ -d "$VENV" ]; then
  echo "dev-setup: reusing existing venv at $VENV"
else
  echo "dev-setup: creating venv at $VENV (python $host_py)"
  python3 -m venv "$VENV"
fi

# A venv made on Windows (Git Bash) lays out Scripts/ rather than bin/.
if [ -x "$VENV/bin/python" ]; then
  VPY="$VENV/bin/python"
  ACTIVATE="source $VENV/bin/activate"
elif [ -x "$VENV/Scripts/python.exe" ]; then
  VPY="$VENV/Scripts/python.exe"
  ACTIVATE="source $VENV/Scripts/activate"
else
  echo "dev-setup: venv looks incomplete (no python under $VENV)" >&2
  exit 1
fi

echo "dev-setup: installing harness deps + pytest (this pulls langchain; a few minutes cold)"
"$VPY" -m pip install --upgrade pip >/dev/null
# pytest is NOT in requirements.txt on purpose -- the image installs it only in
# the `test` stage, so the runtime image ships without it. The host venv is a dev
# tool, so it gets both.
"$VPY" -m pip install -r "$REQ" pytest

echo
echo "dev-setup: done. Activate with:"
echo "    $ACTIVATE"
echo
echo "Then, from deepagent-image/project/:"
echo "    python3 -m pytest tests/          # host + image tiers (importorskip no longer skips)"
echo "    python3 -m harness past list      # keyless admin commands"
echo
echo "Reminder: this venv is a convenience, not the gate. Run ./scripts/smoke.sh"
echo "before a PR -- it builds clean and catches what a local install papers over"
echo "(a missing COPY, a stale image layer, an image-only dep)."
