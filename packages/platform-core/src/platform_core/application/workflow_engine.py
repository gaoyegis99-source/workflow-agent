"""Deterministic engine packages for a frozen bounded-CWL execution.

The public package compiler preserves the submitted workflow structure.  This
module consumes that package, the immutable execution plan, and the frozen root
inputs and emits the smaller graph that Toil actually runs.  User supplied code
is never copied into the generated expressions.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass
from typing import Any

from platform_core.application.workflow_plan import fingerprint
from platform_core.domain.control import require

CONTEXT = "v7_context"
CONDITION = "v7_run_condition"
MAX_ENGINE_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_ENGINE_FILES = 1005
COORDINATION_RESOURCES = {
    "coresMin": 0.2,
    "coresMax": 0.2,
    "ramMin": 128,
    "ramMax": 128,
}
MEMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True)
class Atom:
    source: str
    selectors: tuple[int | str, ...] = ()


@dataclass(frozen=True)
class Value:
    known: bool
    value: Any
    expression: Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def literal(value: Any) -> Value:
    return Value(True, value, value)


def runtime(expression: Any) -> Value:
    return Value(False, None, expression)


def selected(value: Value, selector: int | str) -> Value:
    known_value = value.value[selector] if value.known else None
    expression = value.expression
    if isinstance(expression, Atom):
        expression = Atom(expression.source, (*expression.selectors, selector))
    elif isinstance(expression, list) and isinstance(selector, int):
        expression = expression[selector]
    elif isinstance(expression, dict) and isinstance(selector, str):
        expression = expression[selector]
    else:
        expression = {"select": expression, "key": selector}
    return Value(value.known, known_value, expression)


def _atoms(expression: Any, result: list[str]) -> None:
    if isinstance(expression, Atom):
        if expression.source not in result:
            result.append(expression.source)
    elif isinstance(expression, list):
        for item in expression:
            _atoms(item, result)
    elif isinstance(expression, dict):
        if set(expression) == {"select", "key"}:
            _atoms(expression["select"], result)
        else:
            for item in expression.values():
                _atoms(item, result)


def _javascript(expression: Any, names: dict[str, str]) -> str:
    if isinstance(expression, Atom):
        value = "inputs[" + json.dumps(names[expression.source]) + "]"
        for selector in expression.selectors:
            value += "[" + json.dumps(selector) + "]"
        return value
    if isinstance(expression, list):
        return "[" + ",".join(_javascript(item, names) for item in expression) + "]"
    if isinstance(expression, dict):
        if set(expression) == {"select", "key"}:
            return (
                _javascript(expression["select"], names) + "[" + json.dumps(expression["key"]) + "]"
            )
        return (
            "{"
            + ",".join(
                json.dumps(key) + ":" + _javascript(item, names) for key, item in expression.items()
            )
            + "}"
        )
    return json.dumps(expression, ensure_ascii=False, separators=(",", ":"))


def step_binding(value: Value) -> dict | str:
    """Translate an immutable value expression to one CWL step input binding."""
    sources: list[str] = []
    _atoms(value.expression, sources)
    if not sources:
        return {"default": value.expression}
    if isinstance(value.expression, Atom) and not value.expression.selectors:
        return value.expression.source
    names = {source: f"v{index}" for index, source in enumerate(sources)}
    # A source list would expose only `self`, losing the stable names needed for
    # an arbitrary nested expression.  The caller expands this marker into
    # dedicated hidden step inputs and a final valueFrom input.
    return {
        "v7_sources": sources,
        "v7_expression": _javascript(value.expression, names),
        "v7_names": names,
    }


def normalize_port(value: Any) -> Any:
    if (
        isinstance(value, dict)
        and isinstance(value.get("type"), str)
        and value["type"] in {"array", "record"}
    ):
        return {"type": value}
    return value


class FlatCompiler:
    def __init__(self, source, compiled, plan, inputs, bridge_command):
        self.source = source
        self.compiled = compiled
        self.plan = plan
        self.documents = source["documents"]
        self.implementations = source["implementations"]
        self.bridge_command = bridge_command or ["python", "/opt/v7-cwl/bridge.py"]
        self.files: dict[str, dict] = {}
        self.steps: dict[str, dict] = {}
        self.node_map: dict[str, dict] = {}
        self.tools: dict[tuple[str, ...], str] = {}
        self.plan_nodes = {
            (tuple(item["call_path"]), tuple(item["scatter_indices"])): item
            for item in plan["nodes"]
        }
        self.root = {name: Value(True, value, Atom(name)) for name, value in sorted(inputs.items())}

    @staticmethod
    def step_order(document):
        steps = document["steps"]
        pending, completed, ordered = list(steps), set(), []
        while pending:
            progressed = False
            for name in list(pending):
                dependencies = set()
                for binding in steps[name].get("in", {}).values():
                    source = binding.get("source") if isinstance(binding, dict) else binding
                    for item in source if isinstance(source, list) else [source]:
                        if isinstance(item, str) and "/" in item and item.split("/", 1)[0] in steps:
                            dependencies.add(item.split("/", 1)[0])
                if dependencies <= completed:
                    ordered.append(name)
                    completed.add(name)
                    pending.remove(name)
                    progressed = True
            require(progressed, "INVALID_BINDING", "Flat workflow contains a dependency cycle", 422)
        return ordered

    @staticmethod
    def resolve_source(source, scope, outputs):
        if source in scope:
            return scope[source]
        if source in outputs:
            return outputs[source]
        require(False, "INVALID_BINDING", "Flat workflow source is unresolved", 422)

    def resolve(self, binding, scope, outputs):
        if isinstance(binding, str):
            return self.resolve_source(binding, scope, outputs)
        if isinstance(binding, dict):
            if "source" in binding:
                sources = binding["source"]
                sources = sources if isinstance(sources, list) else [sources]
                values = [self.resolve_source(item, scope, outputs) for item in sources]
                merge = binding.get("linkMerge", "merge_nested")
                if merge == "merge_flattened":
                    expression, known_value = [], []
                    known = all(value.known for value in values)
                    for value in values:
                        if isinstance(value.expression, list):
                            expression.extend(value.expression)
                        else:
                            expression.append(value.expression)
                        if known:
                            if isinstance(value.value, list):
                                known_value.extend(value.value)
                            else:
                                known_value.append(value.value)
                    value = Value(known, known_value if known else None, expression)
                else:
                    value = Value(
                        all(item.known for item in values),
                        [item.value for item in values]
                        if all(item.known for item in values)
                        else None,
                        [item.expression for item in values],
                    )
                if not isinstance(binding["source"], list):
                    value = values[0]
                pick = binding.get("pickValue")
                if pick:
                    require(
                        value.known,
                        "INVALID_BINDING",
                        "Dynamic pickValue requires legacy graph",
                        422,
                    )
                    available = [item for item in value.value if item is not None]
                    if pick == "first_non_null":
                        require(bool(available), "INVALID_BINDING", "pickValue has no value", 422)
                        return literal(available[0])
                    if pick == "the_only_non_null":
                        require(
                            len(available) == 1, "INVALID_BINDING", "pickValue is ambiguous", 422
                        )
                        return literal(available[0])
                    if pick == "all_non_null":
                        return literal(available)
                    require(False, "INVALID_BINDING", "Unsupported pickValue", 422)
                return value
            if "default" in binding:
                return literal(binding["default"])
        require(False, "INVALID_BINDING", "Flat workflow binding is invalid", 422)

    def scope(self, document, supplied, *, allow_unbound=False):
        result = {}
        for name, definition in document.get("inputs", {}).items():
            if name in supplied:
                result[name] = supplied[name]
            elif isinstance(definition, dict) and "default" in definition:
                result[name] = literal(definition["default"])
            elif allow_unbound:
                # Direct application-level admission is retained for recovery
                # tests created before public input resolution was introduced.
                # The staged CWL input remains required at actual execution.
                result[name] = runtime(Atom(name))
            else:
                require(False, "INVALID_BINDING", "Flat workflow input is missing", 422)
        return result

    def tool(self, path, invocation):
        key = tuple(path)
        if key in self.tools:
            return self.tools[key]
        token = hashlib.sha256(canonical([path, "flat-tool"])).hexdigest()[:24]
        filename = f"v7_flat_{token}.cwl"
        # The root request was already validated against the frozen operator
        # signature.  A flattened selector can narrow an array-valued root
        # source to a scalar, which CWL's static checker cannot infer through
        # valueFrom.  The trusted bridge tool therefore receives `Any`; the
        # platform still validates the exact planned operator and binding.
        inputs = {
            name: (
                {"type": "Any", "default": definition["default"]}
                if isinstance(definition, dict) and "default" in definition
                else "Any"
            )
            for name, definition in invocation["inputs"].items()
        }
        inputs |= {CONTEXT: "string", CONDITION: {"type": "boolean", "default": True}}
        self.files[filename] = {
            "cwlVersion": "v1.2",
            "class": "CommandLineTool",
            "inputs": inputs,
            "outputs": {name: {"type": value} for name, value in invocation["outputs"].items()},
            "baseCommand": self.bridge_command,
            "arguments": ["--path", json.dumps(path), "--request", "request.json"],
            "requirements": {
                "InlineJavascriptRequirement": {},
                "ResourceRequirement": COORDINATION_RESOURCES,
                "InitialWorkDirRequirement": {
                    "listing": [{"entryname": "request.json", "entry": "$(JSON.stringify(inputs))"}]
                },
            },
        }
        self.tools[key] = filename
        return filename

    @staticmethod
    def condition(parent, local):
        if parent is not None and parent.known and parent.value is False:
            return literal(False)
        if local is None:
            return parent or literal(True)
        if parent is None or parent.known and parent.value is True:
            return local
        if local.known and local.value is True:
            return parent
        if local.known and local.value is False:
            return literal(False)
        require(False, "INVALID_BINDING", "Multiple dynamic conditions require legacy graph", 422)

    def add_binding(self, inputs, name, value):
        binding = step_binding(value)
        if not isinstance(binding, dict) or "v7_sources" not in binding:
            inputs[name] = binding
            return
        sources = binding["v7_sources"]
        expression = binding["v7_expression"]
        for source, alias in binding["v7_names"].items():
            position = sources.index(source)
            replacement = "self" if len(sources) == 1 else f"self[{position}]"
            expression = expression.replace(f'inputs["{alias}"]', replacement)
        inputs[name] = {"source": sources[0] if len(sources) == 1 else sources}
        if len(sources) > 1:
            inputs[name]["linkMerge"] = "merge_nested"
        inputs[name]["valueFrom"] = "$(" + expression + ")"

    def leaf(self, reference, path, indices, bindings, condition):
        node = self.plan_nodes.get((tuple(path), tuple(indices)))
        require(node is not None, "INVALID_BINDING", "Flat node is absent from execution plan", 422)
        invocation = self.compiled["invocations"].get("/".join(path))
        require(
            invocation is not None and invocation["target"] == self.implementations[reference],
            "INVALID_BINDING",
            "Flat node implementation changed",
            422,
        )
        if node["condition"] is False:
            return {name: literal(None) for name in invocation["outputs"]}
        step_id = node["node_id"]
        step_inputs = {}
        for name, value in bindings.items():
            self.add_binding(step_inputs, name, value)
        step_inputs[CONTEXT] = {"default": json.dumps(indices, separators=(",", ":"))}
        self.add_binding(step_inputs, CONDITION, condition)
        step = {
            "run": self.tool(path, invocation),
            "in": step_inputs,
            "out": list(invocation["outputs"]),
        }
        if not condition.known:
            step["when"] = f"$(inputs.{CONDITION})"
        self.steps[step_id] = step
        self.node_map[node["node_id"]] = {
            "node_id": node["node_id"],
            "call_path": node["call_path"],
            "scatter_indices": node["scatter_indices"],
            "operator_id": node["operator_id"],
            "implementation_digest": node["implementation_digest"],
            "output_ports": node["output_ports"],
            "step_id": step_id,
        }
        return {name: runtime(Atom(f"{step_id}/{name}")) for name in invocation["outputs"]}

    @staticmethod
    def coordinates(method, fields, bindings):
        arrays = [bindings[field].value for field in fields]
        require(
            all(
                bindings[field].known and isinstance(value, list)
                for field, value in zip(fields, arrays, strict=True)
            ),
            "INVALID_BINDING",
            "Dynamic scatter requires legacy graph",
            422,
        )
        if method == "dotproduct":
            require(
                len({len(value) for value in arrays}) <= 1,
                "INVALID_BINDING",
                "Dotproduct lengths differ",
                422,
            )
            return [tuple([index] * len(fields)) for index in range(len(arrays[0]))], [
                len(arrays[0])
            ]
        return list(itertools.product(*(range(len(value)) for value in arrays))), [
            len(value) for value in arrays
        ]

    @staticmethod
    def reshape(values, dimensions):
        if len(dimensions) <= 1:
            return values
        width = 1
        for value in dimensions[1:]:
            width *= value
        return [
            FlatCompiler.reshape(values[index : index + width], dimensions[1:])
            for index in range(0, len(values), width)
        ]

    def invoke(self, reference, path, indices, bindings, condition):
        if reference in self.implementations:
            return self.leaf(reference, path, indices, bindings, condition)
        return self.walk(reference, path, indices, bindings, condition)

    def run_step(self, name, step, path, indices, scope, outputs, parent_condition):
        bindings = {
            key: self.resolve(value, scope, outputs) for key, value in step.get("in", {}).items()
        }
        local = None
        if step.get("when"):
            match = re.fullmatch(r"\$\(inputs\.([A-Za-z][A-Za-z0-9_]*)\)", step["when"])
            require(match is not None, "INVALID_BINDING", "Invalid flat condition", 422)
            local = bindings[match.group(1)]
        condition = self.condition(parent_condition, local)
        scatter = step.get("scatter", [])
        scatter = [scatter] if isinstance(scatter, str) else scatter
        call_path = [*path, name]
        if not scatter:
            return self.invoke(step["run"], call_path, indices, bindings, condition)
        coordinates, dimensions = self.coordinates(
            step.get("scatterMethod", "dotproduct"), scatter, bindings
        )
        collected = {name: [] for name in step.get("out", [])}
        for coordinate in coordinates:
            instance = dict(bindings)
            for field, ordinal in zip(scatter, coordinate, strict=True):
                instance[field] = selected(bindings[field], ordinal)
            local_instance = None
            if step.get("when"):
                field = re.fullmatch(r"\$\(inputs\.([A-Za-z][A-Za-z0-9_]*)\)", step["when"]).group(
                    1
                )
                local_instance = instance[field]
            instance_condition = self.condition(parent_condition, local_instance)
            values = self.invoke(
                step["run"], call_path, [*indices, *coordinate], instance, instance_condition
            )
            for output in collected:
                collected[output].append(values[output])
        nested = step.get("scatterMethod", "dotproduct") == "nested_crossproduct"
        return {
            name: runtime(
                self.reshape([item.expression for item in values], dimensions)
                if nested
                else [item.expression for item in values]
            )
            for name, values in collected.items()
        }

    def walk(self, reference, path, indices, supplied, parent_condition=None):
        document = self.documents[reference]
        scope, outputs = self.scope(document, supplied, allow_unbound=not path), {}
        for name in self.step_order(document):
            values = self.run_step(
                name, document["steps"][name], path, indices, scope, outputs, parent_condition
            )
            for output, value in values.items():
                outputs[f"{name}/{output}"] = value
        result = {}
        for name, definition in document.get("outputs", {}).items():
            source = definition.get("outputSource") if isinstance(definition, dict) else None
            binding = {"source": source}
            if isinstance(definition, dict):
                binding |= {
                    key: definition[key] for key in ("linkMerge", "pickValue") if key in definition
                }
            result[name] = self.resolve(binding, scope, outputs)
        return result

    def gather(self, outputs):
        atoms: list[str] = []
        for value in outputs.values():
            _atoms(value.expression, atoms)
        names = {source: f"i{index}" for index, source in enumerate(atoms)}
        gather_inputs = {name: "Any" for name in names.values()}
        gather_outputs = {name: {"type": value} for name, value in self.compiled["outputs"].items()}
        expression = (
            "${return {"
            + ",".join(
                json.dumps(name) + ":" + _javascript(value.expression, names)
                for name, value in outputs.items()
            )
            + "};}"
        )
        filename = "v7_gather.cwl"
        self.files[filename] = {
            "cwlVersion": "v1.2",
            "class": "ExpressionTool",
            "inputs": gather_inputs,
            "outputs": gather_outputs,
            "requirements": {
                "InlineJavascriptRequirement": {},
                "ResourceRequirement": COORDINATION_RESOURCES,
            },
            "expression": expression,
        }
        self.steps["v7_gather"] = {
            "run": filename,
            "in": {name: source for source, name in names.items()},
            "out": list(outputs),
        }

    def build(self):
        outputs = self.walk(self.source["entrypoint"], [], [], self.root)
        self.gather(outputs)
        expected = {
            node["node_id"] for node in self.plan["nodes"] if node["condition"] is not False
        }
        require(
            set(self.node_map) == expected, "INVALID_BINDING", "Flat node map is incomplete", 422
        )
        workflow = {
            "cwlVersion": "v1.2",
            "class": "Workflow",
            "inputs": {
                name: normalize_port(value) for name, value in self.compiled["inputs"].items()
            },
            "outputs": {
                name: {"type": value, "outputSource": f"v7_gather/{name}"}
                for name, value in self.compiled["outputs"].items()
            },
            "requirements": {
                "StepInputExpressionRequirement": {},
                "InlineJavascriptRequirement": {},
                "MultipleInputFeatureRequirement": {},
            },
            "steps": self.steps,
        }
        self.files["workflow.cwl"] = workflow
        return self.files, self.node_map


def validate_engine_package(package, compiled, plan):
    require(
        isinstance(package, dict)
        and set(package)
        == {
            "schema_version",
            "strategy",
            "source_package_digest",
            "plan_digest",
            "entrypoint",
            "files",
            "node_map",
            "package_digest",
        }
        and package.get("schema_version") == 1
        and package.get("strategy") in {"flat_v1", "legacy_v1"}
        and package.get("source_package_digest") == compiled.get("manifest_digest")
        and package.get("plan_digest") == plan.get("plan_digest")
        and isinstance(package.get("files"), dict)
        and 0 < len(package["files"]) <= MAX_ENGINE_FILES
        and package.get("entrypoint") in package["files"]
        and all(
            MEMBER.fullmatch(name) and isinstance(value, dict)
            for name, value in package["files"].items()
        )
        and isinstance(package.get("node_map"), dict),
        "INVALID_BINDING",
        "Invalid immutable workflow engine package",
        422,
    )
    body = {key: value for key, value in package.items() if key != "package_digest"}
    require(
        package["package_digest"] == fingerprint(body)
        and len(canonical(package)) <= MAX_ENGINE_PACKAGE_BYTES,
        "INVALID_BINDING",
        "Workflow engine package digest or size is invalid",
        422,
    )
    expected = {node["node_id"]: node for node in plan["nodes"] if node["condition"] is not False}
    require(
        set(package["node_map"]) == set(expected),
        "INVALID_BINDING",
        "Engine node map differs from plan",
        422,
    )
    for node_id, item in package["node_map"].items():
        node = expected[node_id]
        require(
            isinstance(item, dict)
            and set(item)
            == {
                "node_id",
                "call_path",
                "scatter_indices",
                "operator_id",
                "implementation_digest",
                "output_ports",
                "step_id",
            }
            and item["node_id"] == node_id
            and item["call_path"] == node["call_path"]
            and item["scatter_indices"] == node["scatter_indices"]
            and item["operator_id"] == node["operator_id"]
            and item["implementation_digest"] == node["implementation_digest"]
            and item["output_ports"] == node["output_ports"]
            and (item["step_id"] is None or MEMBER.fullmatch(item["step_id"])),
            "INVALID_BINDING",
            "Engine node map entry differs from plan",
            422,
        )
    deferred = bool(plan["deferred_templates"])
    require(
        package["strategy"] == ("legacy_v1" if deferred else "flat_v1"),
        "INVALID_BINDING",
        "Workflow engine strategy does not match the plan",
        422,
    )
    if package["strategy"] == "flat_v1":
        root = package["files"][package["entrypoint"]]
        require(
            root.get("class") == "Workflow"
            and "v7_gather" in root.get("steps", {})
            and all(item["step_id"] in root["steps"] for item in package["node_map"].values())
            and not any("scatter" in step for step in root["steps"].values()),
            "INVALID_BINDING",
            "Flat workflow graph is inconsistent",
            422,
        )
    return package


def build_engine_package(source, compiled, plan, inputs, *, bridge_command=None):
    require(plan.get("schema_version") == 1, "INVALID_BINDING", "Execution plan is required", 422)
    if plan["deferred_templates"]:
        files = compiled["files"]
        entrypoint = compiled["entrypoint"]
        node_map = {
            node["node_id"]: {
                "node_id": node["node_id"],
                "call_path": node["call_path"],
                "scatter_indices": node["scatter_indices"],
                "operator_id": node["operator_id"],
                "implementation_digest": node["implementation_digest"],
                "output_ports": node["output_ports"],
                "step_id": None,
            }
            for node in plan["nodes"]
            if node["condition"] is not False
        }
        strategy = "legacy_v1"
    else:
        files, node_map = FlatCompiler(source, compiled, plan, inputs, bridge_command).build()
        entrypoint, strategy = "workflow.cwl", "flat_v1"
    body = {
        "schema_version": 1,
        "strategy": strategy,
        "source_package_digest": compiled["manifest_digest"],
        "plan_digest": plan["plan_digest"],
        "entrypoint": entrypoint,
        "files": files,
        "node_map": node_map,
    }
    package = body | {"package_digest": fingerprint(body)}
    return validate_engine_package(package, compiled, plan)
