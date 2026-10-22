import { describe, expect, it } from "vitest";
import { canvasToSource, compareOperatorVersions, emptyPackage, LatestRequest, sourceToCanvas, stableStringify } from "./source";

const target = { kind: "OPERATOR" as const, operator_id: "table-profile", version_id: "1.0.0", execution_binding: { kind: "LOCAL" as const, implementation_id: "table-profile-native-v1" } };
const descriptors = { "table-profile@1.0.0": { input_ports: { table: {} }, output_ports: { report: {}, table: {}, method: {} }, target } };

describe("bounded source projection", () => {
  it("round trips port order, identity, fixed values, omitted and null values", () => {
    const source = emptyPackage("profile");
    source.documents.main.inputs = { source_table: "File" };
    source.implementations.profile_impl = target;
    source.documents.main.steps.profile = {
      run: "profile_impl", in: { table: "source_table", parameters: { default: { fields: ["f0"], optional: null, exact_integer: "9007199254740993" } } }, out: ["method", "report", "table"],
    };
    source.documents.main.outputs = { result: { type: "File", outputSource: "profile/report" } };
    const projection = sourceToCanvas(source, {});
    expect(projection.editable).toBe(true);
    const rebuilt = canvasToSource(projection, descriptors);
    expect(stableStringify(rebuilt)).toBe(stableStringify(source));
  });

  it("round trips subworkflows, conditions and all scatter modes without losing identity", () => {
    const source = emptyPackage("advanced");
    source.documents.main.inputs = { tables: { type: "array", items: "File" }, enabled: "boolean" };
    source.documents.main.requirements = { SubworkflowFeatureRequirement: {}, ScatterFeatureRequirement: {} };
    source.documents.sub = {
      cwlVersion: "v1.2", class: "Workflow", inputs: { table: "File", enabled: "boolean" },
      outputs: { report: { type: ["null", "File"], outputSource: "profile/report" } },
      steps: { profile: { run: "profile_impl", in: { table: "table", parameters: { default: { fields: ["f0"] } } }, out: ["report"] } },
    };
    source.implementations.profile_impl = target;
    source.documents.main.steps.each = {
      run: "sub", in: { table: "tables", enabled: "enabled" }, out: ["report"],
      scatter: "table", scatterMethod: "nested_crossproduct", when: "$(inputs.enabled)",
    };
    source.documents.main.outputs = { reports: { type: { type: "array", items: ["null", "File"] }, outputSource: "each/report" } };
    const projection = sourceToCanvas(source, {}, descriptors);
    expect(projection.editable).toBe(true);
    expect(projection.nodes.some((node) => node.data.nodeKind === "SUBWORKFLOW")).toBe(true);
    expect(stableStringify(canvasToSource(projection, descriptors))).toBe(stableStringify(source));
  });

  it("keeps unknown features and unsafe integers read only", () => {
    const source = emptyPackage("unsafe");
    source.documents.main.inputs = { value: 9007199254740992 };
    expect(sourceToCanvas(source, {}).editable).toBe(false);
  });

  it("shows operator version changes before applying them", () => {
    const change = compareOperatorVersions(
      { version_id: "1.0.0", input_ports: { table: {} }, output_ports: { result: {} }, parameter_schema: { type: "object" } },
      { version_id: "2.0.0", input_ports: { table: {}, config: {} }, output_ports: { report: {} }, parameter_schema: { type: "object", required: ["mode"] } },
    );
    expect(change.addedInputs).toEqual(["config"]);
    expect(change.removedOutputs).toEqual(["result"]);
    expect(change.safeToApplyWithoutDisconnecting).toBe(false);
  });

  it("does not let an older preview replace a newer edit", () => {
    const requests = new LatestRequest();
    const oldRequest = requests.begin();
    const newRequest = requests.begin();
    expect(requests.current(oldRequest)).toBe(false);
    expect(requests.current(newRequest)).toBe(true);
    requests.invalidate();
    expect(requests.current(newRequest)).toBe(false);
  });
});
