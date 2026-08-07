#!/usr/bin/env bash
# Create or update the workspace-local conda env from environment.yml.
set -euo pipefail

WS="${1:-${AGENT_WORKSPACE:-/project/workspace}}"
ENV_FILE="$WS/environment.yml"
ENV_PREFIX="$WS/.conda/env"
PKGS_DIR="$WS/.conda/pkgs"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
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

echo "Done. Activate with: source /opt/conda/etc/profile.d/conda.sh && conda activate $ENV_PREFIX"
echo "Or run commands via: $WS/scripts/run-in-env.sh <command>"
