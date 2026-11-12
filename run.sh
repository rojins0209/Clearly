#!/usr/bin/env sh
# One command: install, generate sample invoices if missing, start the app on http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements-dev.txt
[ -f .env ] && set -a && . ./.env && set +a
ls samples/*.pdf >/dev/null 2>&1 || python scripts/generate_samples.py
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
