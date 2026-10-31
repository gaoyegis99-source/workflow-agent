/** Pure descriptor-to-canvas adapters for the Workflow Studio. */

import type {
  OperatorDescriptor,
  WorkflowMetadata,
} from "../../../shared/api";
import type { OperatorTarget } from "./source";

export const emptyMetadata: WorkflowMetadata = {
  title: "",
  description: "",
  labels: [],
};

export function trustedTarget(
  operator: OperatorDescriptor,
): OperatorTarget | null {
  const implementation = operator.implementations.find(
    (item) => item.binding_kind === "LOCAL",
  );
  if (!implementation || typeof implementation.implementation_id !== "string")
    return null;
  return {
    kind: "OPERATOR",
    operator_id: operator.operator_id,
    version_id: operator.version_id,
    execution_binding: {
      kind: "LOCAL",
      implementation_id: implementation.implementation_id,
    },
  };
}

export function descriptorMap(operators: OperatorDescriptor[]) {
  return Object.fromEntries(
    operators.flatMap((operator) => {
      const target = trustedTarget(operator);
      return target
        ? [
            [
              `${operator.operator_id}@${operator.version_id}`,
              {
                input_ports: operator.input_ports,
                output_ports: operator.output_ports,
                parameter_schema: operator.parameter_schema,
                target,
              },
            ],
          ]
        : [];
    }),
  );
}
