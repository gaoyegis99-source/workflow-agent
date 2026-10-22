# Capability Studio A5 / 0.14.4 阶段报告

> HISTORICAL. A5 / 0.14.4 阶段记录；当前发布以机器可读 Release pointer 为准。

## 完成内容

A5 在 A4 `0.14.3` 基线上完成受限高级 CWL 编辑闭环，发行版本为 `0.14.4`。Platform schema 仍为 `0022_operator_proposals`，Data、Execution、Knowledge 均无迁移。

- 增加包内子工作流、精确布尔输入条件、三种 scatter、数组/record/nullable/File，以及受限 `linkMerge`、`pickValue`。
- 增加安全 JSON/YAML 导入。解析器拒绝重复键、YAML alias/anchor、自定义 tag、非 JSON 标量、非有限数值、超出精确范围的整数、外部引用和可执行内容；导入不自动保存 Revision。
- Studio 支持多文档 source、子流程、条件、scatter 和版本差异展示；受支持源码与画布能够无损往返，未知或不安全结构保持只读或明确拒绝。
- Validation、Candidate 与 Trial 统一使用 `a5-v1` 编译环境。旧 Validation 保留历史，但必须重新验证后才能冻结 A5 Candidate。
- 新 Trial 冻结 `workflow-trial-case-v2`，分别记录计划、观测、启动、成功、跳过、动态模板、最终输出形状和未覆盖分支。旧 `v1` Trial 按历史 required steps 兼容投影。
- 条件全跳过或空 scatter 可以针对冻结样例得到 PASS，但不能单独支持 Release；至少一条 PASS Trial 必须真实启动并结算计算节点。
- 工作流执行仍走既有 Task、grant、预算、Action、Execution、Toil、原子发布和科学 Acceptance。样例 PASS 不等同于科学 VERIFIED。

完整边界见 [A5 高级 CWL 契约](../architecture/a5-advanced-cwl-contract.md)，升级与回退见 [A5 运维说明](../a5-operations.md)。

## 测试结果

### A5 与 Authoring 门禁

```bash
uv run --locked pytest -q tests/authoring/a5 tests/integration/test_api.py
# 40 passed

bash scripts/check_authoring.sh --phase advanced-cwl
# Studio typecheck passed
# Studio 2 files / 6 tests passed
# Studio production build passed
# A1–A5 Python: 98 passed in 152.73s
# ruff check passed
# 680 files formatted
```

A5 目录自身收集 29 项测试。门禁覆盖 JSON/YAML 等价导入、重复键和 YAML 对抗输入、三种 scatter、重复值身份、条件跳过、空 scatter、1,000 节点上限、子流程调用路径、结构形状、真实固定 cwltool 容器校验、Candidate READY、动态覆盖、旧 Trial 兼容、全跳过发布限制及真实浏览器刷新和内存令牌行为。

### 目录、架构与规则回归

```bash
bash scripts/check_p7.sh --phase catalog
# 24 passed
# 13 P1 selection/locator rules passed

uv run --locked pytest -q tests/architecture/test_boundaries.py
# 33 passed
```

Operator Catalog 仍为 11 个受信计算算子；A5 没有把 authoring source、SERVICE_TOOL 或 Checker 变成可直接执行能力。

### 完整兼容回归

```bash
bash scripts/check_p7.sh --phase compatibility
# 919 passed in 2346.50s (0:39:06)
```

P0–P7 的 PostgreSQL、HTTP、Docker、Toil、QGIS、远程恢复、Knowledge、Acceptance、Agent、审核算子和 A1–A4 Studio 行为全部通过，无失败或必测跳过。按计划未重跑未修改的 P4 长节点性能矩阵。

### 独立发行安装

```bash
uv run --locked python scripts/verify_distribution.py
```

独立构建和仓库外安装通过：11 个 wheel/sdist、core 标准库边界、锁定依赖、API/OpenAPI、四套数据库迁移与 schema 检查、管理 CLI、各类 Worker，以及 P3、冻结 CWL/Toil、GeoPackage/QGIS/Native 和静态制图真实容器闭环。

Release Lock schema v2 固定：

- 11 个 `0.14.4` wheel；
- Operator、Checker、Tool 三个 Catalog；
- 7 个不可变镜像摘要；
- 3 个 Studio 前端资源；
- 391 个源码身份文件。

最终 release digest：`sha256:97b3446d5e8faeb457710b42349e9ad03971c081cee9092cc9af0afe3e722c4a`。

## 问题处理

1. **旧 A1 浏览器断言。** A5 编辑器替换了原只读区标题，首次 Authoring 门禁为 97 通过、1 失败。恢复兼容标题，同时保留新的导入和编辑面板；最终 98 项通过。
2. **HTTP 依赖边界。** 完整兼容首轮为 918 通过、1 失败。安全导入适配器直接引用了 domain 异常，且边界清单尚未登记 API 已声明的 PyYAML 依赖。异常引用改为 application 层出口，移除直接 Pydantic 引用并登记 `yaml`；33 项架构、40 项 A5/API 及 919 项完整回归通过。
3. **CWL runtime 锁版本。** 首次镜像构建发现 runtime 锁仍指向旧工作区版本，构建被阻止。重新生成 runtime lock 后构建 `0.14.4` 固定镜像；镜像内容摘要保持为 `sha256:652754092f5cf48486a9c8bb42f113d1d95d25141ef7d846be44ff8a18504a09`。
4. **发行目录清理被环境拒绝。** 未绕过保护或删除旧证据，改在新的 `wheels-final` 目录从最终源码构建 11 个 wheel 和 11 个 sdist，再据此生成最终 Release Lock。

没有通过跳过用例、放宽导入安全、伪造 Trial 结果、短接计算节点或修改历史执行处理这些问题。

## 范围说明

A5 支持的是受限且可确定验证的 CWL 子集。条件只允许精确布尔输入引用；不支持任意 JavaScript、ExpressionTool、Shell、外部 import/include、URL 抓取、用户镜像、用户命令、动态插件、工作流市场、Agent 编写工作流或跨平台导出。

动态 scatter 仍使用既有保守恢复和运行时额度限制。A5 没有新的性能目标，也没有改变 Toil、Worker 池、计算预算或远程执行协议。Trial 样例用于证明冻结输入下的结构和结果断言，科学可信度继续由 Deliverable、CoveragePlan 和独立 Checker 判定。

## 阶段结论

A5 通过。用户可以安全导入和编辑受限高级 CWL，冻结候选，使用真实 Task/grant/预算试运行，查看分支与 scatter 覆盖，并用至少包含一次真实计算的 PASS 证据发布不可变版本。故障和重放不改变既有 Action、节点、预算或成果身份。A1–A5 完整兼容、发行安装和代码质量门禁全部通过，Capability Studio 首期范围完成；本轮不自动进入工作流市场、Agent 编写或其他阶段。
