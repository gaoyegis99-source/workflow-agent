#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [[ "${1:-}" != "--phase" || $# -ne 2 || ! "${2:-}" =~ ^(runtime|kernel|model|readonly|actions|recovery|science|repair|ui|eval|all)$ ]]; then
  echo "usage: bash scripts/check_p6.sh --phase runtime|kernel|model|readonly|actions|recovery|science|repair|ui|eval|all" >&2
  exit 2
fi

phase="$2"
verification=.p6-runtime/verification
release=.p6-runtime/release
mkdir -p "$verification" "$release"
export UV_LINK_MODE=copy

uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
if [[ "$phase" =~ ^(ui|all)$ ]]; then
  uv run --locked playwright install chromium > "$verification/browser-install.log" 2>&1
fi
if [[ "$phase" == runtime ]]; then
  uv run --locked pytest -q tests/p60 tests/contracts tests/architecture --tb=short \
    --junitxml="$verification/p60-tests.xml"
elif [[ "$phase" == kernel ]]; then
  uv run --locked pytest -q tests/p61 tests/p60 tests/contracts tests/architecture tests/p2 \
    --tb=short --junitxml="$verification/p61-tests.xml"
elif [[ "$phase" == model ]]; then
  uv run --locked pytest -q tests/p62 tests/p61 tests/p60 tests/contracts tests/architecture tests/p2 \
    --tb=short --junitxml="$verification/p62-tests.xml"
elif [[ "$phase" == readonly ]]; then
  uv run --locked pytest -q tests/p63 tests/p62 tests/p61 tests/p60 tests/contracts \
    tests/architecture tests/p2 --tb=short --junitxml="$verification/p63-tests.xml"
elif [[ "$phase" == actions ]]; then
  uv run --locked pytest -q tests/p64 tests/p63 tests/p62 tests/p61 tests/p60 tests/contracts \
    tests/architecture tests/p2 --tb=short --junitxml="$verification/p64-tests.xml"
elif [[ "$phase" == recovery ]]; then
  uv run --locked pytest -q tests/p65 tests/p64 tests/p61 tests/contracts tests/architecture \
    --tb=short --junitxml="$verification/p65-tests.xml"
elif [[ "$phase" == science ]]; then
  uv run --locked pytest -q tests/p66 tests/p65 tests/p64 tests/contracts tests/architecture \
    --tb=short --junitxml="$verification/p66-tests.xml"
elif [[ "$phase" == repair ]]; then
  uv run --locked pytest -q tests/p67 tests/p66 tests/p65 tests/p64 tests/contracts \
    --tb=short --junitxml="$verification/p67-tests.xml"
elif [[ "$phase" == ui ]]; then
  uv run --locked pytest -q tests/p68 tests/p67 tests/p65 tests/contracts \
    --tb=short --junitxml="$verification/p68-tests.xml"
elif [[ "$phase" == eval ]]; then
  uv run --locked pytest -q tests/p69 tests/p68 tests/p67 tests/p66 tests/p65 tests/p64 \
    tests/p52/test_contracts_and_policy.py::test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work \
    tests/p53/test_migrations_and_lifecycle.py::test_withdrawal_marks_current_validity_stale_without_rewriting_report \
    tests/p5r1/test_exact_candidate.py::test_direct_output_is_canonicalized_and_saved_as_exact \
    --tb=short --junitxml="$verification/p69-tests.xml"
  uv run --locked python scripts/run_p6_golden.py \
    --junit "$verification/p69-golden-tests.xml" --repeats 2
  uv run --locked python scripts/evaluate_p6.py --profile deterministic \
    --junit "$verification/p69-tests.xml" \
    --junit "$verification/p69-golden-tests-r1.xml" \
    --junit "$verification/p69-golden-tests-r2.xml" \
    --required-repeats 3 \
    --output "$verification/p69-deterministic-eval.json"
  uv run --locked python scripts/evaluate_p6.py --profile real \
    --output "$verification/p69-real-model-eval.json"
elif [[ "$phase" == all ]]; then
  uv run --locked pytest -q tests/p60 tests/p61 tests/p62 tests/p63 tests/p64 tests/p65 \
    tests/p66 tests/p67 tests/p68 tests/p69 tests/contracts tests/architecture \
    tests/p52/test_contracts_and_policy.py::test_groundwater_policy_is_local_deterministic_and_does_not_block_unrelated_work \
    tests/p53/test_migrations_and_lifecycle.py::test_withdrawal_marks_current_validity_stale_without_rewriting_report \
    tests/p5r1/test_exact_candidate.py::test_direct_output_is_canonicalized_and_saved_as_exact \
    --tb=short --junitxml="$verification/p69-tests.xml"
  uv run --locked python scripts/run_p6_golden.py \
    --junit "$verification/p69-golden-tests.xml" --repeats 2
  uv run --locked python scripts/evaluate_p6.py --profile deterministic \
    --junit "$verification/p69-tests.xml" \
    --junit "$verification/p69-golden-tests-r1.xml" \
    --junit "$verification/p69-golden-tests-r2.xml" \
    --required-repeats 3 \
    --output "$verification/p69-deterministic-eval.json"
  uv run --locked python scripts/evaluate_p6.py --profile real \
    --output "$verification/p69-real-model-eval.json"
fi

rm -rf "$release/dist"
mkdir -p "$release/dist"
uv build --all-packages --no-build-isolation --out-dir "$release/dist" \
  > "$verification/release-build.log" 2>&1
uv run --locked agent-runtime-probe \
  | tee "$verification/runtime-profiles.json"
uv run --locked agent-tool-catalog generate --output .p6-runtime/agent-tool-catalog.json \
  --stage "$([[ "$phase" == readonly ]] && echo readonly || ([[ "$phase" == actions ]] && echo actions || echo science))" \
  | tee "$verification/tool-catalog.json"
uv run --locked agent-tool-catalog check --output .p6-runtime/agent-tool-catalog.json

if [[ "$phase" == all ]]; then
  uv run --locked pytest -q tests/integration/test_api.py --tb=short \
    --junitxml="$verification/p69-api-tests.xml"
  uv run --locked python scripts/verify_p6_runtime_distribution.py \
    | tee "$verification/distribution.log"
  uv run --locked python scripts/record_p69.py
fi
