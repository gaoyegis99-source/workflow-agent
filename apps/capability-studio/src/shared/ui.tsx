import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";
import { X } from "lucide-react";
import { Link } from "react-router-dom";

export function Button({ variant = "primary", busy, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean;
}) {
  return <button className={`ui-button ${variant}`} {...props} disabled={busy || props.disabled}>
    {busy ? <span className="spinner" aria-hidden /> : null}{children}
  </button>;
}

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: ReactNode }) {
  return <label className="ui-field"><span>{label}</span>{children}{error ? <small className="field-error">{error}</small> : hint ? <small>{hint}</small> : null}</label>;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className="ui-select" {...props} />;
}

const success = new Set(["ACTIVE", "PASSED", "SUCCEEDED", "READY", "OPEN", "APPROVED", "COMPLETED"]);
const info = new Set(["QUEUED", "RUNNING", "VALIDATING", "DISPATCHING", "RECORDED", "RECONCILING"]);
const warning = new Set(["DISABLED", "STALE", "PAUSED", "REVIEW_REQUIRED", "CHANGES_REQUESTED", "DRAFT"]);
const danger = new Set(["FAILED", "REJECTED", "REVOKED", "ERROR"]);
const neutral = new Set(["CANCELLED", "SEALED", "UNKNOWN", "CLEAR"]);

export function toneForState(state: string) {
  const value = state.toUpperCase();
  if (success.has(value)) return "success";
  if (info.has(value)) return "info";
  if (warning.has(value)) return "warning";
  if (danger.has(value)) return "danger";
  if (neutral.has(value)) return "neutral";
  return "neutral";
}

export function roleAllows(role: "VIEWER" | "EDITOR" | "OWNER", required: "EDITOR" | "OWNER") {
  return required === "OWNER" ? role === "OWNER" : role === "EDITOR" || role === "OWNER";
}

export function Badge({ children, tone }: { children: ReactNode; tone?: string }) {
  const value = String(children ?? "—");
  return <span className={`ui-badge ${tone ?? toneForState(value)}`}>{children}</span>;
}

export function Card({ title, description, action, children, className = "", ...props }: HTMLAttributes<HTMLElement> & {
  title?: ReactNode; description?: ReactNode; action?: ReactNode;
}) {
  return <section className={`ui-card ${className}`} {...props}>
    {title || description || action ? <header className="card-header"><div>{title ? <h2>{title}</h2> : null}{description ? <p>{description}</p> : null}</div>{action}</header> : null}
    {children}
  </section>;
}

export function MetricCard({ label, value, hint, tone = "default" }: { label: string; value: ReactNode; hint?: string; tone?: string }) {
  return <div className={`metric-card ${tone}`}><span>{label}</span><strong>{value}</strong>{hint ? <small>{hint}</small> : null}</div>;
}

export function DataTable({ columns, rows, empty = "暂无数据", onRowClick }: {
  columns: Array<{ key: string; label: string; render?: (row: Record<string, unknown>) => ReactNode; width?: string }>;
  rows: Array<Record<string, unknown>>;
  empty?: string;
  onRowClick?: (row: Record<string, unknown>) => void;
}) {
  return <div className="table-wrap"><table className="ui-table"><thead><tr>{columns.map((column) => <th key={column.key} style={{ width: column.width }}>{column.label}</th>)}</tr></thead>
    <tbody>{rows.length ? rows.map((row, index) => <tr key={String(row.id ?? row.task_id ?? row.action_id ?? row.asset_id ?? index)} onClick={() => onRowClick?.(row)} className={onRowClick ? "clickable" : ""}>
      {columns.map((column) => <td key={column.key}>{column.render ? column.render(row) : String(row[column.key] ?? "—")}</td>)}</tr>) : <tr><td colSpan={columns.length}><EmptyState title={empty} compact /></td></tr>}</tbody></table></div>;
}

export function Tabs({ tabs, active, onChange }: { tabs: Array<{ id: string; label: string }>; active: string; onChange: (id: string) => void }) {
  return <div className="ui-tabs" role="tablist">{tabs.map((tab) => <button key={tab.id} role="tab" aria-selected={active === tab.id} onClick={() => onChange(tab.id)}>{tab.label}</button>)}</div>;
}

export function Drawer({ open, title, onClose, children, footer }: { open: boolean; title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  if (!open) return null;
  return <div className="overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
    <aside className="ui-drawer" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button></header><div className="drawer-body">{children}</div>{footer ? <footer>{footer}</footer> : null}</aside>
  </div>;
}

export function Modal({ open, title, onClose, children, footer }: { open: boolean; title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  if (!open) return null;
  return <div className="overlay centered" role="presentation"><section className="ui-modal" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button></header><div className="modal-body">{children}</div>{footer ? <footer>{footer}</footer> : null}</section></div>;
}

export function ConfirmDialog({ open, title, description, reason, onReason, onCancel, onConfirm, busy }: {
  open: boolean; title: string; description: string; reason: string; onReason: (value: string) => void; onCancel: () => void; onConfirm: () => void; busy?: boolean;
}) {
  return <Modal open={open} title={title} onClose={onCancel} footer={<><Button variant="secondary" onClick={onCancel}>取消</Button><Button variant="danger" disabled={!reason.trim()} busy={busy} onClick={onConfirm}>确认操作</Button></>}>
    <p>{description}</p><Field label="原因" hint="原因将写入不可变审计记录。"><textarea value={reason} onChange={(event) => onReason(event.target.value)} rows={4} /></Field>
  </Modal>;
}

export function Banner({ tone = "info", title, children, action }: { tone?: string; title?: string; children: ReactNode; action?: ReactNode }) {
  return <div className={`ui-banner ${tone}`} role={tone === "danger" ? "alert" : "status"}><div>{title ? <strong>{title}</strong> : null}<span>{children}</span></div>{action}</div>;
}

export function EmptyState({ title, description, action, compact }: { title: string; description?: string; action?: ReactNode; compact?: boolean }) {
  return <div className={`empty-state ${compact ? "compact" : ""}`}><strong>{title}</strong>{description ? <p>{description}</p> : null}{action}</div>;
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return <div className="skeleton" aria-label="正在加载">{Array.from({ length: lines }, (_, index) => <span key={index} />)}</div>;
}

export function Breadcrumb({ items }: { items: Array<{ label: string; href?: string }> }) {
  return <nav className="breadcrumb" aria-label="面包屑">{items.map((item, index) => <span key={`${item.label}:${index}`}>{index ? <i>/</i> : null}{item.href ? <Link to={item.href}>{item.label}</Link> : item.label}</span>)}</nav>;
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>;
}
