import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import type { PreviewResult } from "../../../shared/api";
import type { CanvasNodeData } from "./source";

export function CapabilityNode({
  data,
  selected,
}: NodeProps<Node<CanvasNodeData>>) {
  const inputs =
    data.nodeKind === "OUTPUT" ? ["value"] : (data.inputPorts ?? []);
  const outputs =
    data.nodeKind === "INPUT" ? [data.label] : (data.outputPorts ?? []);
  return (
    <div
      className={`flow-node ${selected ? "selected" : ""} ${data.nodeKind.toLowerCase()}`}
    >
      <strong>{data.label}</strong>
      <small>
        {data.nodeKind === "OPERATOR"
          ? `${data.operatorId}@${data.versionId}`
          : data.nodeKind}{" "}
        · {data.documentId}
      </small>
      {data.when && <small>条件 {data.when}</small>}
      {!!data.scatter?.length && (
        <small>
          {data.scatterMethod ?? "dotproduct"} · {data.scatter.join(", ")}
        </small>
      )}
      {inputs.map((port, index) => (
        <div className="node-port input-port" key={`in:${port}`}>
          <Handle
            type="target"
            position={Position.Left}
            id={`in:${port}`}
            style={{ top: 48 + index * 24 }}
          />
          {port}
        </div>
      ))}
      {outputs.map((port, index) => (
        <div className="node-port output-port" key={`out:${port}`}>
          {port}
          <Handle
            type="source"
            position={Position.Right}
            id={`out:${port}`}
            style={{ top: 48 + index * 24 }}
          />
        </div>
      ))}
    </div>
  );
}

export const workflowNodeTypes = { capability: CapabilityNode };

export function IssueList({
  title,
  issues,
}: {
  title: string;
  issues: PreviewResult["issues"];
}) {
  return (
    <div className="issues">
      <h3>{title}</h3>
      {issues.length === 0 ? (
        <p>没有问题。</p>
      ) : (
        <ul>
          {issues.map((issue, index) => (
            <li key={`${issue.code}:${index}`}>
              <strong>
                {issue.severity} · {issue.code}
              </strong>
              <span>
                {issue.location.step_key ?? "workflow"}
                {issue.location.port ? ` / ${issue.location.port}` : ""}
                {issue.location.pointer ?? ""}
              </span>
              <p>{issue.message}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
