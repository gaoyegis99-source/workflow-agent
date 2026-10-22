import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { ApiError, request } from "../shared/api";
import {
  Banner,
  Breadcrumb,
  Button,
  EmptyState,
  roleAllows,
  Skeleton,
} from "../shared/ui";
import { useSession } from "./session";
import type { Project, Role, WorkspaceSummary } from "../shared/types";

export function useProjectId() {
  return useParams<{ projectId: string }>().projectId ?? "";
}

export function useSummary() {
  const { token } = useSession();
  const projectId = useProjectId();
  return useQuery({
    queryKey: ["workspace-summary", projectId],
    queryFn: () =>
      request<WorkspaceSummary>(token, `/projects/${projectId}/workspace-summary`),
    enabled: Boolean(token && projectId),
    staleTime: 10_000,
  });
}

export function resolveRole(
  summary: WorkspaceSummary | undefined,
  projects: Project[],
  projectId: string,
): Role {
  return (
    summary?.role ??
    projects.find((project) => project.project_id === projectId)?.role ??
    "VIEWER"
  );
}

export function errorText(error: unknown) {
  if (error instanceof ApiError) return `${error.message}（${error.code}）`;
  return error instanceof Error ? error.message : "请求失败";
}

export function useToast() {
  const [message, setMessage] = useState<{
    text: string;
    tone: "success" | "danger";
  } | null>(null);
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(null), 4500);
    return () => window.clearTimeout(timer);
  }, [message]);
  return {
    success: (text: string) => setMessage({ text, tone: "success" }),
    failure: (error: unknown) =>
      setMessage({ text: errorText(error), tone: "danger" }),
    node: message ? (
      <div className={`toast ${message.tone}`} role="status">
        {message.text}
      </div>
    ) : null,
  };
}

export function PermissionButton({
  role,
  required = "EDITOR",
  children,
  ...props
}: Parameters<typeof Button>[0] & {
  role: Role;
  required?: "EDITOR" | "OWNER";
}) {
  const allowed = roleAllows(role, required);
  return (
    <span
      className="permission-action"
      title={allowed ? undefined : `需要 ${required} 角色`}
    >
      <Button {...props} disabled={!allowed || props.disabled}>
        {children}
      </Button>
    </span>
  );
}

export function PageHeader({
  title,
  description,
  action,
  crumbs,
}: {
  title: string;
  description: string;
  action?: ReactNode;
  crumbs?: Array<{ label: string; href?: string }>;
}) {
  return (
    <>
      <Breadcrumb items={crumbs ?? [{ label: title }]} />
      <header className="page-header">
        <div>
          <h1>{title}</h1>
          <p>{description}</p>
        </div>
        {action}
      </header>
    </>
  );
}

export function PageLoading() {
  return (
    <div className="page">
      <Skeleton lines={10} />
    </div>
  );
}

export function PageError({
  error,
  retry,
  embedded,
}: {
  error: unknown;
  retry: () => void;
  embedded?: boolean;
}) {
  return (
    <div className={embedded ? "embedded-error" : "page"}>
      <Banner
        tone="danger"
        title="无法加载数据"
        action={
          <Button variant="secondary" onClick={retry}>
            重试
          </Button>
        }
      >
        {errorText(error)}
      </Banner>
    </div>
  );
}

export function NotFound() {
  return (
    <main className="not-found">
      <EmptyState
        title="页面不存在"
        description="该路径不属于 V7 工作区。"
        action={<a href="/">返回连接页</a>}
      />
    </main>
  );
}

export function formatDate(value: string) {
  try {
    return new Date(value).toLocaleString("zh-CN");
  } catch {
    return value;
  }
}

export function targetLabel(value: unknown) {
  const requestValue = value as Record<string, unknown> | undefined;
  const target = requestValue?.target as Record<string, unknown> | undefined;
  return String(
    target?.operator_id ??
      (target?.package_ref as Record<string, unknown> | undefined)?.package_id ??
      target?.kind ??
      "—",
  );
}
