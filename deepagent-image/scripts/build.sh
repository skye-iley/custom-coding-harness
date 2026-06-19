#!/usr/bin/env bash
# Build the deepagent-harness image from the repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
docker build -t deepagent-harness "$ROOT"
