import binascii
import hashlib
import json
import struct
import zlib
from copy import deepcopy

from platform_checkers.capabilities import check
from platform_contracts.service import digest
from platform_core.application.checker_catalog import build_catalog


def chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png(colored=True):
    width, height = 1754, 1240
    white = bytearray(b"\xff\xff\xff" * width)
    if colored:
        for x in range(100, 130):
            white[x * 3 : x * 3 + 3] = bytes((76, 120, 168))
    raw = b"".join(b"\x00" + bytes(white) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def fixture(tmp_path, colored=True):
    role = tmp_path / "roles/map"
    role.mkdir(parents=True)
    image = png(colored)
    image_digest = "sha256:" + hashlib.sha256(image).hexdigest()
    source_digest = "sha256:" + "a" * 64
    template_digest = "sha256:" + "b" * 64
    manifest = {
        "kind": "CARTOGRAPHIC_PRODUCT",
        "source": {"digest": source_digest},
        "template": {"template_digest": template_digest},
        "cartography": {"palette": "blue", "legend": True},
        "image": {"digest": image_digest, "width": 1754, "height": 1240},
    }
    method = {"template_digest": template_digest, "source_digest": source_digest}
    contents = {
        "image": image,
        "manifest": json.dumps(manifest).encode(),
        "method": json.dumps(method).encode(),
    }
    files = {}
    for identity, content in contents.items():
        path = role / f"{identity}.bin"
        path.write_bytes(content)
        files[identity] = {
            "path": f"roles/map/{identity}.bin",
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
    capability = next(
        value
        for value in build_catalog()["capabilities"]
        if value["capability_id"] == "cartographic-product-integrity-v1"
    )
    item = {
        "item_id": "coverage-0000",
        "check_type": "DATA_VALIDITY",
        "role_id": "map",
        "coverage_status": "COVERED",
        "capability_id": capability["capability_id"],
    }
    frozen = {
        "run_key": "check-run-00",
        "capability_id": capability["capability_id"],
        "capability_version": capability["version"],
        "capability_digest": capability["capability_digest"],
        "implementation_id": capability["implementation_id"],
        "image_digest": capability["image_digest"],
        "input_contract_digest": capability["input_contract_digest"],
        "output_contract_digest": capability["output_contract_digest"],
        "item_ids": [item["item_id"]],
        "input_roles": ["map"],
        "claim_revision_ids": [],
        "depends_on": [],
    }
    plan = {"schema_version": 3, "items": [item], "checker_runs": [frozen]}
    plan["plan_digest"] = digest(plan)
    request = {"coverage_plan": plan, "checker_run": frozen | {"run_id": "map-run"}}
    index = {
        "roles": {
            "map": {
                "files": files,
                "manifest": {
                    "cartography": manifest["cartography"],
                    "template": manifest["template"],
                    "image": manifest["image"],
                },
                "source_spatial": {
                    "selection": {
                        "asset": {"asset_id": "source"},
                        "representation_digest": source_digest,
                    },
                    "manifest": {"spatial": {"layers": [{"layer_id": "l0"}]}},
                },
            }
        }
    }
    (tmp_path / "input-index.json").write_text(json.dumps(index))
    return request


def test_map_checker_rejects_blank_map_and_false_source(tmp_path):
    request = fixture(tmp_path)
    assert check(request, tmp_path)["status"] == "PASS"

    blank = tmp_path / "blank"
    assert check(fixture(blank, colored=False), blank)["status"] == "FAIL"

    index = json.loads((tmp_path / "input-index.json").read_text())
    index["roles"]["map"]["source_spatial"]["selection"]["representation_digest"] = (
        "sha256:" + "c" * 64
    )
    (tmp_path / "input-index.json").write_text(json.dumps(index))
    assert check(deepcopy(request), tmp_path)["status"] == "FAIL"
