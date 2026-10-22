#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
version="$(python - <<'PY'
import tomllib
from pathlib import Path

print(tomllib.loads(Path("runtimes/cwl/pyproject.toml").read_text())["project"]["version"])
PY
)"
staging=".p4-runtime/image"
rm -rf -- "$staging"
mkdir -p "$staging/wheels"
uv export --project runtimes/cwl --locked --no-dev --no-emit-project --format requirements-txt --output-file "$staging/requirements.txt" > /dev/null
uvx --from pip pip download --only-binary=:all: --no-deps --require-hashes --index-url https://pypi.org/simple -r "$staging/requirements.txt" -d "$staging/wheels"
cp runtimes/cwl/engine.py runtimes/cwl/bridge.py deploy/p4/cwl.Dockerfile "$staging/"
cp packages/contracts/src/platform_contracts/engine_runtime.py "$staging/engine_profile.py"
docker build -f "$staging/cwl.Dockerfile" -t "groundwater-v7-cwl:$version" "$staging"
docker image inspect "groundwater-v7-cwl:$version" --format '{{.Id}}' > .p4-runtime/cwl-image.txt
