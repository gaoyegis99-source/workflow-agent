import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Search, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { mutate, request } from "../../shared/api";
import { useCursorPages } from "../../shared/pagination";
import {
  Badge,
  Banner,
  Button,
  Card,
  ConfirmDialog,
  DataTable,
  EmptyState,
  Field,
  JsonView,
  MetricCard,
  Modal,
  Select,
  Skeleton,
  Tabs,
} from "../../shared/ui";
import { useSession } from "../../core/session";
import {
  errorText,
  formatDate,
  PageError,
  PageHeader,
  PageLoading,
  PermissionButton,
  resolveRole,
  targetLabel,
  useProjectId,
  useSummary,
  useToast,
} from "../../core/support";
import type { Asset, Page, Role, Task } from "../../shared/types";

function assetKey(asset: Asset) {
  return `${encodeURIComponent(asset.ref.asset_id)}:${encodeURIComponent(asset.ref.version_id)}`;
}

export function TasksPage() {
  const session = useSession();
  const { token } = session;
  const projectId = useProjectId();
  const summary = useSummary();
  const navigate = useNavigate();
  const client = useQueryClient();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const [state, setState] = useState("ALL");
  const [open, setOpen] = useState(false);
  const role = resolveRole(summary.data, session.projects, projectId);
  const tasks = useQuery({
    queryKey: ["tasks", projectId],
    queryFn: () => request<Page<Task>>(token, `/projects/${projectId}/tasks`),
    refetchInterval: 10_000,
  });
  const rows = (tasks.data?.items ?? [])
    .filter(
      (task) =>
        (state === "ALL" ||
          task.lifecycle === state ||
          task.control === state) &&
        (task.spec.goal.toLowerCase().includes(search.toLowerCase()) ||
          task.task_id.includes(search)),
    )
    .map((task) => ({
      ...task,
      id: task.task_id,
      goal: task.spec.goal,
      revision: task.spec_revision,
      mode: task.execution_mode,
      status: `${task.lifecycle} / ${task.control}`,
    }));
  return (
    <div className="page">
      <PageHeader
        title="任务"
        description="冻结研究范围与预算，并在同一处跟踪授权、运行、交付和验收。"
        action={
          <PermissionButton role={role} onClick={() => setOpen(true)}>
            <Plus size={16} />
            创建任务
          </PermissionButton>
        }
      />
      <Card className="table-card">
        <div className="table-toolbar">
          <div className="search-box">
            <Search size={16} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索目标或任务 ID"
            />
          </div>
          <Select
            value={state}
            onChange={(event) => setState(event.target.value)}
          >
            <option value="ALL">全部状态</option>
            <option>OPEN</option>
            <option>PAUSED</option>
            <option>COMPLETED</option>
            <option>CANCELLED</option>
          </Select>
        </div>
        {tasks.isLoading ? (
          <Skeleton lines={7} />
        ) : tasks.error ? (
          <PageError
            embedded
            error={tasks.error}
            retry={() => void tasks.refetch()}
          />
        ) : (
          <DataTable
            rows={rows as Array<Record<string, unknown>>}
            onRowClick={(row) => navigate(String(row.task_id))}
            columns={[
              {
                key: "goal",
                label: "研究目标",
                render: (row) => (
                  <div>
                    <strong>{String(row.goal)}</strong>
                    <small className="block">{String(row.task_id)}</small>
                  </div>
                ),
              },
              {
                key: "mode",
                label: "模式",
                render: (row) => <Badge tone="info">{String(row.mode)}</Badge>,
              },
              {
                key: "status",
                label: "状态",
                render: (row) => (
                  <>
                    <Badge>{String(row.lifecycle)}</Badge>{" "}
                    <Badge>{String(row.control)}</Badge>
                  </>
                ),
              },
              { key: "revision", label: "范围修订" },
              {
                key: "created_at",
                label: "创建时间",
                render: (row) => formatDate(String(row.created_at)),
              },
            ]}
          />
        )}
      </Card>
      <TaskWizard
        open={open}
        role={role}
        onClose={() => setOpen(false)}
        onDone={(task) => {
          setOpen(false);
          void client.invalidateQueries({ queryKey: ["tasks", projectId] });
          toast.success("任务已创建，范围和预算已冻结。");
          navigate(task.task_id);
        }}
      />
      {toast.node}
    </div>
  );
}

