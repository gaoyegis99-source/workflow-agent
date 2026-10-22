export type Role = "VIEWER" | "EDITOR" | "OWNER";

export type Project = {
  project_id: string;
  name: string;
  role: Role;
};

export type CountGroup = Record<string, number>;

export type WorkspaceSummary = {
  project: Project;
  role: Role;
  phase: string;
  release_digest?: string | null;
  as_of: string;
  resources: {
    assets: number;
    workflow_definitions: number;
    workflow_releases: number;
    operator_versions: number;
    operator_materials: number;
  };
  tasks: CountGroup;
  executions: CountGroup;
};

export type AssetRef = {
  project_id: string;
  asset_id: string;
  version_id: string;
  version_manifest_digest: string;
};

export type Representation = {
  representation_id: string;
  digest: string;
  schema_id?: string;
  media_type?: string;
};

export type Asset = {
  ref: AssetRef;
  manifest: {
    kind: string;
    driver?: string;
    row_count?: number;
    fields?: Array<Record<string, unknown>>;
    representations?: Representation[];
    spatial?: Record<string, unknown>;
  };
  semantic_revisions?: Array<{
    revision: number;
    payload: unknown;
    created_at?: string;
  }>;
};

export type UploadView = {
  upload_id: string;
  filename: string;
  format: string;
  size: number;
  digest: string;
  status: string;
  error?: { message: string };
};

export type Task = {
  task_id: string;
  project_id: string;
  spec_revision: number;
  control_version: number;
  lifecycle: string;
  control: string;
  cleanup_status: string;
  execution_mode: "DIRECT" | "AGENT";
  created_at: string;
  completion_validity?: string | null;
  spec: {
    goal: string;
    requirements: string[];
    acceptance_spec?: Record<string, unknown> | null;
  };
};

export type ExecutionSummary = {
  action_id: string;
  execution_id?: string | null;
  task_id: string;
  state: string;
  spec_revision: number;
  decision_source: string;
  target_kind: string;
  target_id: string;
  stopped: boolean;
  cancel_requested: boolean;
};

export type Page<T> = {
  items: T[];
  next_cursor?: string | null;
};
