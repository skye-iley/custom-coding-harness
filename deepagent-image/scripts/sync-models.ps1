# Refresh providers/<provider>/models/*.toml from each provider's live API.
# Dev-time only: needs API keys (project\.env) and network. Runs in the harness
# image with the host providers\ dir bind-mounted so writes land in the repo.
# Pass through flags: .\scripts\sync-models.ps1 --dry-run --only openai anthropic
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root "project\.env"

if (-not (Test-Path $EnvFile)) {
    Write-Error "Missing $EnvFile - copy project\.env.example to project\.env and set API keys."
    exit 1
}

$ProvidersMount = (Join-Path $Root "project\providers") + ":/project/providers"

docker run --rm `
    --env-file "$EnvFile" `
    -v "$ProvidersMount" `
    deepagent-harness `
    python3 -m harness sync-models @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
