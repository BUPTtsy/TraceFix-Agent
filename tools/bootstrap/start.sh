#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python tools/bootstrap/bootstrap.py "$@"
fi
exec python3.12 tools/bootstrap/bootstrap.py "$@"
