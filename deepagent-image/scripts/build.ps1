# Build the deepagent-harness image from the repo root.
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root
docker build -t deepagent-harness $Root
