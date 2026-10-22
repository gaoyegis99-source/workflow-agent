import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { mutate, request } from "../../shared/api";
import {
  Badge,
  Banner,
  Button,
  Card,
  DataTable,
  EmptyState,
  JsonView,
  MetricCard,
  Select,
  Skeleton,
} from "../../shared/ui";
import { useSession } from "../../core/session";
import {
  PageError,
  PageHeader,
  PageLoading,
  PermissionButton,
  resolveRole,
  useProjectId,
  useSummary,
  useToast,
} from "../../core/support";
import type { ExecutionSummary, Page } from "../../shared/types";

export function RunsPage() {
  const { token } = useSession(); const projectId = useProjectId(); const navigate = useNavigate(); const [params, setParams] = useSearchParams();
  const state = params.get("state") ?? "ALL"; const taskId = params.get("task_id") ?? "";
  const runs = useQuery({ queryKey: ["executions", projectId, state, taskId], queryFn: () => request<Page<ExecutionSummary>>(token, `/projects/${projectId}/executions?limit=100${state !== "ALL" ? `&state=${encodeURIComponent(state)}` : ""}${taskId ? `&task_id=${encodeURIComponent(taskId)}` : ""}`), refetchInterval: 5000 });
  const rows = (runs.data?.items ?? []).map((run) => ({ ...run, id: run.action_id }));
  return <div className="page"><PageHeader title="运行" description="按任务与状态查看执行事实；未采集的运行日志不会在此伪造展示。" action={<Button variant="secondary" onClick={() => void runs.refetch()}><RefreshCw size={16} />刷新</Button>} />
    <Card className="table-card"><div className="table-toolbar"><Select value={state} onChange={(event) => { const next = new URLSearchParams(params); if (event.target.value === "ALL") next.delete("state"); else next.set("state", event.target.value); setParams(next); }}><option value="ALL">全部状态</option><option>QUEUED</option><option>RUNNING</option><option>SUCCEEDED</option><option>FAILED</option><option>CANCELLED</option><option>REJECTED</option></Select><input value={taskId} onChange={(event) => { const next = new URLSearchParams(params); if (event.target.value) next.set("task_id", event.target.value); else next.delete("task_id"); setParams(next); }} placeholder="按任务 ID 筛选" /></div>{runs.isLoading ? <Skeleton lines={8} /> : runs.error ? <PageError embedded error={runs.error} retry={() => void runs.refetch()} /> : <DataTable rows={rows as Array<Record<string, unknown>>} onRowClick={(row) => row.execution_id ? navigate(String(row.execution_id)) : undefined} columns={[{ key: "action_id", label: "操作 / 执行", render: (row) => <div><strong>{String(row.execution_id ?? "等待执行关联")}</strong><small className="block">Action {String(row.action_id)}</small></div> }, { key: "target_id", label: "目标", render: (row) => <div><strong>{String(row.target_id)}</strong><small className="block">{String(row.target_kind)}</small></div> }, { key: "task_id", label: "任务" }, { key: "state", label: "状态", render: (row) => <Badge>{String(row.state)}</Badge> }, { key: "spec_revision", label: "范围修订" }, { key: "decision_source", label: "决策来源" }, { key: "stop", label: "停止", render: (row) => row.cancel_requested || row.stopped ? <Badge tone="warning">{row.cancel_requested ? "请求取消" : "已停止"}</Badge> : "—" }]} />}</Card></div>;
}

export function RunDetailPage() {
  const session = useSession(); const { token } = session; const projectId = useProjectId(); const { executionId = "" } = useParams(); const toast = useToast(); const summary = useSummary();
  const detail = useQuery({ queryKey: ["execution", projectId, executionId], queryFn: () => request<Record<string, unknown>>(token, `/projects/${projectId}/executions/${executionId}`), refetchInterval: (query) => ["QUEUED", "RUNNING"].includes(String((query.state.data as Record<string, unknown> | undefined)?.state)) ? 3000 : false });
  const steps = useQuery({ queryKey: ["execution-steps", projectId, executionId], queryFn: () => request<Page<Record<string, unknown>>>(token, `/projects/${projectId}/executions/${executionId}/steps`), retry: false });
  if (detail.isLoading) return <PageLoading />; if (detail.error || !detail.data) return <PageError error={detail.error} retry={() => void detail.refetch()} />;
  const value = detail.data; const requestValue = value.request as Record<string, unknown> | undefined; const runtime = value.runtime as Record<string, unknown> | undefined;
  async function cancelExecution() { try { await mutate(token, `/projects/${projectId}/executions/${executionId}/cancel`, { expected_spec_revision: Number(requestValue?.expected_spec_revision) }); await detail.refetch(); toast.success("取消请求已提交。"); } catch (error) { toast.failure(error); } }
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED", "SEALED", "REJECTED"].includes(String(value.state));
  const role = resolveRole(summary.data, session.projects, projectId);
  return <div className="page"><PageHeader title={`运行 ${executionId}`} description="执行目标、输入、预算关联、运行时发布事实与工作流节点。" crumbs={[{ label: "运行", href: `/projects/${projectId}/runs` }, { label: executionId }]} action={<div className="actions"><Badge>{String(value.state ?? "UNKNOWN")}</Badge><PermissionButton role={role} variant="danger" disabled={terminal} onClick={() => void cancelExecution()}>取消运行</PermissionButton></div>} />
    <div className="metric-grid four"><MetricCard label="状态" value={String(value.state ?? "UNKNOWN")} /><MetricCard label="实际执行" value={value.executed ? "是" : "否"} /><MetricCard label="发布" value={String(runtime?.publication ?? "UNKNOWN")} /><MetricCard label="恢复状态" value={runtime?.uncertain ? "UNKNOWN" : "已确认"} /></div>
    <div className="detail-grid"><Card title="目标与任务"><JsonView value={{ execution_id: value.execution_id, task_id: requestValue?.task_id, expected_spec_revision: requestValue?.expected_spec_revision, target: requestValue?.target }} /></Card><Card title="预算与输入"><JsonView value={{ budget: value.budget, inputs: requestValue?.inputs, parameters: requestValue?.parameters }} /></Card><Card title="运行时事实"><JsonView value={runtime ?? {}} /></Card><Card title="工作流节点">{steps.data?.items?.length ? <DataTable rows={steps.data.items.map((item, index) => ({ ...item, id: index }))} columns={[{ key: "call_path", label: "调用路径", render: (row) => Array.isArray(row.call_path) ? row.call_path.join(" / ") : "—" }, { key: "state", label: "状态", render: (row) => <Badge>{String(row.state)}</Badge> }, { key: "step_key", label: "节点" }]} /> : <EmptyState title="没有工作流节点事实" description="单算子运行或执行服务未提供节点摘要。" />}</Card></div>
    <Banner>当前平台没有运行日志采集接口，因此本页只展示服务端已确认的结构化事实。</Banner>{toast.node}
  </div>;
}
