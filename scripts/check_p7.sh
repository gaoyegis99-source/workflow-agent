#!/usr/bin/env bash
set -euo pipefail

phase="${2:-catalog}"
if [[ "${1:-}" == "--phase" ]]; then
  phase="${2:?phase is required}"
fi

case "$phase" in
  tabular)
    bash scripts/build_p71a_image.sh
    uv run --locked pytest -q tests/p71a
    ;;
  tabular-acceptance)
    bash scripts/build_p3_images.sh
    bash scripts/build_p5_checker_images.sh
    bash scripts/build_p71a_image.sh
    uv run --locked pytest -q tests/p71b
    ;;
  table-project)
    bash scripts/build_p3_images.sh
    bash scripts/build_p5_checker_images.sh
    bash scripts/build_p71a_image.sh
    uv run --locked pytest -q tests/p72a
    ;;
  table-filter)
    bash scripts/build_p3_images.sh
    bash scripts/build_p5_checker_images.sh
    bash scripts/build_p71a_image.sh
    uv run --locked pytest -q tests/p72b
    ;;
  table-cast)
    bash scripts/build_p3_images.sh
    bash scripts/build_p5_checker_images.sh
    bash scripts/build_p71a_image.sh
    uv run --locked pytest -q tests/p72c
    ;;
  table-aggregate)
    bash scripts/build_p3_images.sh
    bash scripts/build_p5_checker_images.sh
    bash scripts/build_p71a_image.sh
    uv run --locked pytest -q tests/p73a
    ;;
  catalog)
    uv run --locked pytest -q tests/p70
    uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
    uv run --locked ruff check .
    uv run --locked ruff format --check .
    ;;
  compatibility)
    uv run --locked pytest -q \
      tests/contracts tests/integration tests/architecture tests/p2 tests/p3 \
      tests/p4 tests/p42 tests/p422 tests/p43 tests/p431a tests/p432 \
      tests/p51 tests/p52 tests/p53 tests/p5r1 tests/p5r2 tests/p5r21 \
      tests/p60 tests/p61 tests/p62 tests/p63 tests/p64 tests/p65 \
      tests/p66 tests/p67 tests/p68 tests/p69 tests/p610 \
      tests/p70 tests/p71a tests/p71b tests/p72a tests/p72b tests/p72c tests/p73a
    ;;
  *)
    echo "unknown phase: $phase" >&2
    exit 2
    ;;
esac
