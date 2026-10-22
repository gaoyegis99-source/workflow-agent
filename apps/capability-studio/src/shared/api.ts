export type OperatorDescriptor = {
  operator_id: string;
  version_id: string;
  ownership: string;
  trust_level: string;
  lifecycle: string;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  input_ports: Record<string, unknown>;
  output_ports: Record<string, unknown>;
  implementations: Array<Record<string, unknown>>;
  parameter_schema: Record<string, unknown>;
  server_validation_required: boolean;
  descriptor_digest: string;
};

export type AuthoringDescriptor = {
  operator_catalog_digest: string;
  semantic_contract_digest: string;
  descriptor_digest: string;
  operators: OperatorDescriptor[];
};

export type WorkflowMetadata = { title: string; description: string; labels: string[] };
export type WorkflowRevision = {
  project_id: string;
  definition_id: string;
  revision: number;
  source_digest: string;
  source: Record<string, unknown>;
  metadata: WorkflowMetadata;
  layout: Record<string, unknown>;
  layout_digest: string;
};

export type PreviewResult = {
  status: "COMPATIBLE" | "INCOMPATIBLE" | "REQUIRES_BINDING";
  source_digest: string;
  bindings_digest: string;
  semantic_contract_digest: string;
  catalog_digest: string;
  nodes: Array<{
    step_key: string;
    document_id: string;
    call_path: string[];
    node_kind: "OPERATOR" | "SUBWORKFLOW";
    status: string;
    outputs: Record<string, unknown>;
  }>;
  issues: Array<{ code: string; severity: string; message: string; location: { step_key?: string; port?: string; pointer?: string } }>;
};

export type AuthoringSourceImport = {
  source: Record<string, unknown>;
  source_digest: string;
  format: "JSON" | "YAML";
  editable: true;
};

export type ValidationView = {
  validation_id: string;
  status: "QUEUED" | "RUNNING" | "PASSED" | "REJECTED" | "ERROR";
  status_url: string;
  current_valid?: boolean;
  stale_reasons?: string[];
  issues?: PreviewResult["issues"];
};
export type WorkflowDefinition = {
  project_id: string;
  definition_id: string;
  stable_key: string;
  head_revision: number;
  head: WorkflowRevision;
};

export type WorkflowCandidate = {
  candidate_id: string;
  status: "QUEUED" | "VALIDATING" | "READY" | "REJECTED";
  revision: number;
  source_digest: string;
  package_ref?: { package_id: string; version_id: string; manifest_digest: string };
  error?: { code?: string; message?: string };
  current_valid: boolean;
  stale_reasons: string[];
};

export type WorkflowTrial = {
  trial_id: string;
  candidate_id: string;
  task_id: string;
  action_id: string;
  execution_id?: string;
  execution_outcome: string;
  trial_case_digest: string;
  case_verdict: "PENDING" | "PASS" | "FAIL" | "ERROR";
  case_result?: Record<string, unknown>;
  case_result_digest?: string;
  error_category?: string;
};

export type WorkflowRelease = {
  release_id: string;
  definition_id: string;
  stable_key: string;
  metadata: WorkflowMetadata;
  version_label: string;
  candidate_id: string;
  revision: number;
  source_digest: string;
  source: Record<string, unknown>;
  package_ref: { package_id: string; version_id: string; manifest_digest: string };
  trial_ids: string[];
  context_digest: string;
  lifecycle: "ACTIVE" | "DISABLED" | "REVOKED";
  lifecycle_revision: number;
  lifecycle_reason: string;
  current_executable: boolean;
  unavailable_reasons: string[];
  qualification: "SAMPLE_VERIFIED";
};

export type WorkflowReleaseLifecycle = {
  event_id: string;
  release_id: string;
  lifecycle_revision: number;
  previous_lifecycle?: "ACTIVE" | "DISABLED" | "REVOKED";
  lifecycle: "ACTIVE" | "DISABLED" | "REVOKED";
  reason: string;
  created_by: string;
  created_at: string;
};

export type CommandReceipt = {
  command_id: string;
  status: "RECORDED" | "DISPATCHING" | "RESOLVED" | "REJECTED" | "RECONCILING";
  action_id?: string;
  status_url: string;
  request_id: string;
};

export function releaseExecutionBody(
  task: TaskSummary,
  inputs: Record<string, unknown>,
  maxNodeExecutions: number,
) {
  return {
    task_id: task.task_id,
    expected_spec_revision: task.spec_revision,
    inputs,
    max_node_executions: maxNodeExecutions,
  };
}

