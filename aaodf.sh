#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Ensure virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi

# Ensure python dependencies are installed/up-to-date
if [ -f "requirements.txt" ]; then
    ./.venv/bin/python3 -m pip install --upgrade pip --quiet
    ./.venv/bin/pip install -r requirements.txt --quiet
fi

# Check for required system packages/binaries
MISSING_SYS_BINS=()
for bin in ffmpeg ffprobe mediainfo; do
    if ! command -v "$bin" &> /dev/null; then
        MISSING_SYS_BINS+=("$bin")
    fi
done

if [ ${#MISSING_SYS_BINS[@]} -gt 0 ]; then
    echo "Error: Missing required system dependencies: ${MISSING_SYS_BINS[*]}" >&2
    echo "Please install them using your package manager (e.g., sudo apt install ffmpeg mediainfo)." >&2
    exit 1
fi

exec sudo -u op ./.venv/bin/python3 -m aaodf "$@"
