# Build the shippable deepagent-harness image from the repo root.
# --target runtime is REQUIRED: the Dockerfile's last stage is `test` (FROM
# runtime), so a bare `docker build` would tag the pytest-bearing test image as
# production. verify / run-docker consume this runtime tag.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
docker build --target runtime -t deepagent-harness $Root
