import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import {
  ConnectionPage,
  ConnectionRequired,
  DesktopBoundary,
} from "./connection";
import { NotFound, PageLoading } from "../core/support";
import { SessionProvider, useSession } from "../core/session";
import { AppShell } from "./shell";

const OverviewPage = lazy(() =>
  import("../features/overview").then((module) => ({
    default: module.OverviewPage,
  })),
);
const AssetsPage = lazy(() =>
  import("../features/assets").then((module) => ({
    default: module.AssetsPage,
  })),
);
const AssetDetailPage = lazy(() =>
  import("../features/assets").then((module) => ({
    default: module.AssetDetailPage,
  })),
);
const TasksPage = lazy(() =>
  import("../features/tasks").then((module) => ({
    default: module.TasksPage,
  })),
);
const TaskDetailPage = lazy(() =>
  import("../features/tasks").then((module) => ({
    default: module.TaskDetailPage,
  })),
);
const RunsPage = lazy(() =>
  import("../features/runs").then((module) => ({
    default: module.RunsPage,
  })),
);
const RunDetailPage = lazy(() =>
  import("../features/runs").then((module) => ({
    default: module.RunDetailPage,
  })),
);
const WorkflowPage = lazy(() =>
  import("../features/governance").then((module) => ({
    default: module.WorkflowPage,
  })),
);
const OperatorsPage = lazy(() =>
  import("../features/governance").then((module) => ({
    default: module.OperatorsPage,
  })),
);
const KnowledgePage = lazy(() =>
  import("../features/knowledge").then((module) => ({
    default: module.KnowledgePage,
  })),
);

function ProjectRoute() {
  const { token } = useSession();
  return token ? <AppShell /> : <ConnectionRequired />;
}

export function App() {
  return (
    <SessionProvider>
      <DesktopBoundary>
        <Suspense fallback={<PageLoading />}>
          <Routes>
            <Route path="/" element={<ConnectionPage />} />
            <Route path="/projects/:projectId" element={<ProjectRoute />}>
              <Route index element={<Navigate to="overview" replace />} />
              <Route path="overview" element={<OverviewPage />} />
              <Route path="assets" element={<AssetsPage />} />
              <Route path="assets/:assetId" element={<AssetDetailPage />} />
              <Route path="knowledge" element={<KnowledgePage />} />
              <Route path="tasks" element={<TasksPage />} />
              <Route path="tasks/:taskId" element={<TaskDetailPage />} />
              <Route path="runs" element={<RunsPage />} />
              <Route path="runs/:executionId" element={<RunDetailPage />} />
              <Route path="workflows" element={<WorkflowPage mode="editor" />} />
              <Route path="releases" element={<WorkflowPage mode="releases" />} />
              <Route path="operators" element={<OperatorsPage />} />
            </Route>
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Suspense>
      </DesktopBoundary>
    </SessionProvider>
  );
}
