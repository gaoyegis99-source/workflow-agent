"""Pure compiler for the bounded CWL profile. Only generated tools contain code.

Scatter inputs are tagged with their ordinal before the engine expands them. The
ordinal travels as data through every subworkflow, independent of values or job names.
"""

import hashlib
import json
import re

from platform_core.application.catalog import ports as catalog_ports
from platform_core.domain.control import require, valid_identifier

PROFILE = "v7-cwl-p4-v1"
CONTEXT = "v7_context"
COORDINATION_RESOURCES = {
    "coresMin": 0.2,
    "coresMax": 0.2,
    "ramMin": 128,
    "ramMax": 128,
}


def check_literal(value):
    """Defaults are data only; they may not smuggle a File or CWL directive."""
    if isinstance(value, dict):
        require(
            not any(str(key).startswith("$") for key in value)
            and value.get("class") != "File"
            and not ({"location", "path"} & set(value)),
            "UNSUPPORTED_TARGET",
            "Defaults cannot reference files or CWL directives",
            422,
        )
        for item in value.values():
            check_literal(item)
    elif isinstance(value, list):
        for item in value:
            check_literal(item)
    else:
        require(
            value is None or type(value) in {bool, int, float, str},
            "MALFORMED_REQUEST",
            "Default is not JSON data",
            422,
        )


def short_type(value):
    if isinstance(value, dict) and "type" in value:
        kind = value["type"]
        return value if isinstance(kind, str) and kind in {"array", "record"} else kind
    return value


def check_type(value):
    if isinstance(value, str):
        require(
            value in {"null", "boolean", "int", "long", "float", "double", "string", "File"},
            "UNSUPPORTED_TARGET",
            "Unsupported CWL type",
            422,
        )
    elif isinstance(value, list):
        require(
            len(value) == 2 and "null" in value,
            "UNSUPPORTED_TARGET",
            "Only nullable unions are supported",
            422,
        )
        for item in value:
            check_type(item)
    elif isinstance(value, dict):
        kind = value.get("type")
        require(kind in {"array", "record"}, "UNSUPPORTED_TARGET", "Unsupported compound type", 422)
        if kind == "array":
            require(
                set(value) <= {"type", "items"},
                "UNSUPPORTED_TARGET",
                "Unknown array definition",
                422,
            )
            check_type(value["items"])
        else:
            require(
                set(value) <= {"type", "name", "fields"} and isinstance(value.get("fields"), list),
                "UNSUPPORTED_TARGET",
                "Invalid record",
                422,
            )
            names = []
            for field in value["fields"]:
                require(
                    set(field) <= {"name", "type", "default"}
                    and valid_identifier(field.get("name")),
                    "UNSUPPORTED_TARGET",
                    "Invalid record field",
                    422,
                )
                names.append(field["name"])
                check_type(field["type"])
                if "default" in field:
                    check_literal(field["default"])
            require(
                len(names) == len(set(names)), "INVALID_BINDING", "Duplicate record fields", 422
            )
    else:
        require(False, "MALFORMED_REQUEST", "Invalid type", 422)


def signature(document, key):
    values = document.get(key, {})
    require(isinstance(values, dict), "MALFORMED_REQUEST", "Named CWL ports required", 422)
    for name, value in values.items():
        require(
            valid_identifier(name) and not name.startswith("v7_"),
            "INVALID_BINDING",
            "Invalid or reserved port",
            422,
        )
        check_type(short_type(value))
        if isinstance(value, dict) and "default" in value:
            check_literal(value["default"])
    return values


