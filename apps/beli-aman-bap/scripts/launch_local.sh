#!/bin/bash
# Local-only launcher for the Beli Aman BAP. Reads .env.local (cp from
# .env.example), then execs uvicorn via uv so the system Python is
# untouched. Mirrors infra/vm-bap/launch.sh shape.
#
# Usage: ./scripts/launch_local.sh
set -a
source "$(dirname "$0")/../.env.local"
set +a
exec uv run --no-project --python "$(dirname "$0")/../.venv" -- \
  uvicorn main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8003}" --proxy-headers
