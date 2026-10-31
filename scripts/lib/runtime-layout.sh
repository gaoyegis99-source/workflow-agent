#!/usr/bin/env bash
# One project-local Python resolver owns writable paths and Docker selection.
# --testing strips inherited deployment credentials BEFORE restoring owned paths.
_v7_environment_module="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../maintenance" && pwd)/project_environment.py"
_v7_environment_options=()
if [[ "${1:-}" == "--testing" ]]; then
  _v7_environment_options+=(--testing)
fi
if ! _v7_environment_exports="$(python3 -B "$_v7_environment_module" shell "${_v7_environment_options[@]}")"; then
  unset _v7_environment_module _v7_environment_options _v7_environment_exports
  return 75
fi
eval "$_v7_environment_exports"
unset _v7_environment_module _v7_environment_options _v7_environment_exports

# P8 uses explicit direct-Compose configuration.  Historic release-identity
# observations stay in archived evidence, never as a runtime command gate.
