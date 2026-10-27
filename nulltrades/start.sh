#!/usr/bin/env bash
# One command: set up on first run, build the UI if needed, start in PAPER on 127.0.0.1.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3.12 -m venv .venv
  .venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f ui/dist/index.html ]; then
  (cd ui && npm install --no-audit --no-fund && npm run build)
fi

# Secrets come only from the environment; .env is a local convenience.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

exec .venv/bin/python -m nulltrades "$@"
