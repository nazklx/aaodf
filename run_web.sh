#!/usr/bin/env bash
# Start the web UI. Usage: ./run_web.sh [--host H] [--port P] [--root DIR] [--jobs N]
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN=".venv/bin/python3"
if [ ! -x "${PYTHON_BIN}" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi
"${PYTHON_BIN}" -m pip install --upgrade pip --quiet
"${PYTHON_BIN}" -m pip install -r requirements.txt --quiet
exec "${PYTHON_BIN}" -m webui "$@"
