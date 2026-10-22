#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
mkdir -p .p3-runtime/images/wheels
rm -f .p3-runtime/images/wheels/groundwater_operator_pack-*.whl \
  .p3-runtime/images/wheels/groundwater_checker_pack-*.whl
uv build --package groundwater-operator-pack --wheel --out-dir .p3-runtime/images/wheels
uv build --package groundwater-checker-pack --wheel --out-dir .p3-runtime/images/wheels
uv export --locked --package groundwater-operator-pack --no-dev --no-emit-workspace --format requirements-txt --output-file .p3-runtime/images/requirements.txt > /dev/null
uvx --from pip pip download --only-binary=:all: --no-deps --require-hashes --index-url https://pypi.org/simple -r .p3-runtime/images/requirements.txt -d .p3-runtime/images/wheels
cp deploy/p3/*.Dockerfile .p3-runtime/images/
SOURCE_DIGEST="$(uv run --locked python scripts/release_source_manifest.py --digest-only)"
docker build --build-arg "V7_RELEASE_SOURCE_DIGEST=$SOURCE_DIGEST" --build-arg "V7_VERSION=$VERSION" \
  -f .p3-runtime/images/operator.Dockerfile -t "groundwater-v7-operator:$VERSION" .p3-runtime/images
docker build --build-arg "V7_RELEASE_SOURCE_DIGEST=$SOURCE_DIGEST" --build-arg "V7_VERSION=$VERSION" \
  -f .p3-runtime/images/checker.Dockerfile -t "groundwater-v7-checker:$VERSION" .p3-runtime/images
# Keep the delivered P3 tag as a compatibility alias for historical fault fixtures.
docker tag "groundwater-v7-operator:$VERSION" groundwater-v7-operator:0.2.1
docker tag "groundwater-v7-checker:$VERSION" groundwater-v7-checker:0.2.1
V7_BUILD_VERSION="$VERSION" uv run python - <<'PY'
import json, subprocess
import os
from pathlib import Path
version = os.environ['V7_BUILD_VERSION']
images = {kind: subprocess.check_output(['docker', 'image', 'inspect',
          'groundwater-v7-' + kind + ':' + version, '--format', '{{.Id}}'], text=True).strip()
          for kind in ['operator', 'checker']}
Path('.p3-runtime/images.json').write_text(json.dumps(images, indent=2) + '\n')
PY
