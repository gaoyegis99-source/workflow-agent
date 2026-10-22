import { Plus, RefreshCw, Upload, Workflow } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Badge, Banner, Button, Card, MetricCard } from "../../shared/ui";
import { PageError, PageHeader, PageLoading, useProjectId, useSummary } from "../../core/support";
import type { CountGroup } from "../../shared/types";

export function OverviewPage() {
  const summary = useSummary();
  const projectId = useProjectId();
  const navigate = useNavigate();
  if (summary.isLoading) return <PageLoading />;
  if (summary.error || !summary.data) return <PageError error={summary.error} retry={() => void summary.refetch()} />;
  const value = summary.data;
  const taskIssues = (value.tasks.PAUSED ?? 0) + (value.tasks.CANCELLED ?? 0);
  const runIssues = (value.executions.FAILED ?? 0) + (value.executions.REJECTED ?? 0);
  const taskTotal = Object.values(value.tasks).reduce((sum, count) => sum + count, 0);
  const executionTotal = Object.values(value.executions).reduce((sum, count) => sum + count, 0);
  return <div className="page overview-page">
    <PageHeader title={value.project.name} description={`项目 ${projectId} 的数据、执行和治理全景。`} action={<Button variant="secondary" onClick={() => void summary.refetch()}><RefreshCw size={16} />刷新</Button>} />
    <Banner tone={runIssues ? "warning" : "success"} title={runIssues ? "发现需要关注的运行" : "工作区状态正常"}>
      {runIssues ? `${runIssues} 个失败或拒绝的执行需要检查。` : `统计截至 ${new Date(value.as_of).toLocaleString("zh-CN")}，没有检测到失败执行。`}
    </Banner>
    <div className="metric-grid">
      <MetricCard label="数据资产" value={value.resources.assets} hint="已发布版本" />
      <MetricCard label="任务" value={taskTotal} hint={taskIssues ? `${taskIssues} 项需关注` : "无阻塞任务"} tone={taskIssues ? "warning" : "default"} />
      <MetricCard label="运行" value={executionTotal} hint={`${value.executions.RUNNING ?? 0} 正在运行`} />
      <MetricCard label="工作流草稿" value={value.resources.workflow_definitions} hint={`${value.resources.workflow_releases} 个正式版本`} />
      <MetricCard label="算子版本" value={value.resources.operator_versions} hint={`${value.resources.operator_materials} 份接入材料`} />
    </div>
    <div className="overview-grid">
      <Card title="任务状态" description="完整状态计数，不根据客户端权限推断。"><StateBars values={value.tasks} /></Card>
      <Card title="运行状态" description="包括记录、派发、执行、协调与终止事实。"><StateBars values={value.executions} /></Card>
      <Card title="快捷入口" className="quick-card"><button onClick={() => navigate(`/projects/${projectId}/assets`)}><Upload /><span><strong>导入数据</strong><small>上传并检查科研数据</small></span></button><button onClick={() => navigate(`/projects/${projectId}/tasks`)}><Plus /><span><strong>创建任务</strong><small>冻结范围与预算</small></span></button><button onClick={() => navigate(`/projects/${projectId}/workflows`)}><Workflow /><span><strong>编排工作流</strong><small>节点画布与权威校验</small></span></button></Card>
      <Card title="发布身份" description="当前实例与 Release Lock 的一致性标识。"><dl className="definition-list"><div><dt>Phase</dt><dd><Badge>{value.phase}</Badge></dd></div><div><dt>Release digest</dt><dd><code>{value.release_digest ?? "未报告"}</code></dd></div><div><dt>项目角色</dt><dd><Badge>{value.role}</Badge></dd></div></dl></Card>
    </div>
  </div>;
}

function StateBars({ values }: { values: CountGroup }) {
  const entries = Object.entries(values).filter(([key]) => key !== "total");
  const max = Math.max(1, ...entries.map(([, value]) => value));
  return <div className="state-bars">{entries.map(([key, value]) => <div key={key}><span><Badge>{key}</Badge><strong>{value}</strong></span><i><b style={{ width: `${(value / max) * 100}%` }} /></i></div>)}</div>;
}
