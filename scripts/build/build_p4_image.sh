#!/usr/bin/env bash
set -euo pipefail
V7_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$V7_SOURCE_ROOT"
source "$V7_SOURCE_ROOT/scripts/lib/runtime-layout.sh"
version="$(python - <<'PY'
import tomllib
from pathlib import Path

print(tomllib.loads(Path("runtimes/cwl/pyproject.toml").read_text())["project"]["version"])
PY
)"
staging="${V7_P4_RUNTIME_ROOT}/image"
rm -rf -- "$staging"
mkdir -p "$staging/wheels"
uv export --project runtimes/cwl --locked --no-dev --no-emit-project --format requirements-txt --output-file "$staging/requirements.txt" > /dev/null
uvx --from pip pip download --only-binary=:all: --no-deps --require-hashes --index-url https://pypi.org/simple -r "$staging/requirements.txt" -d "$staging/wheels"
cp runtimes/cwl/engine.py runtimes/cwl/bridge.py deploy/execution/cwl.Dockerfile "$staging/"
cp packages/contracts/src/platform_contracts/engine_runtime.py "$staging/engine_profile.py"
docker build -f "$staging/cwl.Dockerfile" -t "groundwater-v7-cwl:$version" "$staging"
docker image inspect "groundwater-v7-cwl:$version" --format '{{.Id}}' > ${V7_P4_RUNTIME_ROOT}/cwl-image.txt
