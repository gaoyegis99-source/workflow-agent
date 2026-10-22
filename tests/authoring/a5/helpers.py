from platform_core.application.catalog import build_catalog


def target(operator_id="table-profile"):
    operator = next(
        item for item in build_catalog()["operators"] if item["operator_id"] == operator_id
    )
    implementation = next(
        item for item in operator["implementations"] if item["binding_kind"] == "LOCAL"
    )
    return {
        "kind": "OPERATOR",
        "operator_id": operator_id,
        "version_id": operator["version_id"],
        "execution_binding": {
            "kind": "LOCAL",
            "implementation_id": implementation["implementation_id"],
        },
    }


def advanced_source(method="dotproduct"):
    scatter = "table" if method == "dotproduct" else ["table", "tag"]
    inputs = {
        "tables": {"type": {"type": "array", "items": "File"}},
        "enabled": "boolean",
    }
    bindings = {"table": "tables", "enabled": "enabled"}
    sub_inputs = {"table": "File", "enabled": "boolean"}
    if method != "dotproduct":
        inputs["tags"] = {"type": {"type": "array", "items": "string"}}
        bindings["tag"] = "tags"
        sub_inputs["tag"] = "string"
    item = ["null", "File"]
    if method == "nested_crossproduct":
        item = {"type": "array", "items": item}
    return {
        "profile": "v7-cwl-p4-v1",
        "name": "advanced-profile",
        "entrypoint": "main",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "requirements": {
                    "SubworkflowFeatureRequirement": {},
                    "ScatterFeatureRequirement": {},
                },
                "inputs": inputs,
                "outputs": {
                    "reports": {
                        "type": {"type": "array", "items": item},
                        "outputSource": "each/report",
                    }
                },
                "steps": {
                    "each": {
                        "run": "sub",
                        "in": bindings,
                        "out": ["report"],
                        "scatter": scatter,
                        "scatterMethod": method,
                        "when": "$(inputs.enabled)",
                    }
                },
            },
            "sub": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": sub_inputs,
                "outputs": {"report": {"type": "File", "outputSource": "profile/report"}},
                "steps": {
                    "profile": {
                        "run": "profile_impl",
                        "in": {
                            "table": "table",
                            "parameters": {"default": {"fields": ["f0"]}},
                        },
                        "out": ["report", "table", "method"],
                    }
                },
            },
        },
        "implementations": {"profile_impl": target()},
    }
