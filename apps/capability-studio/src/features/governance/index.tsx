import { useQuery } from "@tanstack/react-query";
import { Boxes } from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { type AuthoringDescriptor, type OperatorProposal, request } from "../../shared/api";
import {
  Badge,
  Card,
  DataTable,
  Drawer,
  JsonView,
  Skeleton,
  Tabs,
} from "../../shared/ui";
import { useSession } from "../../core/session";
import {
  PageError,
  PageHeader,
  resolveRole,
  useProjectId,
  useSummary,
} from "../../core/support";
import type { Page } from "../../shared/types";

const LazyWorkflowStudio = lazy(() =>
  import("./studio/WorkflowStudio").then((module) => ({ default: module.WorkflowStudio })),
);

export function WorkflowPage({ mode }: { mode: "editor" | "releases" }) {
  const session = useSession(); const { token } = session; const projectId = useProjectId(); const summary = useSummary();
  const role = resolveRole(summary.data, session.projects, projectId);
  return <div className={`page workflow-page ${mode}`}><PageHeader title={mode === "editor" ? "工作流" : "发布版本"} description={mode === "editor" ? "通过节点画布编排受限工作流；预览不替代服务端正式校验。" : "按稳定键治理候选、试运行、正式版本与生命周期。"} />
    <Suspense fallback={<Card><Skeleton lines={8} /></Card>}><LazyWorkflowStudio token={token} project={projectId} mode={mode} role={role} /></Suspense>
  </div>;
}

export function OperatorsPage() {
  const session = useSession(); const { token } = session; const projectId = useProjectId(); const summary = useSummary(); const [selected, setSelected] = useState<string | null>(null); const [tab, setTab] = useState("catalog");
  const descriptor = useQuery({ queryKey: ["operators", projectId], queryFn: () => request<AuthoringDescriptor>(token, `/projects/${projectId}/authoring/operator-descriptors`) });
  const proposals = useQuery({ queryKey: ["operator-proposals", projectId], queryFn: () => request<Page<OperatorProposal>>(token, `/projects/${projectId}/operator-proposals`) });
  const operator = descriptor.data?.operators.find((item) => `${item.operator_id}@${item.version_id}` === selected);
  const role = resolveRole(summary.data, session.projects, projectId);
  return <div className="page"><PageHeader title="算子目录" description="检查端口、参数 Schema、受信实现与版本，并跟踪接入材料评审和部署匹配。" action={<Badge>{role}</Badge>} />
    <Tabs active={tab} onChange={setTab} tabs={[{ id: "catalog", label: `已部署算子 ${descriptor.data?.operators.length ?? 0}` }, { id: "proposals", label: `接入材料 ${proposals.data?.items.length ?? 0}` }]} />
    {tab === "catalog" ? <Card className="table-card">{descriptor.isLoading ? <Skeleton lines={8} /> : descriptor.error ? <PageError embedded error={descriptor.error} retry={() => void descriptor.refetch()} /> : <DataTable rows={(descriptor.data?.operators ?? []).map((item) => ({ ...item, id: `${item.operator_id}@${item.version_id}`, identity: `${item.operator_id}@${item.version_id}`, implementation_count: item.implementations.length })) as Array<Record<string, unknown>>} onRowClick={(row) => setSelected(String(row.identity))} columns={[{ key: "identity", label: "算子版本", render: (row) => <div className="primary-cell"><Boxes size={17} /><strong>{String(row.identity)}</strong></div> }, { key: "ownership", label: "所有权" }, { key: "trust_level", label: "信任级别", render: (row) => <Badge>{String(row.trust_level)}</Badge> }, { key: "lifecycle", label: "生命周期", render: (row) => <Badge>{String(row.lifecycle)}</Badge> }, { key: "implementation_count", label: "实现" }, { key: "server_validation_required", label: "服务端校验", render: (row) => row.server_validation_required ? "需要" : "否" }]} />}</Card> : <Suspense fallback={<Card><Skeleton lines={7} /></Card>}><LazyWorkflowStudio token={token} project={projectId} mode="operators" role={role} /></Suspense>}
    <Drawer open={Boolean(operator)} title={selected ?? "算子详情"} onClose={() => setSelected(null)}>{operator ? <><dl className="definition-list"><div><dt>所有权</dt><dd>{operator.ownership}</dd></div><div><dt>信任级别</dt><dd><Badge>{operator.trust_level}</Badge></dd></div><div><dt>生命周期</dt><dd><Badge>{operator.lifecycle}</Badge></dd></div><div><dt>描述符摘要</dt><dd><code>{operator.descriptor_digest}</code></dd></div></dl><h3>输入端口</h3><JsonView value={operator.input_ports} /><h3>输出端口</h3><JsonView value={operator.output_ports} /><h3>参数 Schema</h3><JsonView value={operator.parameter_schema} /><h3>实现</h3><JsonView value={operator.implementations} /></> : null}</Drawer>
  </div>;
}
