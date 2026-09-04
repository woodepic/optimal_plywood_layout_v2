#!/bin/sh
# Start the layout UI. Reachable from other devices on the same network.
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python webapp/app.py "$@"