export type TaskSummary = {
  task_id: string;
  spec_revision: number;
  lifecycle: string;
  control: string;
};

export type AssetSummary = {
  ref: { project_id: string; asset_id: string; version_id: string; version_manifest_digest: string };
  manifest: { representations?: Array<{ representation_id: string; digest: string; schema_id?: string }> };
};

export type OperatorProposal = {
  proposal_id: string;
  stable_key: string;
  head_revision: number;
  status: "DRAFT" | "SUBMITTED" | "CHANGES_REQUESTED" | "APPROVED" | "REJECTED";
  head: {
    revision: number;
    materials: Record<string, unknown>;
    materials_digest: string;
    created_by: string;
    created_at: string;
  };
  revisions: Array<{
    revision: number;
    materials: Record<string, unknown>;
    materials_digest: string;
    created_by: string;
    created_at: string;
  }>;
  review_events: Array<{
    event_id: string;
    revision: number;
    event_type: string;
    reason: string;
    actor_kind: string;
    actor_id: string;
    evidence_digest?: string;
    created_at: string;
  }>;
  deployment_receipts: Array<{
    receipt_id: string;
    revision: number;
    release_digest: string;
    operator_catalog_digest: string;
    created_at: string;
  }>;
  deployment_match: {
    status: string;
    current_executable: boolean;
    reasons: string[];
    current_release_digest?: string;
    current_catalog_digest: string;
  };
};

export async function request<T>(
  token: string,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body && typeof options.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { ...options, headers });
  } catch (cause) {
    throw new ApiError(0, "NETWORK_ERROR", "连接中断；当前显示的是上次确认状态。恢复连接后可重试原操作。", cause);
  }
  if (!response.ok) {
    const error = (await response.json().catch(() => ({}))) as { code?: string; message?: string };
    const byCode: Record<string, string> = {
      AUTHENTICATION_REQUIRED: "令牌无效、已撤销或已过期，请重新连接。",
      SCOPE_DENIED: "当前项目角色不足以执行此操作。",
      BUDGET_BLOCKED: "预算或在途名额不足，请检查任务预算。",
      IDEMPOTENCY_CONFLICT: "同一操作键已用于其他内容，请刷新后重试。",
      REVISION_CONFLICT: "资源已被其他操作修改，请刷新后重试。",
      DEPENDENCY_UNAVAILABLE: "依赖服务暂不可用；当前状态尚未重新确认。",
    };
    const fallback: Record<number, string> = {
      401: "令牌无效、已撤销或已过期，请重新连接。",
      403: "当前项目角色不足以执行此操作。",
      409: "资源已被其他操作修改，请刷新后重试。",
      422: "提交内容未通过校验，请检查字段。",
      503: "依赖服务暂不可用；当前状态尚未重新确认。",
    };
    const code = error.code ?? `HTTP_${response.status}`;
    throw new ApiError(response.status, code, byCode[code] ?? error.message ?? fallback[response.status] ?? `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    options?: unknown,
  ) {
    super(message, options ? { cause: options } : undefined);
    this.name = "ApiError";
  }

  get retryable() {
    return this.status === 0 || this.status === 503 || this.status >= 500;
  }
}

const pendingKeys = new Map<string, string>();

/** Keep one key across ambiguous network failures; release it after a server decision. */
export async function mutate<T>(
  token: string,
  path: string,
  body: unknown,
  method = "POST",
): Promise<T> {
  const signature = `${method}:${path}:${JSON.stringify(body)}`;
  const key = pendingKeys.get(signature) ?? newKey();
  pendingKeys.set(signature, key);
  try {
    const value = await request<T>(token, path, {
      method,
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(body),
    });
    pendingKeys.delete(signature);
    return value;
  } catch (error) {
    if (!(error instanceof ApiError) || !error.retryable) pendingKeys.delete(signature);
    throw error;
  }
}

export async function uploadBytes<T>(token: string, path: string, bytes: ArrayBuffer): Promise<T> {
  return request<T>(token, path, { method: "PUT", body: bytes });
}

export async function downloadRepresentation(
  token: string,
  projectId: string,
  assetId: string,
  versionId: string,
  representationId: string,
): Promise<Blob> {
  const response = await fetch(
    `/api/v1/projects/${projectId}/assets/${assetId}/versions/${versionId}/representations/${representationId}/content`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) throw new ApiError(response.status, `HTTP_${response.status}`, "下载失败，请刷新并确认权限。");
  return response.blob();
}

export function newKey(): string {
  return crypto.randomUUID().replaceAll("-", "");
}
