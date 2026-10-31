/** Stable local-operator bindings used by the Task operation panels. */

const implementationIds: Record<string, string> = {
  "timeseries-completeness": "p3-completeness-v1",
  "statistics-linear-trend": "p3-numpy-v1",
  "qgis-buffer": "qgis-buffer-v1",
  "spatial-vector-summary": "spatial-vector-summary-v1",
  "qgis-static-map": "qgis-static-map-v1",
};

export function operatorTarget(operatorId: string) {
  return {
    kind: "OPERATOR",
    operator_id: operatorId,
    version_id: "1.0.0",
    execution_binding: {
      kind: "LOCAL",
      implementation_id: implementationIds[operatorId],
    },
  };
}
