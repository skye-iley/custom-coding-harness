#!/usr/bin/env bash
# Wrapper to activate workspace conda environment before running commands.
set -euo pipefail

# Ensure we have the conda profile script
if [[ -f "/opt/conda/etc/profile.d/conda.sh" ]]; then
    source /opt/conda/etc/profile.d/conda.sh
    conda activate /project/workspace/.conda/env
else
    echo "Error: Conda initialization script not found." >&2
    exit 1
fi

# Execute the passed command
exec "$@"
