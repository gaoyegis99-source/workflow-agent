import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, RefreshCw, Search, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  downloadRepresentation,
  mutate,
  request,
  uploadBytes,
} from "../../shared/api";
import { requestAllPages, useCursorPages } from "../../shared/pagination";
import {
  Badge,
  Banner,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  JsonView,
  Modal,
  Select,
  Skeleton,
  Tabs,
} from "../../shared/ui";
import { useSession } from "../../core/session";
import {
  errorText,
  PageError,
  PageHeader,
  PageLoading,
  PermissionButton,
  resolveRole,
  useProjectId,
  useSummary,
  useToast,
} from "../../core/support";
import type {
  Asset,
  Representation,
  Role,
  UploadView,
} from "../../shared/types";

export function AssetsPage() {
  const session = useSession();
  const { token } = session;
  const projectId = useProjectId();
  const summary = useSummary();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("ALL");
  const [uploadOpen, setUploadOpen] = useState(false);
  const role = resolveRole(summary.data, session.projects, projectId);
  const assets = useCursorPages<Asset>(
    token,
    `/projects/${projectId}/assets`,
    ["assets", projectId],
    { refetchInterval: 3000 },
  );
  const rows = assets.items
    .filter(
      (asset) =>
        (kind === "ALL" || asset.manifest.kind === kind) &&
        asset.ref.asset_id.toLowerCase().includes(search.toLowerCase()),
    )
    .map((asset) => ({
      ...asset,
      id: asset.ref.asset_id,
      asset_id: asset.ref.asset_id,
      version: asset.ref.version_id,
      kind: asset.manifest.kind,
      count: asset.manifest.row_count ?? spatialCount(asset),
      representations: asset.manifest.representations?.length ?? 0,
    }));
  return (
    <div className="page">
      <PageHeader
        title="数据资产"
        description="管理不可变数据版本、字段与空间信息、语义修订和受信表示形式。"
        action={
          <PermissionButton role={role} onClick={() => setUploadOpen(true)}>
            <Upload size={16} />
            上传数据
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
              placeholder="按资产 ID 筛选"
            />
          </div>
          <Select
            value={kind}
            onChange={(event) => setKind(event.target.value)}
          >
            <option value="ALL">全部类型</option>
            <option>OBSERVATIONS</option>
            <option>SPATIAL_VECTOR</option>
            <option>SPATIAL_RASTER</option>
            <option>OPERATOR_RESULT</option>
          </Select>
          <Button variant="ghost" onClick={() => void assets.refetch()}>
            <RefreshCw size={16} />
            刷新
          </Button>
        </div>
        {assets.isLoading ? (
          <Skeleton lines={7} />
        ) : assets.error ? (
          <PageError
            error={assets.error}
            retry={() => void assets.refetch()}
            embedded
          />
        ) : (
          <>
            <DataTable
              rows={rows as Array<Record<string, unknown>>}
              onRowClick={(row) =>
                navigate(
                  `${String(row.asset_id)}?version=${encodeURIComponent(String(row.version))}`,
                )
              }
              columns={[
                {
                  key: "asset_id",
                  label: "资产 ID",
                  render: (row) => (
                    <div className="primary-cell">
                      <Database size={17} />
                      <span>
                        <strong>{String(row.asset_id)}</strong>
                        <small>版本 {String(row.version)}</small>
                      </span>
                    </div>
                  ),
                },
                {
                  key: "kind",
                  label: "类型",
                  render: (row) => <Badge>{String(row.kind)}</Badge>,
                },
                { key: "count", label: "记录 / 空间项" },
                { key: "representations", label: "表示形式" },
                {
                  key: "semantics",
                  label: "语义",
                  render: (row) => (
                    <Badge
                      tone={
                        Array.isArray(row.semantic_revisions) &&
                        row.semantic_revisions.length
                          ? "success"
                          : "warning"
                      }
                    >
                      {Array.isArray(row.semantic_revisions) &&
                      row.semantic_revisions.length
                        ? `修订 ${row.semantic_revisions.length}`
                        : "未声明"}
                    </Badge>
                  ),
                },
              ]}
            />
            {assets.hasNextPage ? (
              <div className="table-footer">
                <Button
                  variant="secondary"
                  busy={assets.isFetchingNextPage}
                  onClick={() => void assets.fetchNextPage()}
                >
                  继续加载
                </Button>
              </div>
            ) : null}
          </>
        )}
      </Card>
      <UploadWizard
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        role={role}
        onDone={() => {
          setUploadOpen(false);
          void queryClient.invalidateQueries({
            queryKey: ["assets", projectId],
          });
          toast.success("数据已完成导入并进入资产目录。");
        }}
      />
      {toast.node}
    </div>
  );
}

function spatialCount(asset: Asset) {
  const spatial = asset.manifest.spatial as
    | { layers?: unknown[]; bands?: unknown[] }
    | undefined;
  return spatial?.layers?.length ?? spatial?.bands?.length ?? 0;
}

function UploadWizard({
  open,
  onClose,
  role,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  role: Role;
  onDone: () => void;
}) {
  const { token } = useSession();
  const projectId = useProjectId();
  const [step, setStep] = useState(1);
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<UploadView | null>(null);
  const [columns, setColumns] = useState(
    '[{"name":"station","type":"string"},{"name":"time","type":"datetime"},{"name":"value","type":"number"}]',
  );
  const [nullValues, setNullValues] = useState('["", "NA"]');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) {
      setStep(1);
      setFile(null);
      setUpload(null);
      setError("");
    }
  }, [open]);
  async function uploadFile() {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const bytes = await file.arrayBuffer();
      const digest = `sha256:${Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      )
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("")}`;
      const lower = file.name.toLowerCase();
      const format = lower.endsWith(".parquet")
        ? "parquet"
        : lower.endsWith(".gpkg")
          ? "geopackage"
          : lower.endsWith(".tif") || lower.endsWith(".tiff")
            ? "geotiff"
            : "csv";
      const created = await mutate<UploadView>(
        token,
        `/projects/${projectId}/uploads`,
        { filename: file.name, format, size: file.size, digest },
      );
      const completed = await uploadBytes<UploadView>(
        token,
        `/projects/${projectId}/uploads/${created.upload_id}/content`,
        bytes,
      );
      setUpload(completed);
      setStep(2);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setBusy(false);
    }
  }
  async function finalize() {
    if (!upload) return;
    setBusy(true);
    setError("");
    try {
      const spatial = ["geopackage", "geotiff"].includes(upload.format);
      await mutate(
        token,
        `/projects/${projectId}/uploads/${upload.upload_id}/finalize`,
        spatial
          ? { kind: "SPATIAL", profile: "strict-spatial-v1" }
          : {
              columns: upload.format === "csv" ? JSON.parse(columns) : [],
              null_values: JSON.parse(nullValues),
            },
      );
      setStep(3);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      open={open}
      title="上传数据"
      onClose={onClose}
      footer={
        step === 1 ? (
          <>
            <Button variant="secondary" onClick={onClose}>
              取消
            </Button>
            <PermissionButton
              role={role}
              busy={busy}
              disabled={!file}
              onClick={uploadFile}
            >
              上传并继续
            </PermissionButton>
          </>
        ) : step === 2 ? (
          <>
            <Button variant="secondary" onClick={() => setStep(1)}>
              上一步
            </Button>
            <PermissionButton role={role} busy={busy} onClick={finalize}>
              检查并导入
            </PermissionButton>
          </>
        ) : (
          <Button onClick={onDone}>完成</Button>
        )
      }
    >
      <ol className="steps">
        <li className={step >= 1 ? "active" : ""}>1 选择文件</li>
        <li className={step >= 2 ? "active" : ""}>2 导入规则</li>
        <li className={step >= 3 ? "active" : ""}>3 完成</li>
      </ol>
      {step === 1 ? (
        <div className="upload-drop">
          <Upload size={28} />
          <strong>
            {file?.name ?? "选择 CSV、Parquet、GeoPackage 或 GeoTIFF"}
          </strong>
          <p>文件摘要会在本地计算，服务端检查成功后生成不可变资产版本。</p>
          <input
            type="file"
            accept=".csv,.parquet,.gpkg,.tif,.tiff"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </div>
      ) : null}
      {step === 2 && upload ? (
        <div className="form-grid">
          <Banner tone="success">
            {upload.filename} 已上传，摘要与大小已经确认。
          </Banner>
          {["csv", "parquet"].includes(upload.format) ? (
            <>
              <Field label="列定义（JSON）">
                <textarea
                  rows={7}
                  value={columns}
                  onChange={(event) => setColumns(event.target.value)}
                />
              </Field>
              <Field label="空值标记（JSON）">
                <textarea
                  rows={3}
                  value={nullValues}
                  onChange={(event) => setNullValues(event.target.value)}
                />
              </Field>
            </>
          ) : (
            <Banner>
              空间文件将使用 strict-spatial-v1 检查 CRS、图层和几何结构。
            </Banner>
          )}
        </div>
      ) : null}
      {step === 3 ? (
        <EmptyState
          title="导入请求已确认"
          description="资产检查在服务端完成。返回目录后可刷新查看最终状态和新版本。"
        />
      ) : null}
      {error ? <Banner tone="danger">{error}</Banner> : null}
    </Modal>
  );
}

