#!/usr/bin/env bash
# Build the shippable deepagent-harness image from the repo root.
# --target runtime is REQUIRED: the Dockerfile's last stage is `test` (FROM
# runtime), so a bare `docker build` would tag the pytest-bearing test image as
# production. verify / run-docker consume this runtime tag.
#
# BENCH=1 ./build.sh also builds the bench stage (runtime + pytest, no tests/),
# tagged deepagent-harness-bench. Opt-in: the bench driver is the only consumer
# (via DEEPAGENTS_IMAGE), and building it every time would cost every ordinary
# build a second image for a tag most runs never touch.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
docker build --target runtime -t deepagent-harness "$ROOT"
if [[ "${BENCH:-0}" == "1" ]]; then
  docker build --target bench -t deepagent-harness-bench "$ROOT"
fi
