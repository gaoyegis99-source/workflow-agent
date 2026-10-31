import {
  Activity,
  Archive,
  BookOpenCheck,
  Boxes,
  ChevronDown,
  CircleHelp,
  Database,
  Home,
  Layers3,
  LogOut,
  Menu,
  Play,
  UserRound,
  Workflow,
} from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { Badge } from "../shared/ui";
import { useSession } from "../core/session";
import { resolveRole, useProjectId, useSummary } from "../core/support";

const navGroups = [
  { label: "项目", items: [{ to: "overview", label: "项目概览", icon: Home }, { to: "assets", label: "数据资产", icon: Database }, { to: "knowledge", label: "知识证据", icon: BookOpenCheck }] },
  { label: "开发与运行", items: [{ to: "tasks", label: "任务", icon: Activity }, { to: "runs", label: "运行", icon: Play }, { to: "workflows", label: "工作流", icon: Workflow }] },
  { label: "治理", items: [{ to: "releases", label: "发布版本", icon: Archive }, { to: "operators", label: "算子目录", icon: Boxes }] },
];

export function AppShell() {
  const session = useSession();
  const projectId = useProjectId();
  const summary = useSummary();
  const navigate = useNavigate();
  const sessionProject = session.projects.find((item) => item.project_id === projectId);
  const project = summary.data?.project ?? sessionProject;
  const role = resolveRole(summary.data, session.projects, projectId);
  return <div className="app-frame">
    <aside className="sidebar">
      <div className="sidebar-brand"><div className="brand-symbol small"><Layers3 /></div><div><strong>Groundwater</strong><span>DATA PLATFORM V7</span></div></div>
      <button className="project-switcher" onClick={() => navigate("/")}><span><small>当前项目</small><strong>{project?.name ?? projectId}</strong></span><ChevronDown size={16} /></button>
      <nav>{navGroups.map((group) => <div className="nav-group" key={group.label}><p>{group.label}</p>{group.items.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to}><Icon size={18} /><span>{label}</span></NavLink>)}</div>)}</nav>
      <div className="sidebar-footer"><div className="role-card"><UserRound size={16} /><span><small>项目角色</small><strong>{role}</strong></span></div><button onClick={session.clear} title="清除内存令牌"><LogOut size={17} />断开连接</button></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><div><button className="icon-button quiet" aria-label="菜单"><Menu size={18} /></button><span className="environment-dot" />V7 Deployment</div><div className="topbar-actions"><button className="icon-button quiet" title="帮助"><CircleHelp size={18} /></button><span className="release-chip" title={summary.data?.release_digest ?? "未报告部署修订"}>部署 {summary.data?.release_digest?.slice(7, 15) ?? "unknown"}</span><Badge>{role}</Badge></div></header>
      <div className="page-scroll"><Outlet context={{ role, summary: summary.data }} /></div>
    </div>
  </div>;
}
