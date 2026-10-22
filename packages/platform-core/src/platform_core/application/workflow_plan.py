"""Pure, deterministic execution planning for the bounded P4 CWL profile."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any

from platform_core.application.control import digest
from platform_core.domain.control import require
from platform_core.domain.workflows import execution_id, invocation_id

UNKNOWN = object()


def fingerprint(value: Any) -> str:
    return "sha256:" + digest(value)


@dataclass(frozen=True)
class Binding:
    value: Any
    token: Any
    dependencies: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.value is not UNKNOWN


def known(value: Any) -> Binding:
    return Binding(value, value)


def unknown(token: Any, dependencies=()) -> Binding:
    return Binding(UNKNOWN, token, tuple(sorted(set(dependencies))))


class Planner:
    def __init__(self, source, compiled, inputs, action_id, maximum):
        self.source = source
        self.compiled = compiled
        self.documents = source["documents"]
        self.implementations = source["implementations"]
        self.root_execution_id = execution_id(action_id)
        self.maximum = maximum
        self.nodes = []
        self.deferred = []
        self._node_ids = set()
        self._template_ids = set()
        self.root_inputs = {name: known(value) for name, value in inputs.items()}

    @staticmethod
    def dependencies(bindings):
        return tuple(
            sorted(
                {dependency for binding in bindings.values() for dependency in binding.dependencies}
            )
        )

    @staticmethod
    def source_step(source):
        if not isinstance(source, str) or "/" not in source:
            return None
        return source.split("/", 1)[0]

    def resolve_source(self, source, scope, outputs):
        if source in scope:
            return scope[source]
        if source in outputs:
            return outputs[source]
        return unknown({"source": source})

    def resolve_binding(self, value, scope, outputs):
        if isinstance(value, str):
            return self.resolve_source(value, scope, outputs)
        if isinstance(value, dict):
            if "source" in value:
                sources = value["source"]
                if isinstance(sources, str):
                    return self.resolve_source(sources, scope, outputs)
                if isinstance(sources, list):
                    items = [self.resolve_source(item, scope, outputs) for item in sources]
                    dependencies = {
                        dependency for item in items for dependency in item.dependencies
                    }
                    if all(item.known for item in items):
                        return Binding(
                            [item.value for item in items],
                            {"sources": [item.token for item in items]},
                            tuple(sorted(dependencies)),
                        )
                    return unknown({"sources": [item.token for item in items]}, dependencies)
            if "default" in value:
                return known(value["default"])
        return unknown({"binding": value})

    def workflow_scope(self, document, supplied):
        result = {}
        for name, definition in document.get("inputs", {}).items():
            if name in supplied:
                result[name] = supplied[name]
            elif isinstance(definition, dict) and "default" in definition:
                result[name] = known(definition["default"])
            else:
                result[name] = unknown({"workflow_input": name})
        return result

    def step_order(self, document):
        steps = document["steps"]
        pending = list(steps)
        completed = set()
        result = []
        while pending:
            progressed = False
            for name in list(pending):
                sources = []
                for value in steps[name].get("in", {}).values():
                    source = value.get("source") if isinstance(value, dict) else value
                    sources.extend(source if isinstance(source, list) else [source])
                dependencies = {
                    step for source in sources if (step := self.source_step(source)) in steps
                }
                if dependencies <= completed:
                    result.append(name)
                    completed.add(name)
                    pending.remove(name)
                    progressed = True
            require(
                progressed,
                "INVALID_BINDING",
                "Workflow plan contains a cyclic or unresolved dependency",
                422,
            )
        return result

    def invocation(self, path):
        value = self.compiled["invocations"].get("/".join(path))
        require(value is not None, "INVALID_BINDING", "Workflow plan call path is unknown", 422)
        return value

    def add_leaf(self, reference, path, indices, bindings, condition):
        invocation = self.invocation(path)
        target = invocation["target"]
        require(
            self.implementations.get(reference) == target,
            "INVALID_BINDING",
            "Workflow implementation lock changed",
            422,
        )
        node_id = invocation_id(self.root_execution_id, path, indices)
        require(
            node_id not in self._node_ids,
            "INVALID_BINDING",
            "Workflow plan produced a duplicate node",
            422,
        )
        self._node_ids.add(node_id)
        record = {
            "node_id": node_id,
            "call_path": path,
            "scatter_indices": indices,
            "dependencies": list(self.dependencies(bindings)),
            "operator_id": target["operator_id"],
            "implementation_digest": fingerprint(target),
            "input_binding_digest": fingerprint(
                {name: binding.token for name, binding in sorted(bindings.items())}
            ),
            "condition": condition,
            "output_ports": sorted(invocation["outputs"]),
        }
        self.nodes.append(record)
        outputs = {}
        for name in invocation["outputs"]:
            if condition is False:
                outputs[name] = known(None)
            else:
                outputs[name] = unknown({"node_id": node_id, "output": name}, [node_id])
        return outputs

    def leaf_layout(self, reference, path, scatter_width=0):
        if reference in self.implementations:
            invocation = self.invocation(path)
            return [
                {
                    "call_path": path,
                    "scatter_width": scatter_width,
                    "target": invocation["target"],
                    "output_ports": sorted(invocation["outputs"]),
                }
            ]
        document = self.documents[reference]
        result = []
        for name, step in document["steps"].items():
            scatter = step.get("scatter", [])
            scatter = [scatter] if isinstance(scatter, str) else scatter
            result.extend(
                self.leaf_layout(
                    step["run"],
                    [*path, name],
                    scatter_width + len(scatter),
                )
            )
        return result

    def add_deferred(self, reference, path, index_depth, bindings):
        dependencies = list(self.dependencies(bindings))
        for leaf in self.leaf_layout(reference, path):
            target = leaf["target"]
            body = {
                "call_path": leaf["call_path"],
                "index_depth": index_depth + leaf["scatter_width"],
                "dependencies": dependencies,
                "operator_id": target["operator_id"],
                "implementation_digest": fingerprint(target),
                "input_binding_digest": fingerprint(
                    {name: binding.token for name, binding in sorted(bindings.items())}
                ),
                "condition": None,
                "output_ports": leaf["output_ports"],
            }
            template = {"template_id": "template_" + digest(body), **body}
            if template["template_id"] not in self._template_ids:
                self._template_ids.add(template["template_id"])
                self.deferred.append(template)

    @staticmethod
    def scatter_coordinates(method, fields, bindings):
        arrays = [bindings[field].value for field in fields]
        require(
            all(isinstance(value, list) for value in arrays),
            "INVALID_BINDING",
            "Static scatter input is not an array",
            422,
        )
        if method == "dotproduct":
            require(
                len({len(value) for value in arrays}) <= 1,
                "INVALID_BINDING",
                "Dotproduct scatter arrays have different lengths",
                422,
            )
            return [tuple([index] * len(fields)) for index in range(len(arrays[0]))]
        return list(itertools.product(*(range(len(value)) for value in arrays)))

    @staticmethod
    def combine_condition(parent, local):
        if parent is False or local is False:
            return False
        if parent is True and local is True:
            return True
        return None

    def invoke(self, reference, path, indices, bindings, condition):
        if reference in self.implementations:
            return self.add_leaf(reference, path, indices, bindings, condition)
        return self.walk(reference, path, indices, bindings, condition)

    def run_step(self, name, step, path, indices, scope, outputs, parent_condition):
        bindings = {
            key: self.resolve_binding(value, scope, outputs)
            for key, value in step.get("in", {}).items()
        }
        local_condition = True
        if step.get("when"):
            match = re.fullmatch(r"\$\(inputs\.([A-Za-z][A-Za-z0-9_]*)\)", step["when"])
            require(match is not None, "INVALID_BINDING", "Invalid planned condition", 422)
            value = bindings.get(match.group(1), unknown({"condition": match.group(1)}))
            local_condition = value.value if value.known and type(value.value) is bool else None
        condition = self.combine_condition(parent_condition, local_condition)
        scatter = step.get("scatter", [])
        scatter = [scatter] if isinstance(scatter, str) else scatter
        call_path = [*path, name]
        if not scatter:
            return self.invoke(step["run"], call_path, indices, bindings, condition)
        if not all(bindings[field].known for field in scatter):
            self.add_deferred(step["run"], call_path, len(indices) + len(scatter), bindings)
            return {
                name: unknown(
                    {"deferred_call": call_path, "output": name}, self.dependencies(bindings)
                )
                for name in step.get("out", [])
            }
        coordinates = self.scatter_coordinates(
            step.get("scatterMethod", "dotproduct"), scatter, bindings
        )
        collected = {name: [] for name in step.get("out", [])}
        for coordinate in coordinates:
            instance = dict(bindings)
            for field, ordinal in zip(scatter, coordinate, strict=True):
                source = bindings[field]
                instance[field] = Binding(
                    source.value[ordinal],
                    {"ordinal": ordinal, "value": source.value[ordinal]},
                    source.dependencies,
                )
            instance_condition = condition
            if step.get("when"):
                field = re.fullmatch(r"\$\(inputs\.([A-Za-z][A-Za-z0-9_]*)\)", step["when"]).group(
                    1
                )
                value = instance.get(field, unknown({"condition": field}))
                local_condition = value.value if value.known and type(value.value) is bool else None
                instance_condition = self.combine_condition(parent_condition, local_condition)
            values = self.invoke(
                step["run"], call_path, [*indices, *coordinate], instance, instance_condition
            )
            for output in collected:
                collected[output].append(values.get(output, unknown({"output": output})))
        result = {}
        for name, values in collected.items():
            dependencies = {dependency for value in values for dependency in value.dependencies}
            if values and all(value.known for value in values):
                result[name] = Binding(
                    [value.value for value in values],
                    [value.token for value in values],
                    tuple(sorted(dependencies)),
                )
            else:
                result[name] = unknown(
                    {"scatter": call_path, "values": [value.token for value in values]},
                    dependencies,
                )
        return result

    def walk(self, reference, path, indices, supplied, parent_condition=True):
        document = self.documents[reference]
        scope = self.workflow_scope(document, supplied)
        outputs = {}
        for name in self.step_order(document):
            values = self.run_step(
                name,
                document["steps"][name],
                path,
                indices,
                scope,
                outputs,
                parent_condition,
            )
            for output, value in values.items():
                outputs[f"{name}/{output}"] = value
        result = {}
        for name, definition in document.get("outputs", {}).items():
            source = definition.get("outputSource") if isinstance(definition, dict) else None
            result[name] = self.resolve_source(source, scope, outputs)
        return result

    def build(self):
        self.walk(self.source["entrypoint"], [], [], self.root_inputs)
        self.nodes.sort(key=lambda item: (item["call_path"], item["scatter_indices"]))
        self.deferred.sort(key=lambda item: item["template_id"])
        executable = sum(node["condition"] is not False for node in self.nodes)
        require(
            executable <= self.maximum and executable <= 1000,
            "INVALID_BINDING",
            "Workflow static expansion exceeds the declared node limit",
            422,
        )
        body = {
            "schema_version": 1,
            "package_digest": self.compiled["manifest_digest"],
            "input_digest": fingerprint(self.root_inputs_token()),
            "nodes": self.nodes,
            "deferred_templates": self.deferred,
        }
        return body | {"plan_digest": fingerprint(body)}

    def root_inputs_token(self):
        return {name: binding.token for name, binding in sorted(self.root_inputs.items())}


def build_execution_plan(
    source,
    compiled,
    inputs,
    *,
    action_id,
    max_node_executions,
):
    require(
        type(max_node_executions) is int and 1 <= max_node_executions <= 1000,
        "INVALID_BINDING",
        "Workflow node limit must be 1 to 1000",
        422,
    )
    require(
        isinstance(compiled.get("manifest_digest"), str)
        and compiled.get("profile") == "v7-cwl-p4-v1",
        "INVALID_BINDING",
        "Workflow package is not frozen",
        422,
    )
    return Planner(source, compiled, inputs, action_id, max_node_executions).build()


def plan_invocation(plan, node_id, call_path, scatter_indices):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        return None
    for node in plan.get("nodes", []):
        if (
            node.get("node_id") == node_id
            and node.get("call_path") == call_path
            and node.get("scatter_indices") == scatter_indices
        ):
            return node
    for template in plan.get("deferred_templates", []):
        if template.get("call_path") == call_path and template.get("index_depth") == len(
            scatter_indices
        ):
            return template
    return None
