#!/usr/bin/env bash
# One-shot local setup: virtualenv, dependencies, config templates, tests.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements-dev.txt

[ -f config/settings.json ] || { cp config/settings.example.json config/settings.json; echo "Created config/settings.json - edit it."; }
[ -f .env ] || { cp .env.example .env; echo "Created .env - add your cookie and Gmail app password."; }
[ -f config/credentials.json ] || echo "Missing config/credentials.json (Google service-account key) - see README."

pytest -q
echo
echo "Ready. Try:  python -m bike_scout --dry-run --limit 3"
