#!/usr/bin/env bash
set -euo pipefail

phase="${2:-drafts}"
lint_paths=(apps packages scripts tests)
if [[ "${1:-}" == "--phase" ]]; then
  phase="${2:?phase is required}"
fi

case "$phase" in
  drafts)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q tests/authoring/a1
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  editor)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q tests/authoring/a1 tests/authoring/a2
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  workflow-release)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q tests/authoring/a1 tests/authoring/a2 tests/authoring/a3
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  operator-registration)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q \
      tests/authoring/a1 tests/authoring/a2 tests/authoring/a3 tests/authoring/a4
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  advanced-cwl)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q \
      tests/authoring/a1 tests/authoring/a2 tests/authoring/a3 \
      tests/authoring/a4 tests/authoring/a5
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  release-library)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q \
      tests/authoring/a1 tests/authoring/a2 tests/authoring/a3 \
      tests/authoring/a4 tests/authoring/a5 tests/authoring/a6
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  release-lifecycle)
    npm --prefix apps/capability-studio ci
    npm --prefix apps/capability-studio run typecheck
    npm --prefix apps/capability-studio test
    npm --prefix apps/capability-studio run build
    uv run --locked pytest -q \
      tests/authoring/a1 tests/authoring/a2 tests/authoring/a3 \
      tests/authoring/a4 tests/authoring/a5 tests/authoring/a6 tests/authoring/a7
    uv run --locked ruff check "${lint_paths[@]}"
    uv run --locked ruff format --check "${lint_paths[@]}"
    ;;
  *)
    echo "unknown authoring phase: $phase" >&2
    exit 2
    ;;
esac
