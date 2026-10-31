#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"

if [[ "${1:-}" != "--phase" || $# -ne 2 || ! "${2:-}" =~ ^(runtime|kernel|model|readonly|actions|recovery|science|repair|ui|eval|all)$ ]]; then
  echo "usage: bash scripts/checks/check_p6.sh --phase runtime|kernel|model|readonly|actions|recovery|science|repair|ui|eval|all" >&2
  exit 2
fi

phase="$2"
verification="$V7_P6_RUNTIME_ROOT/verification"
release="$V7_P6_RUNTIME_ROOT/release"
mkdir -p "$verification" "$release"
export UV_LINK_MODE=copy

uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
if [[ "$phase" =~ ^(ui|all)$ ]]; then
  uv run --locked playwright install chromium > "$verification/browser-install.log" 2>&1
fi
if [[ "$phase" == runtime ]]; then
  uv run --locked pytest -q tests/agent/runtime tests/contracts tests/architecture --tb=short \
    --junitxml="$verification/p60-tests.xml"
elif [[ "$phase" == kernel ]]; then
  uv run --locked pytest -q tests/agent/kernel tests/agent/runtime tests/contracts tests/architecture tests/platform/database \
    --tb=short --junitxml="$verification/p61-tests.xml"
elif [[ "$phase" == model ]]; then
  uv run --locked pytest -q tests/agent/model tests/agent/kernel tests/agent/runtime tests/contracts tests/architecture tests/platform/database \
    --tb=short --junitxml="$verification/p62-tests.xml"
elif [[ "$phase" == readonly ]]; then
  uv run --locked pytest -q tests/agent/readonly tests/agent/model tests/agent/kernel tests/agent/runtime tests/contracts \
    tests/architecture tests/platform/database --tb=short --junitxml="$verification/p63-tests.xml"
elif [[ "$phase" == actions ]]; then
  uv run --locked pytest -q tests/agent/control tests/agent/readonly tests/agent/model tests/agent/kernel tests/agent/runtime tests/contracts \
    tests/architecture tests/platform/database --tb=short --junitxml="$verification/p64-tests.xml"
elif [[ "$phase" == recovery ]]; then
  uv run --locked pytest -q tests/agent/recovery tests/agent/control tests/agent/kernel tests/contracts tests/architecture \
    --tb=short --junitxml="$verification/p65-tests.xml"
elif [[ "$phase" == science ]]; then
  uv run --locked pytest -q tests/agent/science tests/agent/recovery tests/agent/control tests/contracts tests/architecture \
    --tb=short --junitxml="$verification/p66-tests.xml"
elif [[ "$phase" == repair ]]; then
  uv run --locked pytest -q tests/agent/repair tests/agent/science tests/agent/recovery tests/agent/control tests/contracts \
    --tb=short --junitxml="$verification/p67-tests.xml"
elif [[ "$phase" == ui ]]; then
  uv run --locked pytest -q tests/agent/ui tests/agent/repair tests/agent/recovery tests/contracts \
    --tb=short --junitxml="$verification/p68-tests.xml"
elif [[ "$phase" == eval ]]; then
  uv run --locked pytest -q tests/agent/evaluation tests/agent/ui tests/agent/repair tests/agent/science tests/agent/recovery tests/agent/control \
    tests/knowledge/research/test_contracts_and_policy.py::test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work \
    tests/knowledge/acceptance/test_migrations_and_lifecycle.py::test_withdrawal_marks_current_validity_stale_without_rewriting_report \
    tests/knowledge/identity/test_exact_candidate.py::test_direct_output_is_canonicalized_and_saved_as_exact \
    --tb=short --junitxml="$verification/p69-tests.xml"
  uv run --locked python scripts/evaluation/run_p6_golden.py \
    --junit "$verification/p69-golden-tests.xml" --repeats 2
  uv run --locked python scripts/evaluation/evaluate_p6.py --profile deterministic \
    --junit "$verification/p69-tests.xml" \
    --junit "$verification/p69-golden-tests-r1.xml" \
    --junit "$verification/p69-golden-tests-r2.xml" \
    --required-repeats 3 \
    --output "$verification/p69-deterministic-eval.json"
  uv run --locked python scripts/evaluation/evaluate_p6.py --profile real \
    --output "$verification/p69-real-model-eval.json"
elif [[ "$phase" == all ]]; then
  uv run --locked pytest -q tests/agent/runtime tests/agent/kernel tests/agent/model tests/agent/readonly tests/agent/control tests/agent/recovery \
    tests/agent/science tests/agent/repair tests/agent/ui tests/agent/evaluation tests/contracts tests/architecture \
    tests/knowledge/research/test_contracts_and_policy.py::test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work \
    tests/knowledge/acceptance/test_migrations_and_lifecycle.py::test_withdrawal_marks_current_validity_stale_without_rewriting_report \
    tests/knowledge/identity/test_exact_candidate.py::test_direct_output_is_canonicalized_and_saved_as_exact \
    --tb=short --junitxml="$verification/p69-tests.xml"
  uv run --locked python scripts/evaluation/run_p6_golden.py \
    --junit "$verification/p69-golden-tests.xml" --repeats 2
  uv run --locked python scripts/evaluation/evaluate_p6.py --profile deterministic \
    --junit "$verification/p69-tests.xml" \
    --junit "$verification/p69-golden-tests-r1.xml" \
    --junit "$verification/p69-golden-tests-r2.xml" \
    --required-repeats 3 \
    --output "$verification/p69-deterministic-eval.json"
  uv run --locked python scripts/evaluation/evaluate_p6.py --profile real \
    --output "$verification/p69-real-model-eval.json"
fi

rm -rf "$release/dist"
mkdir -p "$release/dist"
uv build --all-packages --no-build-isolation --out-dir "$release/dist" \
  > "$verification/release-build.log" 2>&1
uv run --locked agent-runtime-probe \
  | tee "$verification/runtime-profiles.json"
uv run --locked agent-tool-catalog generate --output "$V7_P6_RUNTIME_ROOT/agent-tool-catalog.json" \
  --stage "$([[ "$phase" == readonly ]] && echo readonly || ([[ "$phase" == actions ]] && echo actions || echo science))" \
  | tee "$verification/tool-catalog.json"
uv run --locked agent-tool-catalog check --output "$V7_P6_RUNTIME_ROOT/agent-tool-catalog.json"

if [[ "$phase" == all ]]; then
  uv run --locked pytest -q tests/integration/test_api.py --tb=short \
    --junitxml="$verification/p69-api-tests.xml"
  uv run --locked python -m scripts.checks.verify_p6_runtime_distribution \
    | tee "$verification/distribution.log"
fi
printf '%s\n' "PASS: P6 results are available under $verification"
