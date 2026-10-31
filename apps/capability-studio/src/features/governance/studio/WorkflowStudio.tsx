import { JsonForms } from "@jsonforms/react";
import { vanillaCells, vanillaRenderers } from "@jsonforms/vanilla-renderers";
import {
  addEdge,
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  type AssetSummary,
  type AuthoringDescriptor,
  type AuthoringSourceImport,
  type CommandReceipt,
  newKey,
  type OperatorDescriptor,
  type OperatorProposal,
  type PreviewResult,
  request,
  type TaskSummary,
  type ValidationView,
  releaseExecutionBody,
  type WorkflowCandidate,
  type WorkflowDefinition,
  type WorkflowMetadata,
  type WorkflowRelease,
  type WorkflowTrial,
  type WorkflowReleaseLifecycle,
} from "../../../shared/api";
import { requestAllPages } from "../../../shared/pagination";
import {
  canvasToSource,
  compareOperatorVersions,
  LatestRequest,
  layoutFromNodes,
  sourceToCanvas,
  type CanvasNodeData,
  type CanvasProjection,
  type JsonValue,
} from "./source";
import { emptyMetadata, descriptorMap, trustedTarget } from "./workflowStudioAdapter";
import { workflowStudioAjv } from "./workflowStudioValidation";
import { groupWorkflowReleases } from "./workflowStudioReleaseGroups";
import { IssueList, workflowNodeTypes } from "./WorkflowStudioViews";
import { ConfirmDialog } from "../../../shared/ui";
import "../../../styles/workflow.css";

const SourceEditor = lazy(() => import("./SourceEditor"));

