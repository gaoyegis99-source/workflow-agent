import { BookOpenCheck, FileSearch, Search } from "lucide-react";
import { useState } from "react";

import { request } from "../../shared/api";
import { Badge, Banner, Button, Card, EmptyState, Field, JsonView } from "../../shared/ui";
import { errorText, PageHeader, useProjectId } from "../../core/support";
import { useSession } from "../../core/session";

type Candidate = {
  document_id: string;
  version_id: string;
  unit_id: string;
  title: string;
  snippet: string;
  score: number;
  discovery_channels: Array<"LEXICAL" | "SEMANTIC">;
  exact_read_required: true;
};

type FusionResult = {
  status: "RESULTS" | "NO_RESULTS" | "PARTIAL";
  query_digest: string;
  lexical_index_version: string;
  semantic_status: "RESULTS" | "NO_RESULTS" | "INDEX_UNAVAILABLE";
  semantic_reason: string;
  candidate_count: number;
  items: Candidate[];
};

type ExactEvidence = {
  evidence_ref: {
    project_id: string;
    document_id: string;
    version_id: string;
    unit_id: string;
    content_digest: string;
    exact_text_digest: string;
  };
  text: string;
  prefix: string;
  suffix: string;
  availability: "ACTIVE" | "WITHDRAWN";
  lifecycle_revision: number;
  read_receipt_id: string;
};

/**
 * Candidate discovery is deliberately separate from exact evidence reads.
 * This page has no action, publication, or confirmation control: a user can
 * inspect evidence here, while governed Agent proposals remain in Task review.
 */
export function KnowledgePage() {
  const { token } = useSession();
  const projectId = useProjectId();
  const [query, setQuery] = useState("");
  const [indexId, setIndexId] = useState("");
  const [profileDigest, setProfileDigest] = useState("");
  const [result, setResult] = useState<FusionResult | null>(null);
  const [evidence, setEvidence] = useState<ExactEvidence | null>(null);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function discover() {
    setBusy(true);
    setError("");
    setEvidence(null);
    try {
      const next = await request<FusionResult>(
        token,
        `/projects/${projectId}/knowledge/fusion-search`,
        {
          method: "POST",
          body: JSON.stringify({
            query,
            limit: 20,
            index: { index_id: indexId, profile_digest: profileDigest },
          }),
        },
      );
      setResult(next);
    } catch (nextError) {
      setResult(null);
      setError(errorText(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function readExact(candidate: Candidate) {
    const key = `${candidate.document_id}:${candidate.version_id}:${candidate.unit_id}`;
    setReading(key);
    setError("");
    try {
      const next = await request<ExactEvidence>(
        token,
        `/projects/${projectId}/knowledge/documents/${candidate.document_id}` +
          `/versions/${candidate.version_id}/units/${candidate.unit_id}`,
      );
      setEvidence(next);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setReading(null);
    }
  }

  const validIndex =
    indexId.trim().length > 0 && /^sha256:[0-9a-f]{64}$/.test(profileDigest);
  const validQuery = query.trim().length > 0 && query.length <= 200;
  return (
    <div className="page">
      <PageHeader
        title="知识证据"
        description="融合检索只用于发现候选；任何事实引用都必须先精确读取当前证据单元。"
      />
      <Banner tone="warning" title="候选不是证据">
        语义或词法命中不能授权操作、确定事实或绕过项目角色。Agent 的提案仍需在任务页按既有确认流程审阅。
      </Banner>
      <Card title="融合发现" description="输入已冻结的语义索引身份；索引不可用时仍可显示词法候选，并会明确标示。">
        <div className="form-grid">
          <Field label="查询">
            <input value={query} maxLength={200} onChange={(event) => setQuery(event.target.value)} placeholder="输入要发现的主题或问题" />
          </Field>
          <Field label="语义索引 ID" hint="由受控索引构建流程提供，页面不会创建或猜测索引。">
            <input value={indexId} onChange={(event) => setIndexId(event.target.value)} placeholder="例如 project-knowledge-v1" />
          </Field>
          <Field label="索引档案摘要" hint="固定模型、维度、分段器和文档投影版本。">
            <input value={profileDigest} onChange={(event) => setProfileDigest(event.target.value)} placeholder="sha256:…" spellCheck={false} />
          </Field>
        </div>
        <div className="button-row">
          <Button busy={busy} disabled={!validQuery || !validIndex} onClick={() => void discover()}>
            <Search size={16} />发现候选
          </Button>
        </div>
        {error ? <Banner tone="danger" title="请求未完成">{error}</Banner> : null}
      </Card>
      {result ? (
        <Card
          title="候选结果"
          description={`词法索引 ${result.lexical_index_version}；语义状态 ${result.semantic_status} / ${result.semantic_reason}。`}
          action={<Badge tone={result.status === "PARTIAL" ? "warning" : undefined}>{result.status}</Badge>}
        >
          {result.items.length ? (
            <div className="stack-list">
              {result.items.map((candidate) => {
                const key = `${candidate.document_id}:${candidate.version_id}:${candidate.unit_id}`;
                return (
                  <article className="list-card" key={key}>
                    <div className="list-card-title">
                      <FileSearch size={18} />
                      <div><strong>{candidate.title}</strong><small>{candidate.document_id} / {candidate.version_id} / {candidate.unit_id}</small></div>
                    </div>
                    <p>{candidate.snippet}</p>
                    <div className="button-row">
                      {candidate.discovery_channels.map((channel) => <Badge key={channel} tone="info">{channel}</Badge>)}
                      <Badge tone="warning">需精确读取</Badge>
                      <Button variant="secondary" busy={reading === key} onClick={() => void readExact(candidate)}>
                        <BookOpenCheck size={16} />精确读取
                      </Button>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <EmptyState title="没有可用候选" description="没有匹配项，或语义索引尚未构建、已过期或当前不可用。" />
          )}
        </Card>
      ) : null}
      {evidence ? (
        <Card title="精确证据阅读" description="该阅读回执才是后续引用或受确认协作提案可使用的证据身份。">
          <Banner tone={evidence.availability === "ACTIVE" ? "info" : "danger"} title={`证据 ${evidence.availability}`}>
            生命周期修订 {evidence.lifecycle_revision}；阅读回执 {evidence.read_receipt_id}
          </Banner>
          <blockquote>{evidence.text}</blockquote>
          <JsonView value={evidence.evidence_ref} />
        </Card>
      ) : null}
    </div>
  );
}
