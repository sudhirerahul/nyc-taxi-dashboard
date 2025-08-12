#!/usr/bin/env bash
set -euo pipefail
export PYTHONUNBUFFERED=1
export PORT="${PORT:-8000}"
python backend3.py