def compile_package(request, *, catalog=None, resolver=None, bridge_command=None):
    """Compile one package against an explicitly selected operator catalogue.

    Tests for the compiler's graph mechanics may inject a narrow ``resolver``.
    Every real catalogue-backed caller must instead pass ``catalog``; the core
    never reads deployment configuration or chooses a catalogue implicitly.
    """

    require(
        (catalog is None) != (resolver is None),
        "INVALID_BINDING",
        "Provide exactly one operator catalog or resolver",
        422,
    )
    if resolver is None:

        def resolve_from_catalog(target):
            return catalog_ports(target, catalog)

        resolver = resolve_from_catalog
    require(
        request.get("profile", PROFILE) == PROFILE,
        "UNSUPPORTED_TARGET",
        "Unsupported CWL profile",
        422,
    )
    require(
        len(json.dumps(request)) <= 1024 * 1024,
        "MALFORMED_REQUEST",
        "Workflow package exceeds 1 MiB",
        422,
    )
    documents, implementations = request["documents"], request["implementations"]
    require(
        all(valid_identifier(k) and not k.startswith("v7_") for k in documents | implementations),
        "INVALID_BINDING",
        "Invalid package member identity",
        422,
    )
    require(
        not set(documents) & set(implementations),
        "INVALID_BINDING",
        "Ambiguous package reference",
        422,
    )
    files, invocations = {}, {}
    command = bridge_command or ["python", "/opt/v7-cwl/bridge.py"]

    def emit(document, path, suffix=""):
        token = hashlib.sha256(json.dumps([path, suffix]).encode()).hexdigest()[:24]
        name = "v7_" + token + ".cwl"
        document = dict(document)
        for direction in ("inputs", "outputs"):
            document[direction] = {
                key: {"type": value}
                if isinstance(value, dict)
                and isinstance(value.get("type"), str)
                and value["type"] in {"array", "record"}
                else value
                for key, value in document.get(direction, {}).items()
            }
        files[name] = {"cwlVersion": "v1.2", **document}
        return name

    def build(reference, path, stack):
        require(
            len(path) <= 8 and reference not in stack,
            "INVALID_BINDING",
            "Recursive or overdeep workflow",
            422,
        )
        if reference in implementations:
            target = implementations[reference]
            inputs, outputs = resolver(target)
            invocation = "/".join(path)
            invocations[invocation] = {"target": target, "inputs": inputs, "outputs": outputs}
            tool = {
                "class": "CommandLineTool",
                "inputs": inputs | {CONTEXT: "string"},
                "outputs": {key: {"type": value} for key, value in outputs.items()},
                "baseCommand": command,
                "arguments": ["--path", json.dumps(path), "--request", "request.json"],
                "requirements": {
                    "InlineJavascriptRequirement": {},
                    "ResourceRequirement": COORDINATION_RESOURCES,
                    "InitialWorkDirRequirement": {
                        "listing": [
                            {"entryname": "request.json", "entry": "$(JSON.stringify(inputs))"}
                        ]
                    },
                },
            }
            return emit(tool, path), inputs, outputs
        require(reference in documents, "INVALID_BINDING", "Unresolved local workflow", 422)
        source = documents[reference]
        require(
            set(source)
            <= {"cwlVersion", "class", "inputs", "outputs", "steps", "label", "doc", "requirements"}
            and source.get("class") == "Workflow"
            and source.get("cwlVersion", "v1.2") == "v1.2",
            "UNSUPPORTED_TARGET",
            "Only bounded CWL Workflow documents are accepted",
            422,
        )
        allowed = {
            "SubworkflowFeatureRequirement",
            "ScatterFeatureRequirement",
            "MultipleInputFeatureRequirement",
        }
        requirements = source.get("requirements", {})
        require(
            isinstance(requirements, dict)
            and set(requirements) <= allowed
            and all(value == {} for value in requirements.values()),
            "UNSUPPORTED_TARGET",
            "User executable requirements are not supported",
            422,
        )
        ins, outs = signature(source, "inputs"), signature(source, "outputs")
        require(bool(outs), "INVALID_BINDING", "Workflow needs a named output", 422)
        steps = source.get("steps", {})
        require(
            isinstance(steps, dict) and 0 < len(steps) <= 1000,
            "INVALID_BINDING",
            "Workflow needs 1 to 1000 steps",
            422,
        )
        output = {
            "class": "Workflow",
            "inputs": ins | {CONTEXT: "string"},
            "outputs": outs,
            "requirements": {
                name: {}
                for name in allowed
                | {"StepInputExpressionRequirement", "InlineJavascriptRequirement"}
            },
            "steps": {},
        }
        for name, step in steps.items():
            require(
                valid_identifier(name) and not name.startswith("v7_") and isinstance(step, dict),
                "INVALID_BINDING",
                "Invalid step identity",
                422,
            )
            require(
                set(step)
                <= {"run", "in", "out", "scatter", "scatterMethod", "when", "label", "doc"},
                "UNSUPPORTED_TARGET",
                "Unsupported step feature",
                422,
            )
            require(
                isinstance(step.get("run"), str),
                "INVALID_BINDING",
                "Local run reference required",
                422,
            )
            run, runins, runouts = build(step["run"], [*path, name], [*stack, reference])
            bindings = step.get("in", {})
            require(
                isinstance(bindings, dict) and set(bindings) <= set(runins),
                "INVALID_BINDING",
                "Unknown step input",
                422,
            )
            for binding in bindings.values():
                if isinstance(binding, dict):
                    require(
                        set(binding) <= {"source", "default", "linkMerge", "pickValue"},
                        "UNSUPPORTED_TARGET",
                        "User expressions or hidden inputs are forbidden",
                        422,
                    )
                    if "default" in binding:
                        check_literal(binding["default"])
                    require(
                        binding.get("linkMerge") in {None, "merge_nested", "merge_flattened"}
                        and binding.get("pickValue")
                        in {None, "first_non_null", "the_only_non_null", "all_non_null"}
                        and not ({"linkMerge", "pickValue"} <= set(binding)),
                        "UNSUPPORTED_TARGET",
                        "Unsupported multiple-input merge semantics",
                        422,
                    )
            when = step.get("when")
            require(
                when is None
                or isinstance(when, str)
                and re.fullmatch(r"\$\(inputs\.[A-Za-z][A-Za-z0-9_]*\)", when),
                "UNSUPPORTED_TARGET",
                "Condition must reference a boolean input",
                422,
            )
            scatter = step.get("scatter", [])
            scatter = [scatter] if isinstance(scatter, str) else scatter
            require(
                isinstance(scatter, list)
                and len(scatter) == len(set(scatter))
                and set(scatter) <= set(bindings),
                "INVALID_BINDING",
                "Invalid scatter inputs",
                422,
            )
            require(
                step.get("scatterMethod", "dotproduct")
                in {"dotproduct", "flat_crossproduct", "nested_crossproduct"},
                "UNSUPPORTED_TARGET",
                "Unsupported scatter method",
                422,
            )
            actual = {
                "run": run,
                "in": bindings | {CONTEXT: CONTEXT},
                "out": step.get("out", list(runouts)),
            }
            if when:
                actual["when"] = when
            if scatter:
                # A trusted expression only tags values; Toil still owns expansion/scheduling.
                helper = "v7_tag_" + name
                helper_outputs, helper_inputs, jsfields = {}, {}, []
                for field in scatter:
                    envelope = {
                        "type": "record",
                        "name": "v7_Item_" + field,
                        "fields": [
                            {"name": "ordinal", "type": "int"},
                            {"name": "value", "type": "Any"},
                        ],
                    }
                    helper_inputs[field] = {"type": "array", "items": "Any"}
                    helper_outputs[field] = {"type": "array", "items": envelope}
                    jsfields.append(
                        json.dumps(field)
                        + ": inputs["
                        + json.dumps(field)
                        + "].map(function(v,i){return {ordinal:i,value:v};})"
                    )
                helper_inputs[CONTEXT] = "string"
                size = "*".join("inputs[" + json.dumps(f) + "].length" for f in scatter)
                if step.get("scatterMethod", "dotproduct") == "dotproduct":
                    size = "inputs[" + json.dumps(scatter[0]) + "].length"
                expression = (
                    "${if (("
                    + size
                    + ")>1000) throw new Error('SCATTER_LIMIT'); return {"
                    + ",".join(jsfields)
                    + "};}"
                )
                helperfile = emit(
                    {
                        "class": "ExpressionTool",
                        "inputs": helper_inputs,
                        "outputs": helper_outputs,
                        "requirements": {
                            "InlineJavascriptRequirement": {},
                            "ResourceRequirement": COORDINATION_RESOURCES,
                        },
                        "expression": expression,
                    },
                    [*path, name],
                    "tag",
                )
                output["steps"][helper] = {
                    "run": helperfile,
                    "in": {f: bindings[f] for f in scatter} | {CONTEXT: CONTEXT},
                    "out": scatter,
                }
                wrapinputs = dict(runins)
                wrapinputs.update({f: helper_outputs[f]["items"] for f in scatter})
                wrapinputs[CONTEXT] = "string"
                wrapbindings = {k: k for k in runins}
                for field in scatter:
                    wrapbindings[field] = {"source": field, "valueFrom": "$(self.value)"}
                indices = ",".join("inputs[" + json.dumps(f) + "].ordinal" for f in scatter)
                wrapbindings[CONTEXT] = {
                    "source": CONTEXT,
                    "valueFrom": "$(JSON.stringify(JSON.parse(self).concat([" + indices + "])))",
                }
                inner = {"run": run, "in": wrapbindings, "out": list(runouts)}
                if when:
                    inner["when"] = when
                wrapper = {
                    "class": "Workflow",
                    "inputs": wrapinputs,
                    "outputs": {
                        k: {
                            "type": ["null", v] if when and not isinstance(v, list) else v,
                            "outputSource": "invoke/" + k,
                        }
                        for k, v in runouts.items()
                    },
                    "requirements": {
                        "SubworkflowFeatureRequirement": {},
                        "StepInputExpressionRequirement": {},
                        "InlineJavascriptRequirement": {},
                    },
                    "steps": {"invoke": inner},
                }
                actual["run"] = emit(wrapper, [*path, name], "scatter")
                actual.pop("when", None)
                for field in scatter:
                    actual["in"][field] = helper + "/" + field
                actual["scatter"] = scatter
                actual["scatterMethod"] = step.get("scatterMethod", "dotproduct")
            output["steps"][name] = actual
        return emit(output, path), ins, {k: short_type(v) for k, v in outs.items()}

    entry, inputs, outputs = build(request["entrypoint"], [], [])
    return {
        "profile": PROFILE,
        "entrypoint": entry,
        "files": files,
        "invocations": invocations,
        "inputs": inputs,
        "outputs": outputs,
    }
