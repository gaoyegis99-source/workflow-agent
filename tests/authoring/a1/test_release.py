import json

import pytest
from platform_contracts import ReleaseContext
from platform_contracts.release import digest, file_digest, production_release
from platform_core.application.catalog import build_catalog


def test_production_rejects_missing_wheel_evidence(tmp_path, monkeypatch):
    catalog = build_catalog()
    catalog_path = tmp_path / "operators.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    unsigned = {
        "schema_version": 2,
        "version": "0.14.0",
        "operator_catalog": {
            "digest": catalog["catalog_digest"],
            "file_sha256": file_digest(catalog_path),
        },
        "images": {},
        "source_tree": {"digest": "sha256:" + "0" * 64, "file_count": 0},
        "wheels": {},
    }
    release = unsigned | {"release_digest": digest(unsigned)}
    lock = tmp_path / "release.json"
    lock.write_text(json.dumps(release), encoding="utf-8")
    with pytest.raises(ValueError, match="wheel evidence"):
        production_release(
            release=ReleaseContext(
                deployment_mode="production",
                release_lock_path=lock,
                release_digest=release["release_digest"],
            ),
            operator_path=catalog_path,
            operator_catalog=catalog,
            version="0.14.0",
        )


def test_tampered_release_lock_is_rejected(tmp_path, monkeypatch):
    lock = tmp_path / "release.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "version": "0.14.0",
                "wheels": {"forged.whl": "sha256:" + "1" * 64},
                "release_digest": "sha256:" + "2" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="digest differs"):
        production_release(
            release=ReleaseContext(
                deployment_mode="production",
                release_lock_path=lock,
                release_digest="sha256:" + "2" * 64,
            ),
            operator_path=lock,
            operator_catalog={"catalog_digest": "sha256:" + "0" * 64},
            version="0.14.0",
        )
