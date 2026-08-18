#!/usr/bin/env bash
# Create or update the workspace-local conda env from environment.yml.
set -euo pipefail

WS="${1:-${AGENT_WORKSPACE:-/project/workspace}}"
ENV_FILE="$WS/environment.yml"
ENV_PREFIX="$WS/.conda/env"
PKGS_DIR="$WS/.conda/pkgs"

if [[ ! -f "$ENV_FILE" ]]; then
  # A workspace with no environment.yml has no conda-managed dependencies to
  # activate -- that is not an error, it is "there is nothing to do here".
  # Exiting 0 (not 1) lets a project that is plain-stdlib code + pytest run
  # tests directly against the harness venv instead of dead-ending on a hard
  # failure that names a file the project was never going to ship (a workspace
  # is not obligated to have conda dependencies just because this script
  # exists). See milestone8_selftest_findings.md §4 point 3.
  echo "No $ENV_FILE -- nothing to activate. If this project has no conda"
  echo "dependencies, run its tests directly (e.g. python -m pytest)."
  exit 0
fi

# shellcheck source=/dev/null
source /opt/conda/etc/profile.d/conda.sh

mkdir -p "$PKGS_DIR"
export CONDA_PKGS_DIRS="$PKGS_DIR"

cd "$WS"

if [[ -x "$ENV_PREFIX/bin/python" ]]; then
  echo "Updating conda env at $ENV_PREFIX"
  conda env update -p "$ENV_PREFIX" -f "$ENV_FILE" --prune
else
  echo "Creating conda env at $ENV_PREFIX"
  conda env create -p "$ENV_PREFIX" -f "$ENV_FILE"
fi

echo "Done. Activate with: . /opt/conda/etc/profile.d/conda.sh && conda activate $ENV_PREFIX"
echo "Or run commands via: $WS/scripts/run-in-env.sh <command>"