export function AssetDetailPage() {
  const session = useSession();
  const { token } = session;
  const projectId = useProjectId();
  const { assetId = "" } = useParams();
  const summary = useSummary();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedVersion = searchParams.get("version");
  const [tab, setTab] = useState("overview");
  const [semantics, setSemantics] = useState(
    '{"profile":"spatial-generic-v1","annotations":[]}',
  );
  const [previewUrl, setPreviewUrl] = useState("");
  const asset = useQuery({
    queryKey: ["asset", projectId, assetId, requestedVersion],
    queryFn: async () => {
      const version =
        requestedVersion ??
        (
          await requestAllPages<Asset>(token, `/projects/${projectId}/assets`)
        )
          .filter((item) => item.ref.asset_id === assetId)
          .at(-1)?.ref.version_id;
      if (!version) throw new Error("资产不存在");
      return request<Asset>(
        token,
        `/projects/${projectId}/assets/${assetId}/versions/${version}`,
      );
    },
  });
  useEffect(() => {
    if (!requestedVersion && asset.data?.ref.version_id) {
      const next = new URLSearchParams(searchParams);
      next.set("version", asset.data.ref.version_id);
      setSearchParams(next, { replace: true });
    }
  }, [asset.data?.ref.version_id, requestedVersion, searchParams, setSearchParams]);
  useEffect(
    () => () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    },
    [previewUrl],
  );
  async function download(rep: Representation, preview = false) {
    if (!asset.data) return;
    try {
      const blob = await downloadRepresentation(
        token,
        projectId,
        assetId,
        asset.data.ref.version_id,
        rep.representation_id,
      );
      const url = URL.createObjectURL(blob);
      if (preview) setPreviewUrl(url);
      else {
        const link = document.createElement("a");
        link.href = url;
        link.download = `${assetId}-${rep.representation_id}`;
        link.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) {
      toast.failure(error);
    }
  }
  async function saveSemantics() {
    if (!asset.data) return;
    try {
      await mutate(
        token,
        `/projects/${projectId}/assets/${assetId}/semantic-revisions`,
        {
          expected_revision:
            asset.data.semantic_revisions?.at(-1)?.revision ?? 0,
          version_id: asset.data.ref.version_id,
          semantics: JSON.parse(semantics),
        },
      );
      await asset.refetch();
      toast.success("语义修订已追加。");
    } catch (error) {
      toast.failure(error);
    }
  }
  if (asset.isLoading) return <PageLoading />;
  if (asset.error || !asset.data)
    return <PageError error={asset.error} retry={() => void asset.refetch()} />;
  const value = asset.data;
  const role = resolveRole(summary.data, session.projects, projectId);
  const image = value.manifest.representations?.find(
    (item) => item.representation_id === "image",
  );
  return (
    <div className="page">
      <PageHeader
        title={assetId}
        description={`${value.manifest.kind} · 不可变版本 ${value.ref.version_id}`}
        crumbs={[
          { label: "数据资产", href: `/projects/${projectId}/assets` },
          { label: assetId },
        ]}
        action={<Badge>{value.manifest.kind}</Badge>}
      />
      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "overview", label: "概览" },
          { id: "schema", label: "字段 / 空间" },
          { id: "representations", label: "表示形式" },
          { id: "semantics", label: "语义修订" },
          { id: "map", label: "静态地图" },
        ]}
      />
      {tab === "overview" ? (
        <div className="detail-grid">
          <Card title="版本身份">
            <dl className="definition-list">
              <div>
                <dt>资产 ID</dt>
                <dd>
                  <code>{value.ref.asset_id}</code>
                </dd>
              </div>
              <div>
                <dt>版本</dt>
                <dd>{value.ref.version_id}</dd>
              </div>
              <div>
                <dt>清单摘要</dt>
                <dd>
                  <code>{value.ref.version_manifest_digest}</code>
                </dd>
              </div>
              <div>
                <dt>驱动</dt>
                <dd>{value.manifest.driver ?? "—"}</dd>
              </div>
            </dl>
          </Card>
          <Card title="清单">
            <JsonView value={value.manifest} />
          </Card>
        </div>
      ) : null}
      {tab === "schema" ? (
        <Card
          title={
            value.manifest.kind === "OBSERVATIONS" ? "字段定义" : "空间信息"
          }
        >
          <JsonView
            value={value.manifest.fields ?? value.manifest.spatial ?? {}}
          />
        </Card>
      ) : null}
      {tab === "representations" ? (
        <Card title="受信表示形式">
          <DataTable
            rows={
              (value.manifest.representations ?? []).map((item) => ({
                ...item,
                id: item.representation_id,
              })) as Array<Record<string, unknown>>
            }
            columns={[
              { key: "representation_id", label: "表示 ID" },
              { key: "media_type", label: "媒体类型" },
              { key: "digest", label: "摘要" },
              {
                key: "action",
                label: "操作",
                render: (row) => (
                  <Button
                    variant="secondary"
                    onClick={() =>
                      void download(row as unknown as Representation)
                    }
                  >
                    下载
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      ) : null}
      {tab === "semantics" ? (
        <Card
          title="追加语义修订"
          description="历史修订不可覆盖；服务端仍是最终校验权威。"
          action={
            <PermissionButton role={role} onClick={() => void saveSemantics()}>
              保存修订
            </PermissionButton>
          }
        >
          <div className="detail-grid">
            <Field label="语义负载（JSON）">
              <textarea
                className="code-input"
                rows={14}
                value={semantics}
                onChange={(event) => setSemantics(event.target.value)}
              />
            </Field>
            <div>
              <h3>修订历史</h3>
              {value.semantic_revisions?.length ? (
                value.semantic_revisions.map((revision) => (
                  <div className="timeline-item" key={revision.revision}>
                    <Badge>REV {revision.revision}</Badge>
                    <JsonView value={revision.payload} />
                  </div>
                ))
              ) : (
                <EmptyState title="尚无语义修订" />
              )}
            </div>
          </div>
        </Card>
      ) : null}
      {tab === "map" ? (
        <Card
          title="受信静态地图"
          description="只显示平台输出，不连接外部地图服务。"
          action={
            image ? (
              <Button
                variant="secondary"
                onClick={() => void download(image, true)}
              >
                加载预览
              </Button>
            ) : null
          }
        >
          {image ? (
            previewUrl ? (
              <img
                className="map-preview"
                src={previewUrl}
                alt={`${assetId} 静态地图`}
              />
            ) : (
              <EmptyState
                title="地图尚未加载"
                description="点击加载预览读取受信 PNG 表示。"
              />
            )
          ) : (
            <EmptyState title="该资产没有静态地图表示" />
          )}
        </Card>
      ) : null}
      {toast.node}
    </div>
  );
}
