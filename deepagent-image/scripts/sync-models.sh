#!/usr/bin/env bash
# Refresh providers/<provider>/models/*.toml from each provider's live API.
# Dev-time only: needs API keys (project/.env) and network. Runs in the harness
# image with the host providers/ dir bind-mounted so writes land in the repo.
# Pass through flags: ./scripts/sync-models.sh --dry-run --only openai anthropic
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/project/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE - copy project/.env.example to project/.env and set API keys." >&2
  exit 1
fi

exec docker run --rm \
  --env-file "$ENV_FILE" \
  -v "$ROOT/project/providers:/project/providers" \
  deepagent-harness \
  python3 -m harness sync-models "$@"
