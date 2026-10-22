import type { Edge, Node, XYPosition } from "@xyflow/react";

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type OperatorTarget = {
  kind: "OPERATOR";
  operator_id: string;
  version_id: string;
  execution_binding: { kind: "LOCAL"; implementation_id: string };
};

export type WorkflowDocument = {
  cwlVersion?: "v1.2";
  class: "Workflow";
  inputs: Record<string, JsonValue>;
  outputs: Record<string, JsonValue>;
  steps: Record<string, JsonValue>;
  requirements?: Record<string, JsonValue>;
  label?: string;
  doc?: string;
};

export type PackageSource = {
  profile: "v7-cwl-p4-v1";
  name: string;
  entrypoint: string;
  documents: Record<string, WorkflowDocument>;
  implementations: Record<string, OperatorTarget>;
};

export type CanvasNodeData = {
  nodeKind: "INPUT" | "OPERATOR" | "SUBWORKFLOW" | "OUTPUT";
  label: string;
  documentId: string;
  callPath?: string[];
  portNames: string[];
  inputPorts?: string[];
  outputPorts?: string[];
  declaredOutputs?: string[];
  operatorId?: string;
  versionId?: string;
  implementationRef?: string;
  parameters?: JsonValue;
  sourceType?: JsonValue;
  outputType?: JsonValue;
  rawBindings?: Record<string, JsonValue>;
  scatter?: string[];
  scatterMethod?: "dotproduct" | "flat_crossproduct" | "nested_crossproduct";
  when?: string;
  cwlLabel?: string;
  cwlDoc?: string;
};

export type CanvasProjection = {
  editable: boolean;
  reason?: string;
  name: string;
  nodes: Node<CanvasNodeData>[];
  edges: Edge[];
  source?: PackageSource;
};

export type OperatorVersionChange = {
  from: string;
  to: string;
  addedInputs: string[];
  removedInputs: string[];
  addedOutputs: string[];
  removedOutputs: string[];
  parameterSchemaChanged: boolean;
  safeToApplyWithoutDisconnecting: boolean;
};

type Layout = Record<string, unknown>;
type DescriptorProjection = Record<string, {
  input_ports: Record<string, unknown>;
  output_ports: Record<string, unknown>;
  target: OperatorTarget;
  parameter_schema?: Record<string, unknown>;
}>;

const documentFields = new Set(["cwlVersion", "class", "inputs", "outputs", "steps", "requirements", "label", "doc"]);
const stepFields = new Set(["run", "in", "out", "scatter", "scatterMethod", "when", "label", "doc"]);
const requirementNames = new Set(["SubworkflowFeatureRequirement", "ScatterFeatureRequirement", "MultipleInputFeatureRequirement"]);
const scatterMethods = new Set(["dotproduct", "flat_crossproduct", "nested_crossproduct"]);
const conditionPattern = /^\$\(inputs\.[A-Za-z][A-Za-z0-9_]*\)$/;

export function upgradeBlankSource(source: unknown, name: string): PackageSource | null {
  if (!isRecord(source)) return null;
  if (source.profile === "v7-cwl-p4-v1") return source as PackageSource;
  if (
    source.cwlVersion === "v1.2" && source.class === "Workflow" &&
    isRecord(source.inputs) && isRecord(source.outputs) && isRecord(source.steps) &&
    Object.keys(source.inputs).length === 0 && Object.keys(source.outputs).length === 0 &&
    Object.keys(source.steps).length === 0
  ) return emptyPackage(name);
  return null;
}

export function emptyPackage(name: string): PackageSource {
  return {
    profile: "v7-cwl-p4-v1", name, entrypoint: "main",
    documents: { main: { cwlVersion: "v1.2", class: "Workflow", inputs: {}, outputs: {}, steps: {} } },
    implementations: {},
  };
}

function isRecord(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function clone<T>(value: T): T { return structuredClone(value); }

function unsafeNumber(value: unknown): boolean {
  if (typeof value === "number") return !Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value));
  if (Array.isArray(value)) return value.some(unsafeNumber);
  if (isRecord(value)) return Object.values(value).some(unsafeNumber);
  return false;
}

function prefix(documentId: string, entrypoint: string): string {
  return documentId === entrypoint ? "" : `doc:${documentId}:`;
}

function nodeId(documentId: string, entrypoint: string, kind: string, name: string): string {
  return `${prefix(documentId, entrypoint)}${kind}:${name}`;
}

