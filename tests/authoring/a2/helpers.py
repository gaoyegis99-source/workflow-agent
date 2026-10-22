from copy import deepcopy

from platform_contracts.authoring import build_authoring_descriptor
from platform_core.application.catalog import build_catalog


def descriptor():
    return build_authoring_descriptor(build_catalog())


def target(operator_id):
    item = next(
        value for value in build_catalog()["operators"] if value["operator_id"] == operator_id
    )
    implementation = next(
        value for value in item["implementations"] if value["binding_kind"] == "LOCAL"
    )
    return {
        "kind": "OPERATOR",
        "operator_id": operator_id,
        "version_id": item["version_id"],
        "execution_binding": {
            "kind": "LOCAL",
            "implementation_id": implementation["implementation_id"],
        },
    }


def profile_source(*, parameters=None):
    return {
        "profile": "v7-cwl-p4-v1",
        "name": "profile-workflow",
        "entrypoint": "main",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": {"table_input": "File"},
                "outputs": {"report": {"type": "File", "outputSource": "profile/report"}},
                "steps": {
                    "profile": {
                        "run": "profile_impl",
                        "in": {
                            "table": "table_input",
                            "parameters": {"default": parameters or {"fields": ["f0"]}},
                        },
                        "out": ["report", "table", "method"],
                    }
                },
            }
        },
        "implementations": {"profile_impl": target("table-profile")},
    }


def resolved_table(*fields):
    return {
        "table": {
            "row_count": 2,
            "fields": [
                {"field_id": field_id, "name": name, "type": kind}
                for field_id, name, kind in fields
            ],
        },
        "manifest": {},
    }


def clone(value):
    return deepcopy(value)