export function WorkflowStudio({
  token,
  project,
  mode = "editor",
  role = "VIEWER",
}: {
  token: string;
  project: string;
  mode?: "editor" | "releases" | "operators";
  role?: "VIEWER" | "EDITOR" | "OWNER";
}) {
  const [descriptor, setDescriptor] = useState<AuthoringDescriptor | null>(
    null,
  );
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([]);
  const [selectedOperator, setSelectedOperator] = useState<string | null>(null);
  const [selectedDefinition, setSelectedDefinition] = useState<string | null>(
    null,
  );
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [stableKey, setStableKey] = useState("");
  const [metadata, setMetadata] = useState<WorkflowMetadata>(emptyMetadata);
  const [projection, setProjection] = useState<CanvasProjection>({
    editable: true,
    name: "workflow",
    nodes: [],
    edges: [],
  });
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<CanvasNodeData>>(
    [],
  );
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [sampleBindings, setSampleBindings] = useState("{}");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [validation, setValidation] = useState<ValidationView | null>(null);
  const [candidates, setCandidates] = useState<WorkflowCandidate[]>([]);
  const [selectedCandidate, setSelectedCandidate] = useState("");
  const [trials, setTrials] = useState<WorkflowTrial[]>([]);
  const [releases, setReleases] = useState<WorkflowRelease[]>([]);
  const [releaseLibrary, setReleaseLibrary] = useState<WorkflowRelease[]>([]);
  const [selectedRelease, setSelectedRelease] = useState("");
  const [releaseReceipt, setReleaseReceipt] = useState<CommandReceipt | null>(
    null,
  );
  const [releaseLifecycle, setReleaseLifecycle] = useState<
    WorkflowReleaseLifecycle[]
  >([]);
  const [lifecycleReason, setLifecycleReason] = useState("");
  const [pendingLifecycle, setPendingLifecycle] = useState<
    "ACTIVE" | "DISABLED" | "REVOKED" | null
  >(null);
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [taskId, setTaskId] = useState("");
  const [trialInputs, setTrialInputs] = useState("{}");
  const [trialExpectations, setTrialExpectations] = useState("[]");
  const [maxNodes, setMaxNodes] = useState(1);
  const [versionLabel, setVersionLabel] = useState("1.0.0");
  const [operatorProposals, setOperatorProposals] = useState<
    OperatorProposal[]
  >([]);
  const [proposalStableKey, setProposalStableKey] = useState(
    "table-profile-registration",
  );
  const [proposalMaterials, setProposalMaterials] = useState("{}");
  const [sourceFormat, setSourceFormat] = useState<"JSON" | "YAML">("JSON");
  const [sourceText, setSourceText] = useState("");
  const [editorView, setEditorView] = useState<"visual" | "source">("visual");
  const [nextOperatorVersion, setNextOperatorVersion] = useState("");
  const [message, setMessage] = useState("输入临时令牌和项目 ID 后加载目录。");
  const previewSequence = useRef(new LatestRequest());
  const canEdit = role === "EDITOR" || role === "OWNER";
  const canOwn = role === "OWNER";

  const operators = descriptor?.operators ?? [];
  const definition = useMemo(
    () => definitions.find((item) => item.definition_id === selectedDefinition),
    [definitions, selectedDefinition],
  );
  const operator = useMemo(
    () =>
      operators.find(
        (item) => `${item.operator_id}@${item.version_id}` === selectedOperator,
      ),
    [operators, selectedOperator],
  );
  const activeNode = useMemo(
    () => nodes.find((item) => item.id === selectedNode),
    [nodes, selectedNode],
  );
  const activeDescriptor =
    activeNode?.data.nodeKind === "OPERATOR"
      ? operators.find(
          (item) =>
            item.operator_id === activeNode.data.operatorId &&
            item.version_id === activeNode.data.versionId,
        )
      : undefined;
  const versionCandidates = activeDescriptor
    ? operators.filter(
        (item) =>
          item.operator_id === activeDescriptor.operator_id &&
          item.version_id !== activeDescriptor.version_id,
      )
    : [];
  const selectedVersionDescriptor = versionCandidates.find(
    (item) => item.version_id === nextOperatorVersion,
  );
  const versionChange =
    activeDescriptor && selectedVersionDescriptor
      ? compareOperatorVersions(activeDescriptor, selectedVersionDescriptor)
      : null;
  const releaseGroups = useMemo(
    () => groupWorkflowReleases(releaseLibrary),
    [releaseLibrary],
  );

  function installProjection(next: CanvasProjection) {
    setProjection(next);
    setNodes(next.nodes);
    setEdges(next.edges);
    setSelectedNode(null);
    setPreview(null);
    setValidation(null);
    setNextOperatorVersion("");
    setSourceText(next.source ? JSON.stringify(next.source, null, 2) : "");
  }

  async function load() {
    if (!token || !project) return;
    try {
      const [nextDescriptor, draftPage, taskPage, proposalPage, releasePage] =
        await Promise.all([
          request<AuthoringDescriptor>(
            token,
            `/projects/${project}/authoring/operator-descriptors`,
          ),
          request<{ items: WorkflowDefinition[] }>(
            token,
            `/projects/${project}/workflow-definitions`,
          ),
          request<{ items: TaskSummary[] }>(
            token,
            `/projects/${project}/tasks`,
          ),
          request<{ items: OperatorProposal[] }>(
            token,
            `/projects/${project}/operator-proposals`,
          ),
          request<{ items: WorkflowRelease[] }>(
            token,
            `/projects/${project}/workflow-releases`,
          ),
        ]);
      const assetItems = await requestAllPages<AssetSummary>(
        token,
        `/projects/${project}/assets`,
      ).catch(() => []);
      setDescriptor(nextDescriptor);
      setDefinitions(draftPage.items);
      setTasks(taskPage.items);
      setAssets(assetItems);
      setOperatorProposals(proposalPage.items);
      setReleaseLibrary(releasePage.items);
      setMessage(
        `已加载 ${nextDescriptor.operators.length} 个算子、${draftPage.items.length} 个草稿、${releasePage.items.length} 个正式版本和 ${proposalPage.items.length} 项接入材料。`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "加载失败");
    }
  }

  useEffect(() => {
    setDescriptor(null);
    setDefinitions([]);
    setSelectedDefinition(null);
    setCandidates([]);
    setTrials([]);
    setReleases([]);
    setReleaseLibrary([]);
    setSelectedRelease("");
    setReleaseReceipt(null);
    setReleaseLifecycle([]);
    setTasks([]);
    setAssets([]);
    setOperatorProposals([]);
    installProjection({
      editable: true,
      name: "workflow",
      nodes: [],
      edges: [],
    });
    if (token && project) void load();
  }, [project, token]);

  function selectDefinition(item: WorkflowDefinition) {
    setSelectedDefinition(item.definition_id);
    setMetadata(item.head.metadata);
    installProjection(
      sourceToCanvas(
        item.head.source,
        item.head.layout,
        descriptorMap(operators),
      ),
    );
    void loadA3(item.definition_id);
  }

  async function loadA3(definitionId = selectedDefinition) {
    if (!definitionId) return;
    try {
      const prefix = `/projects/${project}/workflow-definitions/${definitionId}`;
      const [candidatePage, trialPage, releasePage] = await Promise.all([
        request<{ items: WorkflowCandidate[] }>(token, `${prefix}/candidates`),
        request<{ items: WorkflowTrial[] }>(token, `${prefix}/trial-runs`),
        request<{ items: WorkflowRelease[] }>(token, `${prefix}/releases`),
      ]);
      setCandidates(candidatePage.items);
      setTrials(trialPage.items);
      setReleases(releasePage.items);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "加载 A3 状态失败");
    }
  }

  async function createDefinition() {
    try {
      const created = await request<WorkflowDefinition>(
        token,
        `/projects/${project}/workflow-definitions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            expected_revision: 0,
            stable_key: stableKey,
            metadata,
            layout: {},
          }),
        },
      );
      setDefinitions((items) => [...items, created]);
      selectDefinition(created);
      setMessage("已创建空白草稿 revision 1，可升级为 A2 受限图。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "创建失败");
    }
  }

  function currentProjection(): CanvasProjection {
    return { ...projection, nodes, edges };
  }

  async function saveRevision() {
    if (!definition || !descriptor || !projection.editable) return;
    try {
      const source = canvasToSource(
        currentProjection(),
        descriptorMap(operators),
      );
      const revision = await request<WorkflowDefinition["head"]>(
        token,
        `/projects/${project}/workflow-definitions/${definition.definition_id}/revisions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            expected_revision: definition.head_revision,
            source_digest: definition.head.source_digest,
            source,
            metadata,
            layout: layoutFromNodes(nodes),
          }),
        },
      );
      setDefinitions((items) =>
        items.map((item) =>
          item.definition_id === definition.definition_id
            ? { ...item, head_revision: revision.revision, head: revision }
            : item,
        ),
      );
      installProjection(
        sourceToCanvas(
          revision.source,
          revision.layout,
          descriptorMap(operators),
        ),
      );
      setMessage(`已保存受限图 revision ${revision.revision}。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
    }
  }

  function nextName(prefix: string) {
    const used = new Set(nodes.map((node) => node.data.label));
    let index = 1;
    while (used.has(`${prefix}${index}`)) index += 1;
    return `${prefix}${index}`;
  }

  function ensureEditable() {
    if (!projection.editable) {
      setMessage(projection.reason ?? "该源码只能只读。");
      return false;
    }
    return true;
  }

  function addInput() {
    if (!ensureEditable()) return;
    const label = nextName("input");
    previewSequence.current.invalidate();
    setPreview(null);
    const documentId = projection.source?.entrypoint ?? "main";
    setNodes((items) => [
      ...items,
      {
        id: `input:${label}`,
        type: "capability",
        position: { x: 40, y: 60 + items.length * 28 },
        data: {
          nodeKind: "INPUT",
          documentId,
          label,
          portNames: [label],
          sourceType: "File",
        },
      },
    ]);
  }

  function addOutput() {
    if (!ensureEditable()) return;
    const label = nextName("output");
    previewSequence.current.invalidate();
    setPreview(null);
    const documentId = projection.source?.entrypoint ?? "main";
    setNodes((items) => [
      ...items,
      {
        id: `output:${label}`,
        type: "capability",
        position: { x: 780, y: 60 + items.length * 28 },
        data: {
          nodeKind: "OUTPUT",
          documentId,
          label,
          portNames: [label],
          outputType: "File",
        },
      },
    ]);
  }

  function addOperator() {
    if (!operator || !ensureEditable()) return;
    const target = trustedTarget(operator);
    if (!target) {
      setMessage("该算子没有 A2 可用的受信本地实现。");
      return;
    }
    const label = nextName(operator.operator_id.replaceAll("-", "_"));
    const documentId = projection.source?.entrypoint ?? "main";
    setProjection((value) => {
      if (!value.source) return value;
      const source = structuredClone(value.source);
      source.implementations[`impl_${label}`] = target;
      return { ...value, source };
    });
    previewSequence.current.invalidate();
    setPreview(null);
    setNodes((items) => [
      ...items,
      {
        id: `step:${label}`,
        type: "capability",
        position: { x: 360, y: 60 + items.length * 28 },
        data: {
          nodeKind: "OPERATOR",
          documentId,
          label,
          portNames: [
            ...Object.keys(operator.input_ports),
            ...Object.keys(operator.output_ports),
          ],
          inputPorts: Object.keys(operator.input_ports),
          outputPorts: Object.keys(operator.output_ports),
          operatorId: operator.operator_id,
          versionId: operator.version_id,
          implementationRef: `impl_${label}`,
          parameters: {},
          declaredOutputs: Object.keys(operator.output_ports),
        },
      },
    ]);
  }

  async function importSource() {
    if (!definition) return;
    try {
      const imported = await request<AuthoringSourceImport>(
        token,
        `/projects/${project}/authoring/imports`,
        {
          method: "POST",
          body: JSON.stringify({ format: sourceFormat, content: sourceText }),
        },
      );
      const next = sourceToCanvas(
        imported.source,
        {},
        descriptorMap(operators),
      );
      installProjection(next);
      setSourceFormat("JSON");
      setMessage(
        `源码已安全解析为 ${imported.source_digest}；保存修订前不会改变草稿。`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "源码导入失败");
    }
  }

  function refreshSourceText() {
    try {
      setSourceFormat("JSON");
      setSourceText(
        JSON.stringify(
          canvasToSource(currentProjection(), descriptorMap(operators)),
          null,
          2,
        ),
      );
      setMessage("已从当前画布刷新源码文本，尚未保存修订。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "源码投影失败");
    }
  }

  function applyOperatorVersion() {
    if (
      !activeNode ||
      !activeDescriptor ||
      !selectedVersionDescriptor ||
      !versionChange
    )
      return;
    if (!versionChange.safeToApplyWithoutDisconnecting) {
      setMessage("新版本删除了端口；请先显式处理连接，系统不会自动断线升级。");
      return;
    }
    const target = trustedTarget(selectedVersionDescriptor);
    if (!target || !activeNode.data.implementationRef) return;
    setProjection((value) => {
      if (!value.source) return value;
      const source = structuredClone(value.source);
      source.implementations[activeNode.data.implementationRef!] = target;
      return { ...value, source };
    });
    setNodes((items) =>
      items.map((item) =>
        item.id === activeNode.id
          ? {
              ...item,
              data: {
                ...item.data,
                versionId: selectedVersionDescriptor.version_id,
                inputPorts: Object.keys(selectedVersionDescriptor.input_ports),
                outputPorts: Object.keys(
                  selectedVersionDescriptor.output_ports,
                ),
                portNames: [
                  ...Object.keys(selectedVersionDescriptor.input_ports),
                  ...Object.keys(selectedVersionDescriptor.output_ports),
                ],
              },
            }
          : item,
      ),
    );
    setNextOperatorVersion("");
    setValidation(null);
    setMessage("算子版本变化已应用到未保存草稿；旧候选和版本没有改变。");
  }

  function connect(connection: Connection) {
    if (!projection.editable || !connection.target || !connection.targetHandle)
      return;
    previewSequence.current.invalidate();
    setPreview(null);
    if (connection.source?.startsWith("step:") && connection.sourceHandle) {
      const port = connection.sourceHandle.replace(/^out:/, "");
      setNodes((items) =>
        items.map((item) =>
          item.id === connection.source &&
          !(item.data.declaredOutputs ?? []).includes(port)
            ? {
                ...item,
                data: {
                  ...item.data,
                  declaredOutputs: [...(item.data.declaredOutputs ?? []), port],
                },
              }
            : item,
        ),
      );
    }
    setEdges((items) =>
      addEdge(
        connection,
        items.filter(
          (edge) =>
            !(
              edge.target === connection.target &&
              edge.targetHandle === connection.targetHandle
            ),
        ),
      ),
    );
  }

  function updateParameters(value: JsonValue) {
    if (!activeNode) return;
    previewSequence.current.invalidate();
    setNodes((items) =>
      items.map((item) =>
        item.id === activeNode.id
          ? { ...item, data: { ...item.data, parameters: value } }
          : item,
      ),
    );
    setPreview(null);
  }

  function changeNodes(changes: NodeChange<Node<CanvasNodeData>>[]) {
    if (changes.some((change) => change.type === "remove")) {
      previewSequence.current.invalidate();
      setPreview(null);
    }
    onNodesChange(changes);
  }

  function changeEdges(changes: EdgeChange<Edge>[]) {
    previewSequence.current.invalidate();
    setPreview(null);
    onEdgesChange(changes);
  }

  async function runPreview() {
    if (!descriptor || !projection.editable) return;
    const sequence = previewSequence.current.begin();
    try {
      const source = canvasToSource(
        currentProjection(),
        descriptorMap(operators),
      );
      const bindings = JSON.parse(sampleBindings) as Record<string, unknown>;
      const result = await request<PreviewResult>(
        token,
        `/projects/${project}/authoring/previews`,
        {
          method: "POST",
          body: JSON.stringify({
            source,
            sample_bindings: bindings,
            semantic_contract_digest: descriptor.semantic_contract_digest,
          }),
        },
      );
      if (!previewSequence.current.current(sequence)) return;
      setPreview(result);
      setMessage(
        `结构预览：${result.status}，${result.issues.length} 条提示。`,
      );
    } catch (error) {
      if (previewSequence.current.current(sequence)) {
        setPreview(null);
        setMessage(error instanceof Error ? error.message : "预览失败");
      }
    }
  }

  async function requestValidation() {
    if (!definition || !descriptor) return;
    try {
      const receipt = await request<ValidationView>(
        token,
        `/projects/${project}/workflow-definitions/${definition.definition_id}/validations`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            revision: definition.head_revision,
            source_digest: definition.head.source_digest,
            semantic_contract_digest: descriptor.semantic_contract_digest,
          }),
        },
      );
      setValidation(receipt);
      setMessage("正式校验已排队。");
      let current = receipt;
      for (
        let attempt = 0;
        attempt < 8 && ["QUEUED", "RUNNING"].includes(current.status);
        attempt += 1
      ) {
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        current = await request<ValidationView>(
          token,
          current.status_url.replace(/^\/api\/v1/, ""),
        );
        setValidation(current);
      }
      setMessage(
        `正式校验：${current.status}${current.current_valid === false && current.status === "PASSED" ? "（已过期）" : ""}`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "正式校验失败");
    }
  }

  async function refreshValidation() {
    if (!validation) return;
    try {
      const value = await request<ValidationView>(
        token,
        validation.status_url.replace(/^\/api\/v1/, ""),
      );
      setValidation(value);
      setMessage(
        `正式校验：${value.status}${value.current_valid === false && value.status === "PASSED" ? "（已过期）" : ""}`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "查询校验失败");
    }
  }

  async function freezeCandidate() {
    if (
      !definition ||
      !validation?.current_valid ||
      validation.status !== "PASSED"
    )
      return;
    try {
      const value = await request<WorkflowCandidate>(
        token,
        `/projects/${project}/workflow-definitions/${definition.definition_id}/candidates`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            revision: definition.head_revision,
            source_digest: definition.head.source_digest,
            validation_id: validation.validation_id,
          }),
        },
      );
      setSelectedCandidate(value.candidate_id);
      await loadA3(definition.definition_id);
      setMessage("候选已冻结，等待受信包处理。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "冻结候选失败");
    }
  }

  async function startTrial() {
    if (!definition || !selectedCandidate || !taskId) return;
    const task = tasks.find((item) => item.task_id === taskId);
    if (!task) return;
    try {
      await request(
        token,
        `/projects/${project}/workflow-definitions/${definition.definition_id}/trial-runs`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            candidate_id: selectedCandidate,
            task_id: taskId,
            expected_spec_revision: task.spec_revision,
            inputs: JSON.parse(trialInputs),
            max_node_executions: maxNodes,
            trial_expectations: JSON.parse(trialExpectations),
          }),
        },
      );
      await loadA3(definition.definition_id);
      setMessage("真实试运行已提交；预算和授权沿用所选任务。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "试运行提交失败");
    }
  }

  async function publishRelease() {
    if (!definition || !selectedCandidate) return;
    const trialIds = trials
      .filter(
        (item) =>
          item.candidate_id === selectedCandidate &&
          item.case_verdict === "PASS",
      )
      .map((item) => item.trial_id);
    try {
      await request(
        token,
        `/projects/${project}/workflow-definitions/${definition.definition_id}/releases`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            candidate_id: selectedCandidate,
            version_label: versionLabel,
            trial_ids: trialIds,
          }),
        },
      );
      await loadA3(definition.definition_id);
      await loadReleaseLibrary();
      setMessage(`已发布不可变版本 ${versionLabel}；样例通过不等于科学验收。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "发布失败");
    }
  }

  async function loadReleaseLibrary() {
    try {
      const page = await request<{ items: WorkflowRelease[] }>(
        token,
        `/projects/${project}/workflow-releases`,
      );
      setReleaseLibrary(page.items);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "加载正式版本库失败");
    }
  }

  async function executePublishedRelease() {
    const release = releaseLibrary.find(
      (item) => item.release_id === selectedRelease,
    );
    const task = tasks.find((item) => item.task_id === taskId);
    if (!release || !task) return;
    try {
      const value = await request<CommandReceipt>(
        token,
        `/projects/${project}/workflow-definitions/${release.definition_id}/releases/${release.version_label}/executions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify(
            releaseExecutionBody(task, JSON.parse(trialInputs), maxNodes),
          ),
        },
      );
      setReleaseReceipt(value);
      let current = value;
      for (
        let attempt = 0;
        attempt < 8 && !["RESOLVED", "REJECTED"].includes(current.status);
        attempt += 1
      ) {
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        current = await request<CommandReceipt>(
          token,
          current.status_url.replace(/^\/api\/v1/, ""),
        );
        setReleaseReceipt(current);
      }
      setMessage(
        `正式版本 ${release.stable_key}@${release.version_label} 命令状态：${current.status}；Action 已冻结 Release 身份。`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "正式版本执行失败");
    }
  }

  async function selectPublishedRelease(item: WorkflowRelease) {
    setSelectedRelease(item.release_id);
    setReleaseReceipt(null);
    try {
      const page = await request<{ items: WorkflowReleaseLifecycle[] }>(
        token,
        `/projects/${project}/workflow-definitions/${item.definition_id}/releases/${item.version_label}/lifecycle-revisions`,
      );
      setReleaseLifecycle(page.items);
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "加载版本生命周期失败",
      );
    }
  }

  async function changeReleaseLifecycle(
    lifecycle: "ACTIVE" | "DISABLED" | "REVOKED",
  ) {
    const release = releaseLibrary.find(
      (item) => item.release_id === selectedRelease,
    );
    if (!release || !lifecycleReason.trim()) return;
    try {
      await request<WorkflowReleaseLifecycle>(
        token,
        `/projects/${project}/workflow-definitions/${release.definition_id}/releases/${release.version_label}/lifecycle-revisions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            expected_lifecycle_revision: release.lifecycle_revision,
            lifecycle,
            reason: lifecycleReason,
          }),
        },
      );
      await loadReleaseLibrary();
      const page = await request<{ items: WorkflowReleaseLifecycle[] }>(
        token,
        `/projects/${project}/workflow-definitions/${release.definition_id}/releases/${release.version_label}/lifecycle-revisions`,
      );
      setReleaseLifecycle(page.items);
      setLifecycleReason("");
      setPendingLifecycle(null);
      setMessage(
        `版本 ${release.version_label} 生命周期已变更为 ${lifecycle}。`,
      );
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "版本生命周期变更失败",
      );
    }
  }

  async function copyRelease(item: WorkflowRelease) {
    if (!definition) return;
    try {
      const revision = await request<WorkflowDefinition["head"]>(
        token,
        `/projects/${project}/workflow-definitions/${definition.definition_id}/revisions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            expected_revision: definition.head_revision,
            source_digest: definition.head.source_digest,
            source: item.source,
            metadata,
            layout: {},
          }),
        },
      );
      setDefinitions((items) =>
        items.map((value) =>
          value.definition_id === definition.definition_id
            ? { ...value, head_revision: revision.revision, head: revision }
            : value,
        ),
      );
      installProjection(
        sourceToCanvas(
          revision.source,
          revision.layout,
          descriptorMap(operators),
        ),
      );
      setMessage(
        `版本 ${item.version_label} 已复制为 revision ${revision.revision} 草稿。`,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "复制版本失败");
    }
  }

  async function refreshOperatorProposals() {
    try {
      const result = await request<{ items: OperatorProposal[] }>(
        token,
        `/projects/${project}/operator-proposals`,
      );
      setOperatorProposals(result.items);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "加载接入材料失败");
    }
  }

  async function createOperatorProposal() {
    try {
      await request<OperatorProposal>(
        token,
        `/projects/${project}/operator-proposals`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            stable_key: proposalStableKey,
            materials: JSON.parse(proposalMaterials),
          }),
        },
      );
      await refreshOperatorProposals();
      setMessage("接入材料已保存为 DRAFT；保存不会安装或执行任何内容。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "接入材料保存失败");
    }
  }

  async function reviseOperatorProposal(item: OperatorProposal) {
    try {
      await request(
        token,
        `/projects/${project}/operator-proposals/${item.proposal_id}/revisions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            expected_revision: item.head_revision,
            materials: JSON.parse(proposalMaterials),
          }),
        },
      );
      await refreshOperatorProposals();
      setMessage("材料修订已追加；历史评审记录没有被覆盖。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "材料修订失败");
    }
  }

  async function submitOperatorProposal(item: OperatorProposal) {
    try {
      await request(
        token,
        `/projects/${project}/operator-proposals/${item.proposal_id}/submit`,
        {
          method: "POST",
          headers: { "Idempotency-Key": newKey() },
          body: JSON.stringify({
            revision: item.head_revision,
            materials_digest: item.head.materials_digest,
          }),
        },
      );
      await refreshOperatorProposals();
      setMessage("材料已提交维护者评审；项目 OWNER 不能自行审核或部署。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "提交评审失败");
    }
  }

  const renderedSource = useMemo(() => {
    if (!descriptor || !projection.editable)
      return definition?.head.source ?? {};
    try {
      return canvasToSource(currentProjection(), descriptorMap(operators));
    } catch {
      return definition?.head.source ?? {};
    }
  }, [nodes, edges, projection, descriptor, operators, definition]);
  const hasUnsavedChanges = Boolean(
    mode === "editor" &&
      canEdit &&
      definition &&
      (JSON.stringify(renderedSource) !==
        JSON.stringify(definition.head.source) ||
        JSON.stringify(metadata) !== JSON.stringify(definition.head.metadata) ||
        (editorView === "source" &&
          sourceText !== JSON.stringify(definition.head.source, null, 2))),
  );
  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const protect = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const protectNavigation = (event: MouseEvent) => {
      const anchor = (event.target as Element | null)?.closest("a[href]");
      if (
        anchor &&
        !window.confirm("当前工作流有未保存更改。确定离开并放弃这些更改吗？")
      ) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", protect);
    document.addEventListener("click", protectNavigation, true);
    return () => {
      window.removeEventListener("beforeunload", protect);
      document.removeEventListener("click", protectNavigation, true);
    };
  }, [hasUnsavedChanges]);

  return (
    <div className={`workflow-studio workflow-${mode}`}>
      <p className="status" role="status">
        <span className="status-pulse" aria-hidden="true" />
        {message}
      </p>
      {mode === "editor" && (
        <>
          <div className="editor-commandbar">
            <div>
              <button
                className={editorView === "visual" ? "selected" : ""}
                onClick={() => setEditorView("visual")}
              >
                可视化
              </button>
              <button
                className={editorView === "source" ? "selected" : ""}
                onClick={() => setEditorView("source")}
              >
                源码
              </button>
            </div>
            <span>不会自动保存 · 服务端校验为最终权威</span>
          </div>
          {editorView === "visual" ? (
            <>
              <section className="studio-grid" id="workbench">
                <aside className="sidebar">
                  <h2>草稿</h2>
                  <ul className="list">
                    {definitions.map((item) => (
                      <li key={item.definition_id}>
                        <button
                          className={
                            selectedDefinition === item.definition_id
                              ? "selected"
                              : ""
                          }
                          onClick={() => selectDefinition(item)}
                        >
                          {item.head.metadata.title}
                          <small>
                            {item.stable_key} · r{item.head_revision}
                          </small>
                        </button>
                      </li>
                    ))}
                  </ul>
                  <hr />
                  <label>
                    稳定键
                    <input
                      disabled={!canEdit}
                      value={stableKey}
                      onChange={(event) => setStableKey(event.target.value)}
                    />
                  </label>
                  <label>
                    标题
                    <input
                      disabled={!canEdit}
                      value={metadata.title}
                      onChange={(event) =>
                        setMetadata({ ...metadata, title: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    说明
                    <textarea
                      disabled={!canEdit}
                      value={metadata.description}
                      onChange={(event) =>
                        setMetadata({
                          ...metadata,
                          description: event.target.value,
                        })
                      }
                    />
                  </label>
                  <button
                    title={!canEdit ? "需要 EDITOR 角色" : undefined}
                    onClick={createDefinition}
                    disabled={
                      !canEdit ||
                      !token ||
                      !project ||
                      !stableKey ||
                      !metadata.title
                    }
                  >
                    新建草稿
                  </button>
                </aside>
                <section className="workspace">
                  <div className="toolbar">
                    <button
                      onClick={addInput}
                      disabled={!canEdit || !definition}
                    >
                      添加输入
                    </button>
                    <button
                      onClick={addOutput}
                      disabled={!canEdit || !definition}
                    >
                      添加最终输出
                    </button>
                    <label>
                      算子
                      <select
                        disabled={!canEdit}
                        value={selectedOperator ?? ""}
                        onChange={(event) =>
                          setSelectedOperator(event.target.value || null)
                        }
                      >
                        <option value="">选择算子</option>
                        {operators.map((item) => (
                          <option
                            key={`${item.operator_id}@${item.version_id}`}
                            value={`${item.operator_id}@${item.version_id}`}
                          >
                            {item.operator_id}@{item.version_id}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      onClick={addOperator}
                      disabled={!canEdit || !definition || !operator}
                    >
                      添加算子
                    </button>
                    <button
                      onClick={saveRevision}
                      disabled={!canEdit || !definition || !projection.editable}
                    >
                      保存修订
                    </button>
                  </div>
                  {!projection.editable && (
                    <div className="read-only">
                      只读保护：{projection.reason}
                    </div>
                  )}
                  <div className="flow-canvas">
                    <ReactFlow
                      nodes={nodes}
                      edges={edges}
                      nodeTypes={workflowNodeTypes}
                      onNodesChange={canEdit ? changeNodes : undefined}
                      onEdgesChange={canEdit ? changeEdges : undefined}
                      onConnect={canEdit ? connect : undefined}
                      nodesDraggable={canEdit}
                      nodesConnectable={canEdit}
                      elementsSelectable
                      onNodeClick={(_, node) => setSelectedNode(node.id)}
                      fitView
                    >
                      <MiniMap />
                      <Controls />
                      <Background />
                    </ReactFlow>
                  </div>
                </section>
                <aside className="inspector">
                  <h2>属性与校验</h2>
                  {activeDescriptor && activeNode ? (
                    <>
                      <h3>{activeNode.data.label}</h3>
                      <JsonForms
                        readonly={!canEdit}
                        schema={activeDescriptor.parameter_schema}
                        data={activeNode.data.parameters ?? {}}
                        renderers={vanillaRenderers}
                        cells={vanillaCells}
                        ajv={workflowStudioAjv}
                        validationMode="ValidateAndShow"
                        onChange={({ data }) => {
                          if (canEdit) updateParameters(data as JsonValue);
                        }}
                      />
                      {versionCandidates.length > 0 && (
                        <div className="version-change">
                          <h3>算子版本变更</h3>
                          <select
                            disabled={!canEdit}
                            aria-label="目标算子版本"
                            value={nextOperatorVersion}
                            onChange={(event) =>
                              setNextOperatorVersion(event.target.value)
                            }
                          >
                            <option value="">选择版本</option>
                            {versionCandidates.map((item) => (
                              <option
                                key={item.version_id}
                                value={item.version_id}
                              >
                                {item.version_id}
                              </option>
                            ))}
                          </select>
                          {versionChange && (
                            <>
                              <pre>
                                {JSON.stringify(versionChange, null, 2)}
                              </pre>
                              <button
                                onClick={applyOperatorVersion}
                                disabled={
                                  !canEdit ||
                                  !versionChange.safeToApplyWithoutDisconnecting
                                }
                              >
                                应用到新草稿修订
                              </button>
                            </>
                          )}
                        </div>
                      )}
                    </>
                  ) : (
                    <p>
                      选择算子节点编辑固定参数。子工作流、条件和 scatter
                      身份在节点上明确展示。
                    </p>
                  )}
                  <h3>样例绑定 JSON</h3>
                  <textarea
                    disabled={!canEdit}
                    value={sampleBindings}
                    onChange={(event) => {
                      previewSequence.current.invalidate();
                      setSampleBindings(event.target.value);
                      setPreview(null);
                    }}
                  />
                  <div className="actions">
                    <button
                      onClick={runPreview}
                      disabled={!canEdit || !definition || !projection.editable}
                    >
                      结构预览
                    </button>
                    <button
                      onClick={requestValidation}
                      disabled={!canEdit || !definition}
                    >
                      正式校验
                    </button>
                    {validation && (
                      <button onClick={refreshValidation}>刷新校验</button>
                    )}
                  </div>
                  {preview && (
                    <IssueList
                      title={`预览 ${preview.status}`}
                      issues={preview.issues}
                    />
                  )}
                  {validation && (
                    <IssueList
                      title={`校验 ${validation.status}${validation.current_valid === false && validation.status === "PASSED" ? "（过期）" : ""}`}
                      issues={validation.issues ?? []}
                    />
                  )}
                </aside>
              </section>
              {(preview || validation) && (
                <section className="validation-drawer">
                  <header>
                    <strong>校验结果</strong>
                    <span>
                      {preview?.status ?? "未预览"} ·{" "}
                      {validation?.status ?? "未提交正式校验"}
                    </span>
                  </header>
                  <div>
                    {preview ? (
                      <IssueList title="结构预览" issues={preview.issues} />
                    ) : null}
                    {validation ? (
                      <IssueList
                        title="服务端正式校验"
                        issues={validation.issues ?? []}
                      />
                    ) : null}
                  </div>
                </section>
              )}
            </>
          ) : (
            <section className="source-panel" id="source">
              <div className="source-toolbar">
                <div>
                  <h2>受限源码编辑与导入</h2>
                  <p>
                    只解析自包含
                    JSON/YAML；外部加载、脚本、Shell、镜像、路径、别名和自定义标签会被拒绝。
                  </p>
                </div>
                <div className="actions">
                  <label>
                    格式
                    <select
                      aria-label="源码格式"
                      value={sourceFormat}
                      onChange={(event) =>
                        setSourceFormat(event.target.value as "JSON" | "YAML")
                      }
                    >
                      <option value="JSON">JSON</option>
                      <option value="YAML">YAML</option>
                    </select>
                  </label>
                  <button
                    onClick={refreshSourceText}
                    disabled={!canEdit || !definition || !projection.editable}
                  >
                    从画布刷新
                  </button>
                  <button
                    onClick={() => void importSource()}
                    disabled={!canEdit || !definition || !sourceText}
                  >
                    安全解析并投影
                  </button>
                </div>
              </div>
              <Suspense
                fallback={
                  <div className="editor-loading">正在加载本地源码编辑器…</div>
                }
              >
                <SourceEditor
                  value={sourceText}
                  format={sourceFormat}
                  readOnly={!canEdit || !projection.editable}
                  onChange={setSourceText}
                />
              </Suspense>
              <details className="frozen-source">
                <summary>冻结规范源码（只读）</summary>
                <pre>{JSON.stringify(renderedSource, null, 2)}</pre>
              </details>
            </section>
          )}
        </>
      )}
      {mode === "releases" && (
        <>
          <section className="release-context">
            <label>
              工作流定义
              <select
                aria-label="工作流定义"
                value={selectedDefinition ?? ""}
                onChange={(event) => {
                  const item = definitions.find(
                    (value) => value.definition_id === event.target.value,
                  );
                  if (item) selectDefinition(item);
                }}
              >
                <option value="">选择草稿以查看候选、试运行和版本</option>
                {definitions.map((item) => (
                  <option key={item.definition_id} value={item.definition_id}>
                    {item.head.metadata.title} · {item.stable_key} · r
                    {item.head_revision}
                  </option>
                ))}
              </select>
            </label>
          </section>
          <section className="library-center" id="library">
            <article>
              <h2>项目工作流版本库</h2>
              <p>
                按稳定键分组展示本项目不可变版本。SAMPLE_VERIFIED
                表示冻结样例通过，不代表科学 VERIFIED。
              </p>
              <button
                onClick={() => void loadReleaseLibrary()}
                disabled={!token || !project}
              >
                刷新版本库
              </button>
              {releaseGroups.length ? (
                releaseGroups.map(([key, items]) => (
                  <section className="release-group" key={key}>
                    <h3>{key}</h3>
                    <ul className="list">
                      {items.map((item) => (
                        <li key={item.release_id}>
                          <button
                            className={
                              selectedRelease === item.release_id
                                ? "selected"
                                : ""
                            }
                            onClick={() => void selectPublishedRelease(item)}
                          >
                            {item.metadata.title} · {item.version_label}
                            <small>
                              {item.stable_key} · {item.qualification}
                              <br />
                              {item.lifecycle} · 生命周期 r
                              {item.lifecycle_revision}
                              <br />
                              {item.current_executable
                                ? "当前可执行"
                                : `当前不可执行：${item.unavailable_reasons.join(", ")}`}
                              <br />
                              Release {item.release_id}
                            </small>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))
              ) : (
                <p>暂无不可变版本。</p>
              )}
            </article>
            <article>
              <h2>运行与生命周期</h2>
              <p>
                服务端从所选 Release 解析精确 Package。OWNER
                可停用、恢复或永久撤销；历史版本和既有事实不会删除。
              </p>
              <label>
                正式执行 DIRECT Task
                <select
                  value={taskId}
                  onChange={(event) => setTaskId(event.target.value)}
                >
                  <option value="">选择任务</option>
                  {tasks.map((item) => (
                    <option key={item.task_id} value={item.task_id}>
                      {item.task_id} · {item.lifecycle}/{item.control} · r
                      {item.spec_revision}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                版本节点额度上限
                <input
                  disabled={!canEdit}
                  type="number"
                  min="1"
                  max="1000"
                  value={maxNodes}
                  onChange={(event) => setMaxNodes(Number(event.target.value))}
                />
              </label>
              <label>
                版本输入 JSON
                <textarea
                  disabled={!canEdit}
                  value={trialInputs}
                  onChange={(event) => setTrialInputs(event.target.value)}
                />
              </label>
              <button
                onClick={executePublishedRelease}
                disabled={
                  !canEdit ||
                  !selectedRelease ||
                  !taskId ||
                  !releaseLibrary.find(
                    (item) => item.release_id === selectedRelease,
                  )?.current_executable
                }
              >
                提交正式执行
              </button>
              {releaseReceipt && (
                <div className="receipt">
                  <strong>{releaseReceipt.status}</strong>
                  <small>
                    Command {releaseReceipt.command_id}
                    <br />
                    Action {releaseReceipt.action_id ?? "尚未知"}
                    <br />
                    状态 {releaseReceipt.status_url}
                  </small>
                </div>
              )}
              <hr />
              <p>生命周期操作将在确认对话框中要求填写审计原因。</p>
              <div className="actions">
                <button
                  onClick={() => setPendingLifecycle("DISABLED")}
                  disabled={
                    !canOwn ||
                    !selectedRelease ||
                    releaseLibrary.find(
                      (item) => item.release_id === selectedRelease,
                    )?.lifecycle !== "ACTIVE"
                  }
                >
                  停用新执行
                </button>
                <button
                  onClick={() => setPendingLifecycle("ACTIVE")}
                  disabled={
                    !canOwn ||
                    !selectedRelease ||
                    releaseLibrary.find(
                      (item) => item.release_id === selectedRelease,
                    )?.lifecycle !== "DISABLED"
                  }
                >
                  恢复新执行
                </button>
                <button
                  onClick={() => setPendingLifecycle("REVOKED")}
                  disabled={
                    !canOwn ||
                    !selectedRelease ||
                    releaseLibrary.find(
                      (item) => item.release_id === selectedRelease,
                    )?.lifecycle === "REVOKED"
                  }
                >
                  永久撤销
                </button>
              </div>
              <details>
                <summary>生命周期历史</summary>
                <pre>{JSON.stringify(releaseLifecycle, null, 2)}</pre>
              </details>
            </article>
          </section>
          <section className="release-grid" id="governance">
            <article>
              <h2>候选</h2>
              <p>
                仅当前 PASSED 校验可以冻结；候选包会由固定 cwltool
                镜像重新核验。
              </p>
              <button
                onClick={freezeCandidate}
                disabled={!canEdit || !validation?.current_valid}
              >
                冻结当前候选
              </button>
              <button onClick={() => void loadA3()} disabled={!definition}>
                刷新 A3 状态
              </button>
              <ul className="list">
                {candidates.map((item) => (
                  <li key={item.candidate_id}>
                    <button
                      className={
                        selectedCandidate === item.candidate_id
                          ? "selected"
                          : ""
                      }
                      onClick={() => setSelectedCandidate(item.candidate_id)}
                    >
                      {item.status} · r{item.revision}
                      <small>
                        {item.candidate_id}
                        <br />
                        {item.package_ref?.manifest_digest ??
                          item.error?.code ??
                          "等待包处理"}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            </article>
            <article>
              <h2>真实试运行</h2>
              <label>
                已有 DIRECT Task
                <select
                  value={taskId}
                  onChange={(event) => setTaskId(event.target.value)}
                >
                  <option value="">选择任务</option>
                  {tasks.map((item) => (
                    <option key={item.task_id} value={item.task_id}>
                      {item.task_id} · {item.lifecycle}/{item.control} · r
                      {item.spec_revision}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                最大计算节点
                <input
                  disabled={!canEdit}
                  type="number"
                  min="1"
                  max="1000"
                  value={maxNodes}
                  onChange={(event) => setMaxNodes(Number(event.target.value))}
                />
              </label>
              <label>
                冻结输入 JSON
                <textarea
                  disabled={!canEdit}
                  value={trialInputs}
                  onChange={(event) => setTrialInputs(event.target.value)}
                />
              </label>
              <details>
                <summary>可选资产引用（只读）</summary>
                <pre>{JSON.stringify(assets, null, 2)}</pre>
              </details>
              <label>
                样例断言 JSON
                <textarea
                  disabled={!canEdit}
                  value={trialExpectations}
                  onChange={(event) => setTrialExpectations(event.target.value)}
                />
              </label>
              <button
                onClick={startTrial}
                disabled={!canEdit || !selectedCandidate || !taskId}
              >
                使用任务预算试运行
              </button>
              <ul className="facts">
                {trials.map((item) => (
                  <li key={item.trial_id}>
                    <strong>{item.case_verdict}</strong> · execution{" "}
                    {item.execution_outcome}
                    <br />
                    <small>
                      Action {item.action_id}
                      <br />
                      Execution {item.execution_id ?? "尚未知"}
                      <br />
                      Case {item.trial_case_digest}
                      <br />
                      {item.error_category ?? ""}
                    </small>
                    {item.case_result?.coverage !== undefined && (
                      <details>
                        <summary>条件、scatter 与输出形状覆盖</summary>
                        <pre>
                          {JSON.stringify(item.case_result.coverage, null, 2)}
                        </pre>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            </article>
            <article>
              <h2>不可变版本</h2>
              <p>
                版本仅表示冻结样例通过，科学 VERIFIED 仍由 Deliverable、Checker
                和 Acceptance 给出。
              </p>
              <label>
                SemVer
                <input
                  disabled={!canEdit}
                  value={versionLabel}
                  onChange={(event) => setVersionLabel(event.target.value)}
                />
              </label>
              <button
                onClick={publishRelease}
                disabled={
                  !canEdit ||
                  !selectedCandidate ||
                  !trials.some(
                    (item) =>
                      item.candidate_id === selectedCandidate &&
                      item.case_verdict === "PASS",
                  )
                }
              >
                发布 PASS 试运行
              </button>
              <ul className="facts">
                {releases.map((item) => (
                  <li key={item.release_id}>
                    <strong>{item.version_label}</strong> · {item.qualification}
                    <br />
                    <small>
                      source {item.source_digest}
                      <br />
                      package {item.package_ref.manifest_digest}
                      <br />
                      context {item.context_digest}
                      <br />
                      {item.current_executable
                        ? "当前可执行"
                        : `当前不可执行：${item.unavailable_reasons.join(", ")}`}
                    </small>
                    <br />
                    <button
                      disabled={!canEdit}
                      onClick={() => void copyRelease(item)}
                    >
                      复制为新草稿
                    </button>
                  </li>
                ))}
              </ul>
            </article>
          </section>
        </>
      )}
      {mode === "operators" && (
        <section className="proposal-center" id="proposals">
          <article>
            <h2>提交接入材料</h2>
            <p>
              这里只保存受限文本、契约和摘要；服务器不会获取来源、构建镜像或执行命令。
            </p>
            <label>
              接入材料标识
              <input
                disabled={!canEdit}
                value={proposalStableKey}
                onChange={(event) => setProposalStableKey(event.target.value)}
              />
            </label>
            <label>
              受限材料 JSON
              <textarea
                disabled={!canEdit}
                value={proposalMaterials}
                onChange={(event) => setProposalMaterials(event.target.value)}
              />
            </label>
            <button
              onClick={createOperatorProposal}
              disabled={!canEdit || !token || !project || !proposalStableKey}
            >
              创建材料草稿
            </button>
            <button
              onClick={() => void refreshOperatorProposals()}
              disabled={!token || !project}
            >
              刷新材料
            </button>
          </article>
          <article>
            <h2>评审记录与部署匹配</h2>
            <p>
              APPROVED 只表示该修订经过评审；只有精确匹配当前 Release Lock 和
              Operator Catalog 才能显示为可执行。
            </p>
            <ul className="facts">
              {operatorProposals.map((item) => (
                <li key={item.proposal_id}>
                  <strong>
                    {item.stable_key} · r{item.head_revision} · {item.status}
                  </strong>
                  <br />
                  <small>
                    {item.proposal_id}
                    <br />
                    materials {item.head.materials_digest}
                    <br />
                    部署匹配 {item.deployment_match.status}
                    {item.deployment_match.current_executable
                      ? " · 当前可执行"
                      : " · 当前不可执行"}
                    <br />
                    {item.deployment_match.reasons.join(", ")}
                  </small>
                  <div>
                    <button
                      disabled={!canEdit}
                      onClick={() => {
                        setProposalStableKey(item.stable_key);
                        setProposalMaterials(
                          JSON.stringify(item.head.materials, null, 2),
                        );
                      }}
                    >
                      载入当前材料
                    </button>
                    <button
                      onClick={() => void reviseOperatorProposal(item)}
                      disabled={!canEdit || item.status === "SUBMITTED"}
                    >
                      追加修订
                    </button>
                    <button
                      onClick={() => void submitOperatorProposal(item)}
                      disabled={
                        !canEdit ||
                        !(
                          ["DRAFT", "CHANGES_REQUESTED", "REJECTED"] as string[]
                        ).includes(item.status)
                      }
                    >
                      提交维护者评审
                    </button>
                  </div>
                  <details>
                    <summary>历史修订、评审与部署证据</summary>
                    <pre>
                      {JSON.stringify(
                        {
                          revisions: item.revisions,
                          review_events: item.review_events,
                          deployment_receipts: item.deployment_receipts,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </details>
                </li>
              ))}
            </ul>
          </article>
        </section>
      )}
      <ConfirmDialog
        open={pendingLifecycle !== null}
        title={`${pendingLifecycle ?? ""} 工作流版本`}
        description={
          pendingLifecycle === "REVOKED"
            ? "撤销不可逆；历史版本和既有执行事实仍会保留。"
            : "生命周期变更只影响后续准入，不删除历史事实。"
        }
        reason={lifecycleReason}
        onReason={setLifecycleReason}
        onCancel={() => {
          setPendingLifecycle(null);
          setLifecycleReason("");
        }}
        onConfirm={() => {
          if (pendingLifecycle) void changeReleaseLifecycle(pendingLifecycle);
        }}
      />
    </div>
  );
}