function position(layout: Layout, id: string, fallback: XYPosition): XYPosition {
  const positions = isRecord(layout.positions) ? layout.positions : {};
  const value = isRecord(positions[id]) ? positions[id] : {};
  return {
    x: typeof value.x === "number" && Number.isFinite(value.x) ? value.x : fallback.x,
    y: typeof value.y === "number" && Number.isFinite(value.y) ? value.y : fallback.y,
  };
}

function bindingSources(value: unknown): string[] | null {
  if (typeof value === "string") return [value];
  if (!isRecord(value)) return null;
  if (!("source" in value)) return [];
  if (typeof value.source === "string") return [value.source];
  if (Array.isArray(value.source) && value.source.every((item) => typeof item === "string")) return value.source;
  return null;
}

function supportedSource(source: PackageSource): string | null {
  if (source.profile !== "v7-cwl-p4-v1" || !isRecord(source.documents) || !isRecord(source.implementations)) return "源码不是受支持的自包含工作流包。";
  if (!(source.entrypoint in source.documents)) return "入口 Workflow 不存在。";
  if (unsafeNumber(source)) return "源码含超出 JavaScript 安全范围的数值，只能只读。";
  for (const [documentId, document] of Object.entries(source.documents)) {
    if (
      !isRecord(document) || document.class !== "Workflow" ||
      (document.cwlVersion !== undefined && document.cwlVersion !== "v1.2")
    ) return `${documentId} 不是 CWL 1.2 Workflow。`;
    if (Object.keys(document).some((field) => !documentFields.has(field))) return `${documentId} 含无法无损投影的字段。`;
    if (!isRecord(document.inputs) || !isRecord(document.outputs) || !isRecord(document.steps)) return `${documentId} 的端口或步骤结构无效。`;
    if (document.requirements && (!isRecord(document.requirements) || Object.entries(document.requirements).some(([name, value]) => !requirementNames.has(name) || !isRecord(value) || Object.keys(value).length !== 0))) return `${documentId} 含不受支持的 requirement。`;
    for (const [stepKey, raw] of Object.entries(document.steps)) {
      if (!isRecord(raw) || Object.keys(raw).some((field) => !stepFields.has(field))) return `${documentId}/${stepKey} 含无法无损投影的字段。`;
      const step = raw as Record<string, any>;
      if (typeof step.run !== "string" || (!(step.run in source.documents) && !(step.run in source.implementations))) return `${documentId}/${stepKey} 的 run 不是本地受信引用。`;
      if (!isRecord(step.in) || !Array.isArray(step.out) || !step.out.every((item: unknown) => typeof item === "string")) return `${documentId}/${stepKey} 的端口结构无效。`;
      if (Object.values(step.in).some((value) => bindingSources(value) === null)) return `${documentId}/${stepKey} 含无法投影的输入绑定。`;
      const scatter = typeof step.scatter === "string" ? [step.scatter] : step.scatter ?? [];
      if (!Array.isArray(scatter) || !scatter.every((item: unknown) => typeof item === "string")) return `${documentId}/${stepKey} 的 scatter 无法投影。`;
      if (step.scatterMethod !== undefined && (!scatter.length || !scatterMethods.has(step.scatterMethod))) return `${documentId}/${stepKey} 的 scatterMethod 不受支持。`;
      if (step.when !== undefined && (typeof step.when !== "string" || !conditionPattern.test(step.when))) return `${documentId}/${stepKey} 的条件不受支持。`;
    }
  }
  return null;
}

