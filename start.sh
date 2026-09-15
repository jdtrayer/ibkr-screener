#!/usr/bin/env bash
# Launches the scanner: activates .venv and runs main.py, from wherever
# this script is invoked (cds to the repo root first, based on this file's
# own location, so it works regardless of the caller's cwd).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
source .venv/bin/activate
exec python3 main.py
