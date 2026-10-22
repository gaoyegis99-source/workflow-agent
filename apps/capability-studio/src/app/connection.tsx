import { Database, GitBranch, Layers3, Plus, ShieldCheck } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { mutate, request } from "../shared/api";
import { Banner, Button, EmptyState, Field, Select } from "../shared/ui";
import { errorText } from "../core/support";
import { useSession } from "../core/session";
import type { Page, Project } from "../shared/types";

export function DesktopBoundary({ children }: { children: ReactNode }) {
  return <><div className="desktop-only">{children}</div><div className="desktop-warning"><div className="brand-symbol"><Layers3 /></div><h1>请使用桌面浏览器</h1><p>V7 工作区需要至少 1100px 的可用宽度，以保证数据表格、治理信息和工作流画布可以安全操作。</p></div></>;
}

export function ConnectionRequired() {
  const location = useLocation();
  return <ConnectionPage returnTo={location.pathname + location.search} />;
}

export function ConnectionPage({ returnTo }: { returnTo?: string }) {
  const session = useSession();
  const navigate = useNavigate();
  const [tokenInput, setTokenInput] = useState("");
  const [name, setName] = useState("");
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function connect(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const value = tokenInput.trim();
      const page = await request<Page<Project>>(value, "/projects");
      session.setSession(value, page.items);
      setTokenInput("");
      setSelected(page.items[0]?.project_id ?? "");
      if (returnTo) navigate(returnTo, { replace: true });
    } catch (nextError) { setError(errorText(nextError)); }
    finally { setBusy(false); }
  }

  async function createProject() {
    setBusy(true); setError("");
    try {
      const created = await mutate<Project>(session.token, "/projects", { name });
      const next = [...session.projects, created];
      session.setProjects(next); setSelected(created.project_id); setName("");
    } catch (nextError) { setError(errorText(nextError)); }
    finally { setBusy(false); }
  }

  return <main className="connection-page">
    <section className="connection-brand">
      <div className="brand-lockup"><div className="brand-symbol"><Layers3 /></div><div><span>GROUNDWATER V7</span><strong>科研数据与工作流平台</strong></div></div>
      <div className="connection-copy"><p className="eyebrow">Project workspace</p><h1>让数据、任务与科研流程<br />在同一个可信工作区运行。</h1><p>从资产语义到执行验收，从工作流编排到不可变发布，所有关键事实均由服务端授权并留下可追溯记录。</p></div>
      <div className="trust-row"><span><ShieldCheck size={18} />逐次鉴权</span><span><Database size={18} />数据可追溯</span><span><GitBranch size={18} />版本治理</span></div>
    </section>
    <section className="connection-form-wrap">
      <div className="connection-card">
        <header><p className="eyebrow">Secure access</p><h2>{session.token ? "选择项目" : "连接工作区"}</h2><p>{session.token ? "选择一个有权访问的项目继续。" : "令牌只保存在当前页面内存中，刷新后需要重新输入。"}</p></header>
        {!session.token ? <form onSubmit={connect}>
          <Field label="临时 Bearer Token" hint="不会写入 URL、Cookie 或浏览器存储。"><input autoFocus type="password" autoComplete="off" value={tokenInput} onChange={(event) => setTokenInput(event.target.value)} placeholder="粘贴临时访问令牌" /></Field>
          {error ? <Banner tone="danger">{error}</Banner> : null}
          <Button type="submit" busy={busy} disabled={!tokenInput.trim()}>连接并加载项目</Button>
        </form> : <div className="project-picker">
          {session.projects.length ? <Field label="项目"><Select value={selected} onChange={(event) => setSelected(event.target.value)}>{session.projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name} · {project.project_id}</option>)}</Select></Field> : <EmptyState title="还没有项目" description="创建一个项目开始工作。" />}
          <Button disabled={!selected} onClick={() => navigate(`/projects/${selected}/overview`)}>进入工作区</Button>
          <div className="divider"><span>或创建项目</span></div>
          <Field label="项目名称"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：华北地下水调查" /></Field>
          <Button variant="secondary" busy={busy} disabled={!name.trim()} onClick={createProject}><Plus size={16} />创建项目</Button>
          {error ? <Banner tone="danger">{error}</Banner> : null}
          <button className="text-button" onClick={session.clear}>使用其他令牌</button>
        </div>}
      </div>
      <p className="connection-footnote">P8.2 · Release Lock · 仅限授权科研环境</p>
    </section>
  </main>;
}
