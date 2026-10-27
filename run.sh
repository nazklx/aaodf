#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN=".venv/bin/python3"

setup_venv() {
    echo "Recreating virtual environment (.venv)..."
    rm -rf .venv
    python3 -m venv .venv
    "${PYTHON_BIN}" -m pip install --upgrade pip --quiet
    if [ -f "requirements.txt" ]; then
        "${PYTHON_BIN}" -m pip install -r requirements.txt
    fi
}

# Check if .venv, python binary, or pip module is missing/broken
if [ ! -d ".venv" ] || [ ! -x "${PYTHON_BIN}" ] || ! "${PYTHON_BIN}" -m pip --version &>/dev/null; then
    echo "Warning: Virtual environment is missing or broken."
    read -p "Do you want to recreate it automatically? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        setup_venv
    else
        echo "Aborted." >&2
        exit 1
    fi
else
    if [ -f "requirements.txt" ]; then
        "${PYTHON_BIN}" -m pip install --upgrade pip --quiet
        "${PYTHON_BIN}" -m pip install -r requirements.txt --quiet
    fi
fi

# Extract run_as_user from config.json using Python
CONFIG_USER=$("$PYTHON_BIN" -c "
import json
try:
    with open('config.json') as f:
        print(json.load(f).get('run_as_user', '').strip())
except Exception:
    print('')
")

CURRENT_USER="$(id -un)"
TARGET_USER="${CONFIG_USER:-$CURRENT_USER}"

# Execute as target user, setting PYTHONPATH to src so python -m src works
if [ "${CURRENT_USER}" != "${TARGET_USER}" ]; then
    exec sudo -u "${TARGET_USER}" env PYTHONPATH="src" "${PYTHON_BIN}" -m src "$@"
else
    export PYTHONPATH="src"
    exec "${PYTHON_BIN}" -m src "$@"
fi
