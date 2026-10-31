from pathlib import Path

from scripts.verification.probe_p8_direct_acceptance import exact_operator_role_binding
from tests.pathing import SOURCE_ROOT


def test_direct_acceptance_probe_builds_an_exact_manifest_backed_role_binding():
    digest = "sha256:" + "a" * 64
    ref = {"asset_id": "result-asset", "version_id": "1"}
    execution = {
        "action_id": "action-1",
        "execution_id": "execution-1",
        "request": {"target": {"operator_id": "timeseries-completeness"}},
        "runtime": {"publication_receipt": {"asset": ref}},
    }
    asset = {
        "manifest": {
            "representations": [
                {
                    "representation_id": "result",
                    "digest": digest,
                    "schema_id": "p3-result-v1",
                }
            ]
        }
    }

    assert exact_operator_role_binding(execution, asset) == {
        "role_id": "timeseries-completeness",
        "output_ref": {
            "kind": "OPERATOR_OUTPUT",
            "action_id": "action-1",
            "execution_id": "execution-1",
            "output_port": "result",
            "asset_ref": ref,
            "representation_id": "result",
            "representation_digest": digest,
            "schema_id": "p3-result-v1",
            "schema_source": "MANIFEST",
        },
    }


def test_p8_probes_record_the_current_deployment_identity_header():
    for name in (
        "probe_p8_compose_openapi.py",
        "probe_p8_direct_acceptance.py",
        "probe_p8_spatial_workflow.py",
    ):
        source = (Path(SOURCE_ROOT) / "scripts" / "verification" / name).read_text(encoding="utf-8")
        assert "X-V7-Deployment-Revision" in source
        assert "X-V7-Release-Digest" not in source
