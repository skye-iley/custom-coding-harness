#!/usr/bin/env bash
# Build the shippable deepagent-harness image from the repo root.
# --target runtime is REQUIRED: the Dockerfile's last stage is `test` (FROM
# runtime), so a bare `docker build` would tag the pytest-bearing test image as
# production. verify / run-docker consume this runtime tag.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
docker build --target runtime -t deepagent-harness "$ROOT"