export function sourceToCanvas(source: unknown, layout: Layout = {}, descriptors: DescriptorProjection = {}): CanvasProjection {
  const packageSource = upgradeBlankSource(source, "workflow");
  if (!packageSource) return { editable: false, reason: "源码超出受限工作流包范围。", name: "", nodes: [], edges: [] };
  const reason = supportedSource(packageSource);
  if (reason) return { editable: false, reason, name: packageSource.name, nodes: [], edges: [], source: clone(packageSource) };
  const nodes: Node<CanvasNodeData>[] = [];
  const edges: Edge[] = [];
  const documents = Object.entries(packageSource.documents);
  for (const [documentIndex, [documentId, document]] of documents.entries()) {
    const offset = documentIndex * 980;
    Object.entries(document.inputs).forEach(([name, type], index) => {
      const id = nodeId(documentId, packageSource.entrypoint, "input", name);
      nodes.push({ id, type: "capability", position: position(layout, id, { x: offset + 40, y: 80 + index * 130 }), data: { nodeKind: "INPUT", documentId, label: name, portNames: [name], sourceType: type } });
    });
    Object.entries(document.steps).forEach(([stepKey, raw], index) => {
      const step = raw as Record<string, any>;
      const target = packageSource.implementations[step.run];
      const sub = packageSource.documents[step.run];
      const descriptor = target ? descriptors[`${target.operator_id}@${target.version_id}`] : undefined;
      const inputPorts = descriptor ? Object.keys(descriptor.input_ports) : sub ? Object.keys(sub.inputs) : Object.keys(step.in).filter((name) => name !== "parameters");
      const declaredOutputs = step.out as string[];
      const outputPorts = descriptor ? Object.keys(descriptor.output_ports) : sub ? Object.keys(sub.outputs) : declaredOutputs;
      const parameterBinding = isRecord(step.in.parameters) ? step.in.parameters : {};
      const id = nodeId(documentId, packageSource.entrypoint, "step", stepKey);
      const scatter = typeof step.scatter === "string" ? [step.scatter] : step.scatter ?? [];
      nodes.push({ id, type: "capability", position: position(layout, id, { x: offset + 380, y: 80 + index * 160 }), data: {
        nodeKind: sub ? "SUBWORKFLOW" : "OPERATOR", documentId, callPath: [documentId, stepKey], label: stepKey,
        portNames: [...inputPorts, ...outputPorts], operatorId: target?.operator_id, versionId: target?.version_id,
        implementationRef: step.run, parameters: "default" in parameterBinding ? parameterBinding.default : {},
        inputPorts, outputPorts, declaredOutputs, rawBindings: clone(step.in), scatter,
        scatterMethod: step.scatterMethod, when: step.when, cwlLabel: step.label, cwlDoc: step.doc,
      } });
      for (const [port, binding] of Object.entries(step.in)) {
        if (port === "parameters") continue;
        const sources = bindingSources(binding) ?? [];
        sources.forEach((sourceRef, sourceIndex) => {
          const [parent, output = parent] = sourceRef.split("/", 2);
          const sourceId = sourceRef.includes("/") ? nodeId(documentId, packageSource.entrypoint, "step", parent) : nodeId(documentId, packageSource.entrypoint, "input", parent);
          edges.push({ id: `edge:${documentId}:${sourceId}:${output}:${id}:${port}:${sourceIndex}`, source: sourceId, sourceHandle: `out:${output}`, target: id, targetHandle: `in:${port}` });
        });
      }
    });
    Object.entries(document.outputs).forEach(([name, raw], index) => {
      const output = (isRecord(raw) ? raw : { type: raw }) as Record<string, any>;
      const id = nodeId(documentId, packageSource.entrypoint, "output", name);
      nodes.push({ id, type: "capability", position: position(layout, id, { x: offset + 760, y: 80 + index * 130 }), data: { nodeKind: "OUTPUT", documentId, label: name, portNames: [name], outputType: (output.type ?? "File") as JsonValue } });
      if (typeof output.outputSource === "string") {
        const [parent, port] = output.outputSource.split("/", 2);
        if (parent && port) edges.push({ id: `edge:${documentId}:step:${parent}:${port}:output:${name}`, source: nodeId(documentId, packageSource.entrypoint, "step", parent), sourceHandle: `out:${port}`, target: id, targetHandle: "in:value" });
      }
    });
  }
  return { editable: true, name: packageSource.name, nodes, edges, source: clone(packageSource) };
}

function sourceReference(nodes: Node<CanvasNodeData>[], edge: Edge): string | null {
  const source = nodes.find((item) => item.id === edge.source);
  if (!source) return null;
  const port = edge.sourceHandle?.replace(/^out:/, "") ?? source.data.label;
  return source.data.nodeKind === "INPUT" ? source.data.label : `${source.data.label}/${port}`;
}

function rebuiltBinding(raw: JsonValue | undefined, sources: string[]): JsonValue | undefined {
  const original: Record<string, JsonValue> = isRecord(raw) ? clone(raw) as Record<string, JsonValue> : {};
  if (!sources.length) {
    if (!isRecord(raw)) return undefined;
    delete original.source;
    return Object.keys(original).length ? original : undefined;
  }
  if (sources.length === 1 && typeof raw === "string") return sources[0];
  original.source = sources.length === 1 ? sources[0] : sources;
  return original;
}