function TaskWizard({
  open,
  role,
  onClose,
  onDone,
}: {
  open: boolean;
  role: Role;
  onClose: () => void;
  onDone: (task: Task) => void;
}) {
  const { token } = useSession();
  const projectId = useProjectId();
  const [step, setStep] = useState(1);
  const [template, setTemplate] = useState<
    "GENERIC" | "TIMESERIES" | "SPATIAL"
  >("GENERIC");
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<"DIRECT" | "AGENT">("DIRECT");
  const [assetId, setAssetId] = useState("");
  const [asset, setAsset] = useState<Asset | null>(null);
  const [groupField, setGroupField] = useState("");
  const [timeField, setTimeField] = useState("");
  const [valueField, setValueField] = useState("");
  const [groups, setGroups] = useState('["A", "B"]');
  const [start, setStart] = useState("2024-01-01T00:00:00+00:00");
  const [end, setEnd] = useState("2024-01-05T00:00:00+00:00");
  const [frequency, setFrequency] = useState("day");
  const [minSamples, setMinSamples] = useState(3);
  const [coverage, setCoverage] = useState(1);
  const [allowPartial, setAllowPartial] = useState(false);
  const [budget, setBudget] = useState(3);
  const [concurrency, setConcurrency] = useState(1);
  const [modelCalls, setModelCalls] = useState(30);
  const [decisionFrames, setDecisionFrames] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const assets = useCursorPages<Asset>(
    token,
    `/projects/${projectId}/assets`,
    ["task-wizard-assets", projectId],
    { enabled: open && step === 2 && template !== "GENERIC" },
  );
  const candidates = assets.items.filter((item) =>
    template === "TIMESERIES"
      ? item.manifest.kind === "OBSERVATIONS"
      : ["SPATIAL_VECTOR", "OPERATOR_RESULT"].includes(item.manifest.kind),
  );
  useEffect(() => {
    if (!open) {
      setStep(1);
      setError("");
      setAsset(null);
    }
  }, [open]);
  useEffect(() => {
    if (!assetId || !open) return;
    const summaryAsset = candidates.find((item) => assetKey(item) === assetId);
    if (!summaryAsset) return;
    void request<Asset>(
      token,
      `/projects/${projectId}/assets/${summaryAsset.ref.asset_id}/versions/${summaryAsset.ref.version_id}`,
    )
      .then((next) => {
        setAsset(next);
        const fields = next.manifest.fields ?? [];
        setGroupField(String(fields[0]?.field_id ?? ""));
        setTimeField(String(fields[1]?.field_id ?? ""));
        setValueField(String(fields[2]?.field_id ?? ""));
      })
      .catch((nextError) => setError(errorText(nextError)));
  }, [assetId, open, projectId, token, assets.data]);
  function validInput() {
    if (template === "GENERIC") return true;
    if (!asset) return false;
    return (
      template === "SPATIAL" ||
      Boolean(
        groupField &&
          timeField &&
          valueField &&
          asset.semantic_revisions?.length,
      )
    );
  }
  async function submit() {
    setBusy(true);
    setError("");
    try {
      const spec: Record<string, unknown> = { goal, requirements: [] };
      if (template === "TIMESERIES" && asset) {
        const representation = asset.manifest.representations?.find(
          (item) => item.representation_id === "normalized",
        );
        if (!representation || !asset.semantic_revisions?.length)
          throw new Error("时间序列资产需要 normalized 表示和语义修订。");
        const input = {
          asset: asset.ref,
          representation_id: representation.representation_id,
          representation_digest: representation.digest,
          semantic_revision: asset.semantic_revisions.at(-1)?.revision,
          field_bindings: { groupField, timeField, valueField },
        };
        spec.acceptance_spec = {
          profile: "p3-timeseries-v1",
          input,
          parameters: {
            groups: JSON.parse(groups),
            start,
            end,
            frequency,
            min_samples: minSamples,
          },
          required_outputs: [
            "timeseries-completeness",
            "statistics-linear-trend",
          ],
          allow_not_evaluable: allowPartial,
          minimum_evaluable: coverage,
        };
      }
      const body: Record<string, unknown> = {
        execution_mode: mode,
        spec,
        budget: { execution_count: budget, max_in_flight: concurrency },
      };
      if (mode === "AGENT")
        body.model_budget = {
          max_model_calls: modelCalls,
          max_input_tokens: 1_000_000,
          max_output_tokens: 100_000,
          max_reasoning_tokens: 50_000,
          max_estimated_cost_microunits: 10_000_000,
          max_agent_wall_seconds: 3600,
          max_decision_frames: decisionFrames,
        };
      const created = await mutate<Task>(
        token,
        `/projects/${projectId}/tasks`,
        body,
      );
      onDone(created);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setBusy(false);
    }
  }
  const footer =
    step === 1 ? (
      <>
        <Button variant="secondary" onClick={onClose}>
          取消
        </Button>
        <Button disabled={!goal.trim()} onClick={() => setStep(2)}>
          下一步
        </Button>
      </>
    ) : step === 2 ? (
      <>
        <Button variant="secondary" onClick={() => setStep(1)}>
          上一步
        </Button>
        <Button disabled={!validInput()} onClick={() => setStep(3)}>
          下一步
        </Button>
      </>
    ) : (
      <>
        <Button variant="secondary" onClick={() => setStep(2)}>
          上一步
        </Button>
        <PermissionButton role={role} busy={busy} onClick={() => void submit()}>
          创建任务
        </PermissionButton>
      </>
    );
  return (
    <Modal open={open} title="创建任务" onClose={onClose} footer={footer}>
      <ol className="steps">
        <li className={step >= 1 ? "active" : ""}>1 范围与模式</li>
        <li className={step >= 2 ? "active" : ""}>2 数据范围</li>
        <li className={step >= 3 ? "active" : ""}>3 预算与确认</li>
      </ol>
      {step === 1 ? (
        <div className="form-grid">
          <Field label="任务模板">
            <Select
              value={template}
              onChange={(event) => {
                const next = event.target.value as typeof template;
                setTemplate(next);
                if (next === "TIMESERIES") setConcurrency(2);
                if (next === "SPATIAL") {
                  setMode("DIRECT");
                  setBudget(3);
                  setConcurrency(2);
                }
              }}
            >
              <option value="GENERIC">通用研究任务</option>
              <option value="TIMESERIES">时间序列验收任务</option>
              <option value="SPATIAL">空间处理任务</option>
            </Select>
          </Field>
          <Field label="研究目标">
            <textarea
              rows={5}
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder="清晰描述期望产出和科学范围"
            />
          </Field>
          <Field label="执行模式">
            <Select
              value={mode}
              disabled={template === "SPATIAL"}
              onChange={(event) =>
                setMode(event.target.value as "DIRECT" | "AGENT")
              }
            >
              <option value="DIRECT">DIRECT · 人工发起执行</option>
              <option value="AGENT">AGENT · 受预算约束的智能执行</option>
            </Select>
          </Field>
        </div>
      ) : null}
      {step === 2 ? (
        template === "GENERIC" ? (
          <Banner>
            通用任务不冻结资产选择；后续授权和执行仍需显式指定精确资源。
          </Banner>
        ) : (
          <div className="form-grid">
            <Field
              label={template === "TIMESERIES" ? "观测资产" : "空间矢量资产"}
            >
              <Select
                value={assetId}
                onChange={(event) => {
                  setAssetId(event.target.value);
                  setAsset(null);
                }}
              >
                <option value="">选择资产</option>
                {candidates.map((item) => (
                  <option value={assetKey(item)} key={assetKey(item)}>
                    {item.ref.asset_id} · v{item.ref.version_id} · {item.manifest.kind}
                  </option>
                ))}
              </Select>
            </Field>
            {template === "TIMESERIES" && asset ? (
              <>
                <div className="form-grid three">
                  <Field label="分组字段">
                    <Select
                      value={groupField}
                      onChange={(event) => setGroupField(event.target.value)}
                    >
                      {(asset.manifest.fields ?? []).map((field) => (
                        <option
                          key={String(field.field_id)}
                          value={String(field.field_id)}
                        >
                          {String(field.name ?? field.field_id)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="时间字段">
                    <Select
                      value={timeField}
                      onChange={(event) => setTimeField(event.target.value)}
                    >
                      {(asset.manifest.fields ?? []).map((field) => (
                        <option
                          key={String(field.field_id)}
                          value={String(field.field_id)}
                        >
                          {String(field.name ?? field.field_id)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="数值字段">
                    <Select
                      value={valueField}
                      onChange={(event) => setValueField(event.target.value)}
                    >
                      {(asset.manifest.fields ?? []).map((field) => (
                        <option
                          key={String(field.field_id)}
                          value={String(field.field_id)}
                        >
                          {String(field.name ?? field.field_id)}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </div>
                <Field label="分组值（JSON）">
                  <input
                    value={groups}
                    onChange={(event) => setGroups(event.target.value)}
                  />
                </Field>
                <div className="form-grid three">
                  <Field label="开始（含）">
                    <input
                      value={start}
                      onChange={(event) => setStart(event.target.value)}
                    />
                  </Field>
                  <Field label="结束（不含）">
                    <input
                      value={end}
                      onChange={(event) => setEnd(event.target.value)}
                    />
                  </Field>
                  <Field label="频率">
                    <input
                      value={frequency}
                      onChange={(event) => setFrequency(event.target.value)}
                    />
                  </Field>
                  <Field label="最少样本">
                    <input
                      type="number"
                      value={minSamples}
                      onChange={(event) =>
                        setMinSamples(Number(event.target.value))
                      }
                    />
                  </Field>
                  <Field label="最低可评估比例">
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step="0.05"
                      value={coverage}
                      onChange={(event) =>
                        setCoverage(Number(event.target.value))
                      }
                    />
                  </Field>
                  <Field label="允许部分不可评估">
                    <Select
                      value={allowPartial ? "yes" : "no"}
                      onChange={(event) =>
                        setAllowPartial(event.target.value === "yes")
                      }
                    >
                      <option value="no">否</option>
                      <option value="yes">是</option>
                    </Select>
                  </Field>
                </div>
                {!asset.semantic_revisions?.length ? (
                  <Banner tone="warning">
                    该资产尚无语义修订，请先在资产详情中声明时间与量纲语义。
                  </Banner>
                ) : null}
              </>
            ) : null}
            {template === "SPATIAL" && asset ? (
              <Banner tone="success">
                已选择 {asset.ref.asset_id}
                。精确图层、算子和地图参数将在授权与执行时冻结。
              </Banner>
            ) : null}
          </div>
        )
      ) : null}
      {step === 3 ? (
        <div className="form-grid two">
          <Field label="执行次数">
            <input
              type="number"
              min={0}
              value={budget}
              onChange={(event) => setBudget(Number(event.target.value))}
            />
          </Field>
          <Field label="最大并发">
            <input
              type="number"
              min={1}
              value={concurrency}
              onChange={(event) => setConcurrency(Number(event.target.value))}
            />
          </Field>
          {mode === "AGENT" ? (
            <>
              <Field label="模型调用上限">
                <input
                  type="number"
                  min={1}
                  value={modelCalls}
                  onChange={(event) =>
                    setModelCalls(Number(event.target.value))
                  }
                />
              </Field>
              <Field label="决策帧上限">
                <input
                  type="number"
                  min={1}
                  value={decisionFrames}
                  onChange={(event) =>
                    setDecisionFrames(Number(event.target.value))
                  }
                />
              </Field>
            </>
          ) : null}
          <Card title="确认范围">
            <dl className="definition-list">
              <div>
                <dt>模板</dt>
                <dd>{template}</dd>
              </div>
              <div>
                <dt>目标</dt>
                <dd>{goal}</dd>
              </div>
              <div>
                <dt>模式</dt>
                <dd>
                  <Badge>{mode}</Badge>
                </dd>
              </div>
              <div>
                <dt>资产</dt>
                <dd>{asset?.ref.asset_id ?? "任务级不冻结"}</dd>
              </div>
              <div>
                <dt>额度</dt>
                <dd>
                  {budget} 次 / {concurrency} 并发
                </dd>
              </div>
            </dl>
          </Card>
        </div>
      ) : null}
      {step === 2 && template !== "GENERIC" && assets.hasNextPage ? (
        <Button
          variant="secondary"
          busy={assets.isFetchingNextPage}
          onClick={() => void assets.fetchNextPage()}
        >
          继续加载资产
        </Button>
      ) : null}
      {error ? <Banner tone="danger">{error}</Banner> : null}
    </Modal>
  );
}

const implementationIds: Record<string, string> = {
  "timeseries-completeness": "p3-completeness-v1",
  "statistics-linear-trend": "p3-numpy-v1",
  "qgis-buffer": "qgis-buffer-v1",
  "spatial-vector-summary": "spatial-vector-summary-v1",
  "qgis-static-map": "qgis-static-map-v1",
};

function operatorTarget(operatorId: string) {
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

function TaskOperations({
  mode,
  task,
  role,
  activities,
  deliverables,
  acceptance,
  grants,
  refresh,
}: {
  mode: "grants" | "runs" | "delivery" | "acceptance";
  task: Task;
  role: Role;
  activities: Array<Record<string, unknown>>;
  deliverables: Array<Record<string, unknown>>;
  acceptance: Array<Record<string, unknown>>;
  grants: Array<Record<string, unknown>>;
  refresh: () => Promise<unknown>;
}) {
  const { token } = useSession();
  const projectId = useProjectId();
  const toast = useToast();
  const [assetId, setAssetId] = useState("");
  const [asset, setAsset] = useState<Asset | null>(null);
  const [layerId, setLayerId] = useState("");
  const [packageId, setPackageId] = useState("");
  const [expiresAt, setExpiresAt] = useState(() =>
    new Date(Date.now() + 3_600_000).toISOString(),
  );
  const [maxNodes, setMaxNodes] = useState(3);
  const [distance, setDistance] = useState(100);
  const [outputCrs, setOutputCrs] = useState("EPSG:3857");
  const [mapTitle, setMapTitle] = useState("地下水空间分析");
  const [busy, setBusy] = useState(false);
  const [deliveryIds, setDeliveryIds] = useState<Record<string, string>>({});
  const [genericExecutions, setGenericExecutions] = useState("");
  const [workflowExecution, setWorkflowExecution] = useState("");
  const [workflowOutputs, setWorkflowOutputs] = useState("[]");
  const [deliverableId, setDeliverableId] = useState("");
  const [review, setReview] = useState({
    qualification: "",
    criteria: "",
    reason: "",
  });
  const assets = useCursorPages<Asset>(
    token,
    `/projects/${projectId}/assets`,
    ["task-operation-assets", projectId],
    { enabled: mode === "grants" || mode === "runs" },
  );
  const packages = useQuery({
    queryKey: ["workflow-packages", projectId],
    queryFn: () =>
      request<Page<Record<string, unknown>>>(
        token,
        `/projects/${projectId}/workflow-packages`,
      ),
    enabled: mode === "grants" || mode === "runs",
  });
  const readyPackages = (packages.data?.items ?? []).filter(
    (item) => item.status === "READY",
  );
  const selectedPackage = readyPackages.find(
    (item) => item.package_id === packageId,
  );
  const acceptanceSpec = task.spec.acceptance_spec as
    | Record<string, unknown>
    | null
    | undefined;
  const requiredOutputs =
    (acceptanceSpec?.required_outputs as string[] | undefined) ?? [];
  const frozenInput = acceptanceSpec?.input as
    | Record<string, unknown>
    | undefined;
  const parameters = acceptanceSpec?.parameters as
    | Record<string, unknown>
    | undefined;
  useEffect(() => {
    if (!assetId) {
      setAsset(null);
      return;
    }
    const summaryAsset = assets.items.find(
      (item) => assetKey(item) === assetId,
    );
    if (!summaryAsset) return;
    void request<Asset>(
      token,
      `/projects/${projectId}/assets/${summaryAsset.ref.asset_id}/versions/${summaryAsset.ref.version_id}`,
    )
      .then((next) => {
        setAsset(next);
        const layers =
          (
            next.manifest.spatial as
              | { layers?: Array<Record<string, unknown>> }
              | undefined
          )?.layers ?? [];
        setLayerId(String(layers[0]?.layer_id ?? ""));
      })
      .catch(toast.failure);
  }, [assetId, projectId, token, assets.data]);
  function spatialSelection() {
    if (!asset || !layerId) throw new Error("请选择空间矢量资产和精确图层。");
    const representation = asset.manifest.representations?.find(
      (item) =>
        item.representation_id === "original" ||
        item.representation_id === "vector",
    );
    if (!representation) throw new Error("资产没有可执行的矢量表示。");
    return {
      asset: asset.ref,
      representation_id: representation.representation_id,
      representation_digest: representation.digest,
      selector: { kind: "VECTOR_LAYER", layer_id: layerId },
      semantic_revision: asset.semantic_revisions?.at(-1)?.revision ?? null,
    };
  }
  async function run(operation: () => Promise<unknown>, message: string) {
    setBusy(true);
    try {
      await operation();
      await refresh();
      toast.success(message);
    } catch (error) {
      toast.failure(error);
    } finally {
      setBusy(false);
    }
  }
  async function createGrant() {
    await run(async () => {
      const targets: Array<Record<string, unknown>> = acceptanceSpec
        ? requiredOutputs.map(operatorTarget)
        : [
            operatorTarget("qgis-buffer"),
            operatorTarget("spatial-vector-summary"),
            operatorTarget("qgis-static-map"),
          ];
      if (selectedPackage)
        targets.push({
          kind: "WORKFLOW",
          package_ref: selectedPackage.package_ref,
        });
      const input = acceptanceSpec ? frozenInput : spatialSelection();
      const resource = input?.asset;
      if (!resource) throw new Error("任务没有可授权的精确资产。");
      await mutate(token, `/projects/${projectId}/grants`, {
        task_id: task.task_id,
        targets,
        resources: [resource],
        derived_outputs: true,
        acceptance_profiles: acceptanceSpec ? ["p3-timeseries-v1"] : [],
        expires_at: expiresAt,
      });
    }, "授权已创建。");
  }
  async function runOperator(operatorId: string) {
    await run(async () => {
      let inputs: Record<string, unknown>;
      let operatorParameters: Record<string, unknown>;
      if (acceptanceSpec) {
        inputs = { observations: frozenInput };
        operatorParameters = parameters ?? {};
      } else {
        inputs = { vector: spatialSelection() };
        operatorParameters =
          operatorId === "qgis-buffer"
            ? { distance, unit: "metre", output_crs: outputCrs, segments: 8 }
            : operatorId === "spatial-vector-summary"
              ? { layer_id: layerId }
              : {
                  layer_id: layerId,
                  template_id: "v7-spatial-basic-v1",
                  output_crs: outputCrs,
                  title: mapTitle,
                  legend: true,
                  palette: "blue",
                };
      }
      await mutate(token, `/projects/${projectId}/executions`, {
        task_id: task.task_id,
        expected_spec_revision: task.spec_revision,
        target: operatorTarget(operatorId),
        inputs,
        parameters: operatorParameters,
      });
    }, `${operatorId} 已提交。`);
  }
  async function runWorkflow() {
    await run(async () => {
      if (!selectedPackage) throw new Error("请选择 READY 工作流。");
      let inputs: Record<string, unknown>;
      if (acceptanceSpec) inputs = { observations: frozenInput, parameters };
      else {
        inputs = {
          vector: spatialSelection(),
          buffer_parameters: {
            distance,
            unit: "metre",
            output_crs: outputCrs,
            segments: 8,
          },
          summary_parameters: { layer_id: layerId },
        };
        if (String(selectedPackage.name).toLowerCase().includes("map"))
          inputs.map_parameters = {
            layer_id: layerId,
            template_id: "v7-spatial-basic-v1",
            output_crs: outputCrs,
            title: mapTitle,
            legend: true,
            palette: "blue",
          };
      }
      await mutate(token, `/projects/${projectId}/executions`, {
        task_id: task.task_id,
        expected_spec_revision: task.spec_revision,
        target: { kind: "WORKFLOW", package_ref: selectedPackage.package_ref },
        inputs,
        max_node_executions: maxNodes,
      });
    }, "工作流执行已提交。");
  }
  async function createDeliverable() {
    await run(async () => {
      const executions = acceptanceSpec
        ? requiredOutputs
            .map((operator) => deliveryIds[operator])
            .filter(Boolean)
        : genericExecutions
            .split(",")
            .map((item) => item.trim())
            .filter(Boolean);
      if (
        !executions.length ||
        (acceptanceSpec && executions.length !== requiredOutputs.length)
      )
        throw new Error("请为每项必要输出选择成功执行。");
      await mutate(
        token,
        `/projects/${projectId}/tasks/${task.task_id}/deliverables`,
        { expected_spec_revision: task.spec_revision, executions },
      );
    }, "候选交付已登记。");
  }
  async function createWorkflowDeliverable() {
    await run(async () => {
      if (!workflowExecution) throw new Error("请选择成功的工作流执行。");
      const parsed = JSON.parse(workflowOutputs) as unknown;
      if (
        !Array.isArray(parsed) ||
        !parsed.length ||
        parsed.some((item) => typeof item !== "string" || !item.trim())
      )
        throw new Error("工作流输出必须是非空字符串数组。");
      await mutate(
        token,
        `/projects/${projectId}/tasks/${task.task_id}/deliverables`,
        {
          expected_spec_revision: task.spec_revision,
          workflow_outputs: parsed.map((output) => ({
            execution_id: workflowExecution,
            output: output.trim(),
          })),
        },
      );
    }, "工作流候选交付已登记。");
  }
  async function requestAcceptance(retryOf?: string) {
    await run(
      async () => {
        const selected = retryOf
          ? acceptance.find((item) => item.acceptance_id === retryOf)
          : deliverables.find((item) => item.deliverable_id === deliverableId);
        const candidateId = retryOf ? selected?.deliverable_id : deliverableId;
        if (!candidateId) throw new Error("请选择当前范围修订的候选交付。");
        await mutate(
          token,
          `/projects/${projectId}/tasks/${task.task_id}/acceptance`,
          {
            expected_spec_revision: task.spec_revision,
            deliverable_id: candidateId,
            ...(retryOf ? { retry_of: retryOf } : {}),
          },
        );
      },
      retryOf ? "验收重试已提交。" : "验收已提交。",
    );
  }
  async function submitReview(report: Record<string, unknown>) {
    await run(async () => {
      const coverage = report.coverage_plan as
        | { revision?: number; items?: Array<Record<string, unknown>> }
        | undefined;
      const item = coverage?.items?.find(
        (value) =>
          value.check_type === "HUMAN_REVIEW" &&
          value.status === "REVIEW_REQUIRED",
      );
      if (!coverage?.revision || !item?.item_id)
        throw new Error("没有可提交的限定人工评审项。");
      await mutate(
        token,
        `/projects/${projectId}/tasks/${task.task_id}/acceptance/${String(report.acceptance_id)}/reviews`,
        {
          expected_spec_revision: task.spec_revision,
          coverage_plan_revision: coverage.revision,
          item_id: item.item_id,
          qualification: review.qualification,
          criteria: review.criteria,
          decision: "PASS",
          reason: review.reason,
        },
      );
    }, "人工评审决定已提交。");
  }
  const layers =
    (
      asset?.manifest.spatial as
        | { layers?: Array<Record<string, unknown>> }
        | undefined
    )?.layers ?? [];
  const assetPager = assets.hasNextPage ? (
    <Button
      variant="secondary"
      busy={assets.isFetchingNextPage}
      onClick={() => void assets.fetchNextPage()}
    >
      继续加载资产
    </Button>
  ) : null;
  const successful = activities.filter(
    (item) => item.state === "SUCCEEDED" && item.execution_id,
  );
  const successfulWorkflows = successful.filter(
    (item) =>
      (
        (item.request as Record<string, unknown> | undefined)?.target as
          | Record<string, unknown>
          | undefined
      )?.kind === "WORKFLOW",
  );
  if (mode === "grants")
    return (
      <>
        <Card
          title="创建最小范围授权"
          description="仅 OWNER 可创建；服务端在每次执行与发布前重新鉴权。"
        >
          <div className="form-grid two">
            {!acceptanceSpec ? (
              <>
                <Field label="空间资产">
                  <Select
                    value={assetId}
                    onChange={(event) => setAssetId(event.target.value)}
                  >
                    <option value="">选择资产</option>
                    {assets.items
                      .filter((item) =>
                        ["SPATIAL_VECTOR", "OPERATOR_RESULT"].includes(
                          item.manifest.kind,
                        ),
                      )
                      .map((item) => (
                        <option
                          value={assetKey(item)}
                          key={assetKey(item)}
                        >
                          {item.ref.asset_id} · v{item.ref.version_id}
                        </option>
                      ))}
                  </Select>
                  {assetPager}
                </Field>
                <Field label="精确图层">
                  <Select
                    value={layerId}
                    onChange={(event) => setLayerId(event.target.value)}
                  >
                    <option value="">选择图层</option>
                    {layers.map((layer) => (
                      <option
                        key={String(layer.layer_id)}
                        value={String(layer.layer_id)}
                      >
                        {String(layer.name ?? layer.layer_id)}
                      </option>
                    ))}
                  </Select>
                </Field>
              </>
            ) : (
              <Banner tone="success">
                授权资源来自冻结的验收范围，不允许客户端替换。
              </Banner>
            )}
            <Field label="附加 READY 工作流">
              <Select
                value={packageId}
                onChange={(event) => setPackageId(event.target.value)}
              >
                <option value="">不附加工作流</option>
                {readyPackages.map((item) => (
                  <option
                    key={String(item.package_id)}
                    value={String(item.package_id)}
                  >
                    {String(item.name)} ·{" "}
                    {String(
                      (item.package_ref as Record<string, unknown>)?.version_id,
                    )}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="过期时间">
              <input
                value={expiresAt}
                onChange={(event) => setExpiresAt(event.target.value)}
              />
            </Field>
          </div>
          <PermissionButton
            role={role}
            required="OWNER"
            busy={busy}
            onClick={() => void createGrant()}
          >
            创建授权
          </PermissionButton>
        </Card>
        {toast.node}
      </>
    );
  if (mode === "runs")
    return (
      <>
        <Card
          title="提交执行"
          description={
            task.execution_mode === "AGENT"
              ? "AGENT 任务由受预算约束的 Agent 决策，人工执行入口保持禁用。"
              : "执行仍受任务预算、控制状态和精确授权约束。"
          }
        >
          <div className="form-grid two">
            {!acceptanceSpec ? (
              <>
                <Field label="空间资产">
                  <Select
                    value={assetId}
                    onChange={(event) => setAssetId(event.target.value)}
                  >
                    <option value="">选择资产</option>
                    {assets.items
                      .filter((item) =>
                        ["SPATIAL_VECTOR", "OPERATOR_RESULT"].includes(
                          item.manifest.kind,
                        ),
                      )
                      .map((item) => (
                        <option
                          value={assetKey(item)}
                          key={assetKey(item)}
                        >
                          {item.ref.asset_id} · v{item.ref.version_id}
                        </option>
                      ))}
                  </Select>
                  {assetPager}
                </Field>
                <Field label="精确图层">
                  <Select
                    value={layerId}
                    onChange={(event) => setLayerId(event.target.value)}
                  >
                    <option value="">选择图层</option>
                    {layers.map((layer) => (
                      <option
                        key={String(layer.layer_id)}
                        value={String(layer.layer_id)}
                      >
                        {String(layer.name ?? layer.layer_id)}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Buffer 距离（米）">
                  <input
                    type="number"
                    min={0.01}
                    value={distance}
                    onChange={(event) =>
                      setDistance(Number(event.target.value))
                    }
                  />
                </Field>
                <Field label="输出 CRS">
                  <input
                    value={outputCrs}
                    onChange={(event) => setOutputCrs(event.target.value)}
                  />
                </Field>
                <Field label="地图标题">
                  <input
                    value={mapTitle}
                    onChange={(event) => setMapTitle(event.target.value)}
                  />
                </Field>
              </>
            ) : null}
            <Field label="READY 工作流">
              <Select
                value={packageId}
                onChange={(event) => setPackageId(event.target.value)}
              >
                <option value="">选择工作流</option>
                {readyPackages.map((item) => (
                  <option
                    key={String(item.package_id)}
                    value={String(item.package_id)}
                  >
                    {String(item.name)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="节点额度">
              <input
                type="number"
                min={1}
                max={1000}
                value={maxNodes}
                onChange={(event) => setMaxNodes(Number(event.target.value))}
              />
            </Field>
          </div>
          <div className="actions wrap">
            {(acceptanceSpec
              ? requiredOutputs
              : ["qgis-buffer", "spatial-vector-summary", "qgis-static-map"]
            ).map((operator) => (
              <PermissionButton
                key={operator}
                role={role}
                busy={busy}
                disabled={
                  task.execution_mode === "AGENT" ||
                  task.control !== "ACTIVE" ||
                  task.lifecycle !== "OPEN"
                }
                onClick={() => void runOperator(operator)}
              >
                {operator}
              </PermissionButton>
            ))}
            <PermissionButton
              role={role}
              variant="secondary"
              busy={busy}
              disabled={
                !selectedPackage ||
                task.execution_mode === "AGENT" ||
                task.control !== "ACTIVE"
              }
              onClick={() => void runWorkflow()}
            >
              运行工作流
            </PermissionButton>
          </div>
        </Card>
        {toast.node}
      </>
    );
  if (mode === "delivery")
    return (
      <>
        <Card
          title="登记算子交付"
          description="只能引用当前任务已成功发布的执行成果。"
        >
          <div className="form-grid">
            {acceptanceSpec ? (
              requiredOutputs.map((operator) => (
                <Field label={operator} key={operator}>
                  <Select
                    value={deliveryIds[operator] ?? ""}
                    onChange={(event) =>
                      setDeliveryIds((value) => ({
                        ...value,
                        [operator]: event.target.value,
                      }))
                    }
                  >
                    <option value="">选择成功执行</option>
                    {successful
                      .filter((item) => targetLabel(item.request) === operator)
                      .map((item) => (
                        <option
                          key={String(item.execution_id)}
                          value={String(item.execution_id)}
                        >
                          {String(item.execution_id)}
                        </option>
                      ))}
                  </Select>
                </Field>
              ))
            ) : (
              <Field label="执行 ID（逗号分隔，最多 2 个）">
                <input
                  value={genericExecutions}
                  onChange={(event) => setGenericExecutions(event.target.value)}
                  placeholder={successful
                    .filter((item) => !successfulWorkflows.includes(item))
                    .map((item) => item.execution_id)
                    .join(", ")}
                />
              </Field>
            )}
            <PermissionButton
              role={role}
              busy={busy}
              onClick={() => void createDeliverable()}
            >
              登记候选交付
            </PermissionButton>
          </div>
        </Card>
        {successfulWorkflows.length ? (
          <Card
            title="登记工作流交付"
            description="明确选择工作流发布端口；服务端仍会校验执行归属、端口与不可变输出。"
          >
            <div className="form-grid">
              <Field label="成功工作流执行">
                <Select
                  value={workflowExecution}
                  onChange={(event) => setWorkflowExecution(event.target.value)}
                >
                  <option value="">选择执行</option>
                  {successfulWorkflows.map((item) => (
                    <option
                      key={String(item.execution_id)}
                      value={String(item.execution_id)}
                    >
                      {String(item.execution_id)} · {targetLabel(item.request)}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="发布输出（JSON 字符串数组）">
                <textarea
                  rows={4}
                  value={workflowOutputs}
                  onChange={(event) => setWorkflowOutputs(event.target.value)}
                  placeholder={'["complete_result", "trend_result"]'}
                />
              </Field>
              <PermissionButton
                role={role}
                busy={busy}
                onClick={() => void createWorkflowDeliverable()}
              >
                登记工作流交付
              </PermissionButton>
            </div>
          </Card>
        ) : null}
        {toast.node}
      </>
    );
  return (
    <>
      <Card
        title="请求验收"
        description="验收由可信 Checker 与限定人工评审共同决定。"
      >
        <div className="form-grid">
          <Field label="当前候选交付">
            <Select
              value={deliverableId}
              onChange={(event) => setDeliverableId(event.target.value)}
            >
              <option value="">选择候选</option>
              {deliverables
                .filter((item) => item.spec_revision === task.spec_revision)
                .map((item) => (
                  <option
                    key={String(item.deliverable_id)}
                    value={String(item.deliverable_id)}
                  >
                    {String(item.deliverable_id)}
                  </option>
                ))}
            </Select>
          </Field>
          <PermissionButton
            role={role}
            busy={busy}
            disabled={!deliverableId}
            onClick={() => void requestAcceptance()}
          >
            提交验收
          </PermissionButton>
        </div>
      </Card>
      {acceptance.map((report) => (
        <Card
          key={String(report.acceptance_id)}
          title={`验收 ${String(report.status)}`}
          description={
            report.stale ? "旧要求报告，不适用于当前修订" : "当前要求报告"
          }
          action={<Badge>{String(report.status)}</Badge>}
        >
          <JsonView value={report.report ?? report.coverage_plan ?? report} />
          {report.status === "ERROR" && !report.stale ? (
            <PermissionButton
              role={role}
              variant="secondary"
              onClick={() =>
                void requestAcceptance(String(report.acceptance_id))
              }
            >
              显式重试检查
            </PermissionButton>
          ) : null}
          {report.status === "REVIEW_REQUIRED" && !report.stale ? (
            <div className="review-form">
              <Field label="评审资格">
                <input
                  value={review.qualification}
                  onChange={(event) =>
                    setReview({ ...review, qualification: event.target.value })
                  }
                />
              </Field>
              <Field label="准则与范围">
                <textarea
                  value={review.criteria}
                  onChange={(event) =>
                    setReview({ ...review, criteria: event.target.value })
                  }
                />
              </Field>
              <Field label="决定理由">
                <textarea
                  value={review.reason}
                  onChange={(event) =>
                    setReview({ ...review, reason: event.target.value })
                  }
                />
              </Field>
              <PermissionButton
                role={role}
                required="OWNER"
                disabled={
                  !review.qualification || !review.criteria || !review.reason
                }
                onClick={() => void submitReview(report)}
              >
                OWNER 提交 PASS 评审
              </PermissionButton>
            </div>
          ) : null}
        </Card>
      ))}
      {toast.node}
    </>
  );
}

function useAgentStream(
  token: string,
  projectId: string,
  task: Task | undefined,
) {
  const [snapshot, setSnapshot] = useState<
    Record<string, unknown> | undefined
  >();
  const cursor = useRef(-1);
  useEffect(() => {
    setSnapshot(undefined);
    cursor.current = -1;
    if (!task || task.execution_mode !== "AGENT") return;
    const controller = new AbortController();
    let active = true;
    const wait = () =>
      new Promise((resolve) => window.setTimeout(resolve, 1000));
    void (async () => {
      while (active) {
        try {
          const response = await fetch(
            `/api/v1/projects/${projectId}/tasks/${task.task_id}/agent/events?after=${cursor.current}`,
            {
              headers: {
                Authorization: `Bearer ${token}`,
                Accept: "text/event-stream",
              },
              signal: controller.signal,
            },
          );
          if (!response.ok) throw new Error(`SSE ${response.status}`);
          const wire = await response.text();
          const id = wire.split("\n").find((line) => line.startsWith("id: "));
          const data = wire
            .split("\n")
            .find((line) => line.startsWith("data: "));
          if (id)
            cursor.current = Math.max(cursor.current, Number(id.slice(4)));
          if (data) {
            const value = JSON.parse(data.slice(6)) as Record<string, unknown>;
            if (!value.heartbeat) {
              cursor.current = Math.max(
                cursor.current,
                Number(value.wake_revision ?? cursor.current),
              );
              setSnapshot(value);
            }
          }
        } catch (error) {
          if (
            !active ||
            (error instanceof DOMException && error.name === "AbortError")
          )
            return;
        }
        await wait();
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [projectId, task?.task_id, task?.execution_mode, token]);
  return snapshot;
}

export function TaskDetailPage() {
  const session = useSession();
  const { token } = session;
  const projectId = useProjectId();
  const { taskId = "" } = useParams();
  const summary = useSummary();
  const client = useQueryClient();
  const toast = useToast();
  const [tab, setTab] = useState("scope");
  const [confirm, setConfirm] = useState<"cancel" | null>(null);
  const [reason, setReason] = useState("");
  const [answer, setAnswer] = useState("{}");
  const task = useQuery({
    queryKey: ["task", projectId, taskId],
    queryFn: () =>
      request<Task>(token, `/projects/${projectId}/tasks/${taskId}`),
    refetchInterval: 5000,
  });
  const budget = useQuery({
    queryKey: ["task-budget", projectId, taskId],
    queryFn: () =>
      request<Record<string, unknown>>(
        token,
        `/projects/${projectId}/tasks/${taskId}/budget`,
      ),
    enabled: Boolean(task.data),
    refetchInterval: 5000,
  });
  const activities = useQuery({
    queryKey: ["task-activities", projectId, taskId],
    queryFn: () =>
      request<Page<Record<string, unknown>>>(
        token,
        `/projects/${projectId}/tasks/${taskId}/activities`,
      ),
    enabled: Boolean(task.data),
    refetchInterval: 5000,
  });
  const grants = useQuery({
    queryKey: ["grants", projectId],
    queryFn: () =>
      request<Page<Record<string, unknown>>>(
        token,
        `/projects/${projectId}/grants`,
      ),
  });
  const deliverables = useQuery({
    queryKey: ["deliverables", projectId, taskId],
    queryFn: () =>
      request<Page<Record<string, unknown>>>(
        token,
        `/projects/${projectId}/tasks/${taskId}/deliverables`,
      ),
    enabled: Boolean(task.data),
    refetchInterval: 5000,
  });
  const acceptance = useQuery({
    queryKey: ["acceptance", projectId, taskId],
    queryFn: () =>
      request<Page<Record<string, unknown>>>(
        token,
        `/projects/${projectId}/tasks/${taskId}/acceptance`,
      ),
    enabled: Boolean(task.data),
    refetchInterval: (query) =>
      (
        (query.state.data as Page<Record<string, unknown>> | undefined)
          ?.items ?? []
      ).some((item) => item.status === "QUEUED")
        ? 2000
        : 5000,
  });
  const agent = useQuery({
    queryKey: ["agent", projectId, taskId],
    queryFn: () =>
      request<Record<string, unknown>>(
        token,
        `/projects/${projectId}/tasks/${taskId}/agent`,
      ),
    enabled: task.data?.execution_mode === "AGENT",
    refetchInterval: 3000,
  });
  const agentEvent = useAgentStream(token, projectId, task.data);
  async function control(command: "pause" | "resume" | "cancel") {
    if (!task.data) return;
    try {
      await mutate(token, `/projects/${projectId}/tasks/${taskId}/${command}`, {
        expected_control_version: task.data.control_version,
      });
      await Promise.all([task.refetch(), activities.refetch()]);
      setConfirm(null);
      setReason("");
      toast.success(
        command === "pause"
          ? "任务已暂停。"
          : command === "resume"
            ? "任务已恢复。"
            : "任务取消已确认。",
      );
    } catch (error) {
      toast.failure(error);
    }
  }
  async function revokeGrant(grantId: string) {
    try {
      await mutate(
        token,
        `/projects/${projectId}/grants/${grantId}/revoke`,
        {},
      );
      await grants.refetch();
      toast.success("授权已撤销。");
    } catch (error) {
      toast.failure(error);
    }
  }
  async function answerQuestion(questionId: string) {
    if (!task.data) return;
    try {
      await mutate(
        token,
        `/projects/${projectId}/tasks/${taskId}/questions/${questionId}/answers`,
        {
          expected_spec_revision: task.data.spec_revision,
          answer: JSON.parse(answer),
        },
      );
      await agent.refetch();
      toast.success("结构化回答已提交。");
    } catch (error) {
      toast.failure(error);
    }
  }
  if (task.isLoading) return <PageLoading />;
  if (task.error || !task.data)
    return <PageError error={task.error} retry={() => void task.refetch()} />;
  const value = task.data;
  const role = resolveRole(summary.data, session.projects, projectId);
  const taskGrants = (grants.data?.items ?? []).filter(
    (item) => item.task_id === taskId,
  );
  return (
    <div className="page">
      <PageHeader
        title={value.spec.goal}
        description={`任务 ${taskId} · 范围修订 ${value.spec_revision}`}
        crumbs={[
          { label: "任务", href: `/projects/${projectId}/tasks` },
          { label: taskId },
        ]}
        action={
          <div className="actions">
            <Badge>{value.execution_mode}</Badge>
            <Badge>{value.lifecycle}</Badge>
            <Badge>{value.control}</Badge>
          </div>
        }
      />
      <div className="metric-grid four">
        <MetricCard label="任务状态" value={value.lifecycle} />
        <MetricCard label="控制状态" value={value.control} />
        <MetricCard
          label="预算"
          value={`${String(budget.data?.used ?? 0)} / ${String(budget.data?.execution_count ?? "—")}`}
          hint={`${String(budget.data?.in_flight ?? 0)} 在途`}
        />
        <MetricCard label="清理状态" value={value.cleanup_status} />
      </div>
      <div className="command-bar">
        <PermissionButton
          role={role}
          variant="secondary"
          disabled={value.control === "PAUSED" || value.lifecycle !== "OPEN"}
          onClick={() => void control("pause")}
        >
          暂停
        </PermissionButton>
        <PermissionButton
          role={role}
          variant="secondary"
          disabled={value.control !== "PAUSED" || value.lifecycle !== "OPEN"}
          onClick={() => void control("resume")}
        >
          恢复
        </PermissionButton>
        <PermissionButton
          role={role}
          variant="danger"
          disabled={value.lifecycle !== "OPEN"}
          onClick={() => setConfirm("cancel")}
        >
          <XCircle size={16} />
          取消任务
        </PermissionButton>
      </div>
      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "scope", label: "范围" },
          { id: "budget", label: "预算" },
          { id: "grants", label: "授权" },
          { id: "runs", label: "运行" },
          { id: "delivery", label: "交付" },
          { id: "acceptance", label: "验收" },
          ...(value.execution_mode === "AGENT"
            ? [{ id: "agent", label: "Agent" }]
            : []),
        ]}
      />
      {tab === "scope" ? (
        <Card title="冻结任务范围">
          <JsonView value={value.spec} />
        </Card>
      ) : null}
      {tab === "budget" ? (
        <Card title="执行预算">
          <JsonView value={budget.data ?? {}} />
        </Card>
      ) : null}
      {tab === "grants" ? (
        <div className="stack">
          <TaskOperations
            mode="grants"
            task={value}
            role={role}
            activities={activities.data?.items ?? []}
            deliverables={deliverables.data?.items ?? []}
            acceptance={acceptance.data?.items ?? []}
            grants={taskGrants}
            refresh={async () => {
              await grants.refetch();
            }}
          />
          <Card title="现有授权" description="过期或撤销授权不会被客户端掩盖。">
            {taskGrants.length ? (
              taskGrants.map((grant, index) => (
                <div
                  className="timeline-item"
                  key={String(grant.grant_id ?? index)}
                >
                  <div>
                    <Badge>{grant.revoked ? "REVOKED" : "ACTIVE"}</Badge>
                    <strong>{String(grant.grant_id)}</strong>
                    {!grant.revoked ? (
                      <PermissionButton
                        role={role}
                        required="OWNER"
                        variant="danger"
                        onClick={() => void revokeGrant(String(grant.grant_id))}
                      >
                        撤销
                      </PermissionButton>
                    ) : null}
                  </div>
                  <JsonView value={grant} />
                </div>
              ))
            ) : (
              <EmptyState title="当前任务没有授权" />
            )}
          </Card>
        </div>
      ) : null}
      {tab === "runs" ? (
        <div className="stack">
          <TaskOperations
            mode="runs"
            task={value}
            role={role}
            activities={activities.data?.items ?? []}
            deliverables={deliverables.data?.items ?? []}
            acceptance={acceptance.data?.items ?? []}
            grants={taskGrants}
            refresh={async () => {
              await Promise.all([activities.refetch(), budget.refetch()]);
            }}
          />
          <Card
            title="运行活动"
            action={
              <Button
                variant="secondary"
                onClick={() => void activities.refetch()}
              >
                <RefreshCw size={15} />
                刷新
              </Button>
            }
          >
            {activities.data?.items?.length ? (
              <DataTable
                rows={activities.data.items.map((item, index) => ({
                  ...item,
                  id: String(item.action_id ?? index),
                  target: targetLabel(item.request),
                  revision: item.spec_revision,
                }))}
                columns={[
                  { key: "target", label: "目标" },
                  {
                    key: "state",
                    label: "状态",
                    render: (row) => <Badge>{String(row.state)}</Badge>,
                  },
                  { key: "revision", label: "范围修订" },
                  { key: "execution_id", label: "执行 ID" },
                ]}
              />
            ) : (
              <EmptyState title="还没有执行活动" />
            )}
          </Card>
        </div>
      ) : null}
      {tab === "delivery" ? (
        <div className="stack">
          <TaskOperations
            mode="delivery"
            task={value}
            role={role}
            activities={activities.data?.items ?? []}
            deliverables={deliverables.data?.items ?? []}
            acceptance={acceptance.data?.items ?? []}
            grants={taskGrants}
            refresh={async () => {
              await deliverables.refetch();
            }}
          />
          <Card title="候选交付">
            <JsonView value={deliverables.data?.items ?? []} />
          </Card>
        </div>
      ) : null}
      {tab === "acceptance" ? (
        <div className="stack">
          <Banner>
            SAMPLE_VERIFIED 仅表示冻结样例通过，不等于科学 VERIFIED。
          </Banner>
          <TaskOperations
            mode="acceptance"
            task={value}
            role={role}
            activities={activities.data?.items ?? []}
            deliverables={deliverables.data?.items ?? []}
            acceptance={acceptance.data?.items ?? []}
            grants={taskGrants}
            refresh={async () => {
              await Promise.all([acceptance.refetch(), task.refetch()]);
            }}
          />
        </div>
      ) : null}
      {tab === "agent" ? (
        <AgentPanel
          value={agentEvent ?? agent.data}
          answer={answer}
          setAnswer={setAnswer}
          onAnswer={answerQuestion}
        />
      ) : null}
      <ConfirmDialog
        open={confirm === "cancel"}
        title="取消任务"
        description="取消将停止后续执行并触发清理流程。已经发布的不可变事实不会被删除。"
        reason={reason}
        onReason={setReason}
        onCancel={() => setConfirm(null)}
        onConfirm={() => void control("cancel")}
      />
      {toast.node}
    </div>
  );
}

function AgentPanel({
  value,
  answer,
  setAnswer,
  onAnswer,
}: {
  value?: Record<string, unknown>;
  answer: string;
  setAnswer: (value: string) => void;
  onAnswer: (id: string) => void;
}) {
  if (!value)
    return (
      <Card>
        <Skeleton lines={6} />
      </Card>
    );
  const questions =
    (value.questions as Array<Record<string, unknown>> | undefined) ?? [];
  const open = questions.filter((item) => item.status === "OPEN");
  return (
    <div className="detail-grid">
      <Card title="Agent 状态">
        <dl className="definition-list">
          <div>
            <dt>状态</dt>
            <dd>
              <Badge>{String(value.status)}</Badge>
            </dd>
          </div>
          <div>
            <dt>计划修订</dt>
            <dd>{String(value.plan_revision ?? "—")}</dd>
          </div>
          <div>
            <dt>Runtime epoch</dt>
            <dd>{String(value.epoch ?? "—")}</dd>
          </div>
          <div>
            <dt>事件游标</dt>
            <dd>{String(value.wake_revision ?? "—")}</dd>
          </div>
        </dl>
        <JsonView
          value={{
            model_usage: value.model_usage,
            wait_dependencies: value.wait_dependencies,
            runtime_recoveries: value.runtime_recoveries,
          }}
        />
      </Card>
      <Card title="待回答问题">
        {open.length ? (
          open.map((question) => (
            <div className="question-card" key={String(question.question_id)}>
              <strong>
                {String(
                  (question.question as Record<string, unknown> | undefined)
                    ?.question ?? question.gap_id,
                )}
              </strong>
              <Field label="结构化回答（JSON）">
                <textarea
                  rows={5}
                  value={answer}
                  onChange={(event) => setAnswer(event.target.value)}
                />
              </Field>
              <Button onClick={() => onAnswer(String(question.question_id))}>
                提交结构化回答
              </Button>
            </div>
          ))
        ) : (
          <EmptyState title="没有待回答问题" />
        )}
      </Card>
      <Card title="近期决策">
        <JsonView value={value.recent_decisions ?? []} />
      </Card>
      <Card title="交付尝试">
        <JsonView value={value.delivery_attempts ?? []} />
      </Card>
    </div>
  );
}
