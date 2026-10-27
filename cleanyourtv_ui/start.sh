#!/usr/bin/env bash
# One command: fetch CleanYourTV on first run, set up its tools, start the UI on 127.0.0.1.
set -euo pipefail
cd "$(dirname "$0")"
REPO="${CLEANYOURTV_DIR:-$PWD/upstream}"

if [ ! -f "$REPO/tools/tv.py" ]; then
  git clone --depth 1 https://github.com/Gathin23/cleanyourtv "$REPO"
fi

# The project's own scripts look for tools/venv; the Setup button does the same thing.
if [ ! -x "$REPO/tools/venv/bin/python" ]; then
  python3 -m venv "$REPO/tools/venv"
  "$REPO/tools/venv/bin/pip" install --quiet --disable-pip-version-check -r "$REPO/tools/requirements.txt"
fi

cd ..
exec python3 -m cleanyourtv_ui --repo "$REPO" "$@"
