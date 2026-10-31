#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"

phase="${2:-catalog}"
if [[ "${1:-}" == "--phase" ]]; then
  phase="${2:?phase is required}"
fi

case "$phase" in
  tabular)
    bash scripts/build/build_p71a_image.sh
    uv run --locked pytest -q tests/operators/tabular/core
    ;;
  tabular-acceptance)
    bash scripts/build/build_p3_images.sh
    bash scripts/build/build_p5_checker_images.sh
    bash scripts/build/build_p71a_image.sh
    uv run --locked pytest -q tests/operators/tabular/acceptance
    ;;
  table-project)
    bash scripts/build/build_p3_images.sh
    bash scripts/build/build_p5_checker_images.sh
    bash scripts/build/build_p71a_image.sh
    uv run --locked pytest -q tests/operators/project
    ;;
  table-filter)
    bash scripts/build/build_p3_images.sh
    bash scripts/build/build_p5_checker_images.sh
    bash scripts/build/build_p71a_image.sh
    uv run --locked pytest -q tests/operators/filters
    ;;
  table-cast)
    bash scripts/build/build_p3_images.sh
    bash scripts/build/build_p5_checker_images.sh
    bash scripts/build/build_p71a_image.sh
    uv run --locked pytest -q tests/operators/cast
    ;;
  table-aggregate)
    bash scripts/build/build_p3_images.sh
    bash scripts/build/build_p5_checker_images.sh
    bash scripts/build/build_p71a_image.sh
    uv run --locked pytest -q tests/operators/aggregate
    ;;
  catalog)
    uv run --locked pytest -q tests/operators/catalog
    uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
    uv run --locked ruff check .
    uv run --locked ruff format --check .
    ;;
  compatibility)
    uv run --locked pytest -q \
      tests/contracts tests/integration tests/architecture tests/platform/database tests/platform/services \
      tests/execution/workflows tests/spatial/native tests/spatial/cartography tests/spatial/remote tests/spatial/reliability tests/spatial/mixed_workflows \
      tests/knowledge/core tests/knowledge/research tests/knowledge/acceptance tests/knowledge/identity tests/knowledge/checkers tests/knowledge/reliability \
      tests/agent/runtime tests/agent/kernel tests/agent/model tests/agent/readonly tests/agent/control tests/agent/recovery \
      tests/agent/science tests/agent/repair tests/agent/ui tests/agent/evaluation tests/agent/embeddings \
      tests/operators/catalog tests/operators/tabular/core tests/operators/tabular/acceptance tests/operators/project tests/operators/filters tests/operators/cast tests/operators/aggregate
    ;;
  *)
    echo "unknown phase: $phase" >&2
    exit 2
    ;;
esac
