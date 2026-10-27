#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Ensure virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi

if [ -f "requirements.txt" ]; then
    ./.venv/bin/python3 -m pip install --upgrade pip --quiet
    ./.venv/bin/pip install -r requirements.txt --quiet
fi

# Extract run_as_user from config.json using Python
TARGET_USER=$(./.venv/bin/python3 -c "
import json
try:
    with open('config.json') as f:
        print(json.load(f).get('run_as_user', '').strip())
except Exception:
    print('')
")

PYTHON_BIN="./.venv/bin/python3"

# Execute as target user if specified and different from current user
if [ -n "${TARGET_USER}" ] && [ "$(id -un)" != "${TARGET_USER}" ]; then
    exec sudo -u "${TARGET_USER}" "${PYTHON_BIN}" -m aaodf "$@"
else
    exec "${PYTHON_BIN}" -m aaodf "$@"
fi
