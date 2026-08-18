#!/usr/bin/env bash
# One-shot setup for macOS / Linux: create .venv and install dependencies.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo
echo "Done. Start the web UI with:"
echo "    .venv/bin/python app.py"