export function canvasToSource(projection: CanvasProjection, descriptors: DescriptorProjection): PackageSource {
  if (!projection.editable) throw new Error("只读源码不能保存为受限图。");
  const source = clone(projection.source ?? emptyPackage(projection.name));
  source.name = projection.name;
  for (const [documentId, original] of Object.entries(source.documents)) {
    const inputs: Record<string, JsonValue> = {};
    const outputs: Record<string, JsonValue> = {};
    const steps: Record<string, JsonValue> = {};
    const members = projection.nodes.filter((item) => item.data.documentId === documentId);
    for (const node of members) {
      if (node.data.nodeKind === "INPUT") inputs[node.data.label] = node.data.sourceType ?? "File";
      if (node.data.nodeKind === "OPERATOR" || node.data.nodeKind === "SUBWORKFLOW") {
        const run = node.data.implementationRef;
        if (!run) throw new Error(`${documentId}/${node.data.label} 缺少本地 run。`);
        if (node.data.nodeKind === "OPERATOR") {
          const descriptor = descriptors[`${node.data.operatorId}@${node.data.versionId}`];
          if (!descriptor || stableStringify(descriptor.target) !== stableStringify(source.implementations[run])) throw new Error(`算子 ${node.data.operatorId}@${node.data.versionId} 缺少受信实现。`);
        }
        const raw = clone(node.data.rawBindings ?? {});
        const bindings: Record<string, JsonValue> = {};
        const ports = new Set([...(node.data.inputPorts ?? []), ...Object.keys(raw)]);
        for (const port of ports) {
          if (port === "parameters") { bindings.parameters = { default: node.data.parameters ?? {} }; continue; }
          const sources = projection.edges.filter((edge) => edge.target === node.id && edge.targetHandle === `in:${port}`).map((edge) => sourceReference(projection.nodes, edge)).filter((value): value is string => value !== null);
          const binding = rebuiltBinding(raw[port], sources);
          if (binding !== undefined) bindings[port] = binding;
        }
        const step: Record<string, JsonValue> = { run, in: bindings, out: node.data.declaredOutputs ?? [] };
        if (node.data.scatter?.length) {
          step.scatter = node.data.scatter.length === 1 ? node.data.scatter[0] : node.data.scatter;
          step.scatterMethod = node.data.scatterMethod ?? "dotproduct";
        }
        if (node.data.when) step.when = node.data.when;
        if (node.data.cwlLabel !== undefined) step.label = node.data.cwlLabel;
        if (node.data.cwlDoc !== undefined) step.doc = node.data.cwlDoc;
        steps[node.data.label] = step;
      }
      if (node.data.nodeKind === "OUTPUT") {
        const previous = original.outputs[node.data.label];
        const value: Record<string, JsonValue> = isRecord(previous) ? clone(previous) as Record<string, JsonValue> : { type: node.data.outputType ?? "File" };
        value.type = node.data.outputType ?? value.type ?? "File";
        const edge = projection.edges.find((item) => item.target === node.id && item.targetHandle === "in:value");
        const ref = edge ? sourceReference(projection.nodes, edge) : null;
        if (ref) value.outputSource = ref; else delete value.outputSource;
        outputs[node.data.label] = value;
      }
    }
    source.documents[documentId] = { ...clone(original), inputs, outputs, steps };
  }
  return source;
}

export function compareOperatorVersions(current: { version_id: string; input_ports: Record<string, unknown>; output_ports: Record<string, unknown>; parameter_schema: Record<string, unknown> }, next: { version_id: string; input_ports: Record<string, unknown>; output_ports: Record<string, unknown>; parameter_schema: Record<string, unknown> }): OperatorVersionChange {
  const difference = (left: Record<string, unknown>, right: Record<string, unknown>) => Object.keys(right).filter((key) => !(key in left)).sort();
  const removedInputs = difference(next.input_ports, current.input_ports);
  const removedOutputs = difference(next.output_ports, current.output_ports);
  return {
    from: current.version_id, to: next.version_id,
    addedInputs: difference(current.input_ports, next.input_ports), removedInputs,
    addedOutputs: difference(current.output_ports, next.output_ports), removedOutputs,
    parameterSchemaChanged: stableStringify(current.parameter_schema) !== stableStringify(next.parameter_schema),
    safeToApplyWithoutDisconnecting: !removedInputs.length && !removedOutputs.length,
  };
}

export function layoutFromNodes(nodes: Node<CanvasNodeData>[]): Layout {
  return { positions: Object.fromEntries(nodes.map((node) => [node.id, node.position])) };
}

export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (isRecord(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

export class LatestRequest {
  private revision = 0;
  begin(): number { this.revision += 1; return this.revision; }
  invalidate(): void { this.revision += 1; }
  current(revision: number): boolean { return revision === this.revision; }
}
