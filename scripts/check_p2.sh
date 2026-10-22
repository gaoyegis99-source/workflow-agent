#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
mkdir -p .p2-runtime/verification
# Prevent inherited deployment settings from connecting baseline tests to live state.
unset V7_DATABASE_URL V7_OWNER_TOKEN
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q --tb=short --junitxml=.p2-runtime/verification/tests.xml
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
uv run --locked python scripts/verify_distribution.py
V7_POSTGRES_PASSWORD=configuration-validation-only docker compose -f deploy/p2.compose.yaml config --quiet
uv run --locked python scripts/record_p2.py
