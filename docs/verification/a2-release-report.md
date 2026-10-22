# Capability Studio A2 / 0.14.1 阶段报告

> HISTORICAL. A2 / 0.14.1 阶段记录；当前发布以机器可读 Release pointer 为准。

## 完成内容

- 受限图形编辑器支持根输入、11 个已登记算子、数据边、固定参数和最终输出。React Flow 只维护画布投影，保存对象仍是不可变 `v7-cwl-p4-v1` source revision。
- JSON Forms 使用服务端派生的参数 Schema；服务端 Pydantic DTO 进行最终校验。源码视图只读，不能无损投影的高级图保持只读。
- 图形往返保留 source 中声明的输出顺序，同时展示描述符中的全部可信输入和输出端口；新增连线会把对应输出纳入 source，不会因画布投影丢失未连接端口。
- 新增无副作用结构预览。预览按当前权限解析精确样例资产，传播受信表格或空间结构，明确区分兼容、冲突和需要绑定；不落库、不创建 Action、不占预算。
- 新增 Platform `0020_authoring_validation`、验证仓储和独立 `platform-authoring-worker`。正式校验冻结 source、目录、语义契约、编译器和固定 cwltool 镜像，通过租约与 generation 隔离恢复。
- cwltool 校验容器固定为 1 CPU、512 MiB、64 个进程，无网络、只读根目录、非 root、删除全部 capability 并启用 `no-new-privileges`。
- 新增三个 A2 公开路由：`POST /authoring/previews`、`POST /workflow-definitions/{definition_id}/validations` 和 `GET /authoring/validations/{validation_id}`。没有 A3 candidates、trials、releases 路由或表。
- 工作区 11 个 wheel、CWL runtime 和 Studio 统一为 `0.14.1`。Release lock 固定 11 个 wheel、3 个目录、7 个镜像和 3 个前端资源。

## 测试结果

| 门禁 | 实际结果 |
| --- | --- |
| `uv run --locked pytest -q tests/authoring/a2` | 18 passed；包含真实 PostgreSQL、独立 Worker 子进程、真实固定 cwltool 容器及容器资源边界 |
| Studio `typecheck / test / build` | TypeScript 通过；2 个文件、4 项测试通过；生产 bundle 成功 |
| `bash scripts/check_authoring.sh --phase editor` | A1+A2 共 35 passed；前端 typecheck/test/build、Ruff 和格式检查通过 |
| `bash scripts/check_p7.sh --phase catalog` | 24 pytest + 13 项 P1 规则通过；Ruff 通过 |
| `bash scripts/check_p7.sh --phase compatibility` | 首次完整执行为 918 passed、1 failed；唯一失败是旧 P3 发布后取消断言。统一事实规则后，直接受影响的 P3 故障与执行套件 16/16 通过，无遗留失败 |
| `uv run --locked python scripts/verify_distribution.py` | 11 个 wheel/sdist 构建、仓库外锁定安装、API/迁移/CLI/Worker、P3 容器、P4 Toil、QGIS 空间与制图闭环全部通过 |
| `uv run --locked ruff check .` / `ruff format --check .` | 通过；643 个文件格式一致 |

必测覆盖包括：环、缺输入或输出、未知实现、字段删除和类型改变、Validate 报告身份、PNG→Table、未知结构、跨项目样例、乱序预览、参数错误位置、高级图只读、validator 不可用、租约接管、环境改变、展示变化、容器资源隔离和 source↔canvas 往返。

完整兼容门禁没有在修复后再重复运行全部 919 项。修复仅涉及直接执行在“Data 已原子提交、取消随后到达”时的终态仲裁；完整运行已证明其余 918 项通过，随后覆盖该路径及相邻响应丢失、发布和取消分支的 16 项套件全部通过。结构化证据保留两次结果，避免把它描述成一次 919 项全绿运行。

## 问题处理

- 初版把正式校验装入通用基础设施 Worker，违反层次依赖。现已拆为 `platform-authoring-worker` 组合入口；基础设施继续只依赖 Core，API 不绕过应用层。
- 初版前端预览只在样例文本变化时丢弃旧响应。现已覆盖参数、节点、边和删除操作，旧异步响应不能覆盖新图。
- 初版画布只生成 source 已声明的输出 handle，导致合法的未使用输出无法在 UI 中连线。现改为显示描述符的全部可信端口，并单独保存 source 的输出声明和顺序。
- cwltool 校验容器原先具备网络、文件系统和身份隔离，但缺少固定 CPU、内存和进程上限。现已补齐生产参数和真实容器回归。
- 兼容测试暴露旧 P3 断言与 P4.3.2 已接受的“事实先发生者获胜”语义冲突。现在 Data 原子提交先完成时执行保持 `SUCCEEDED`，记录 `TOO_LATE_COMMITTED`；中止先完成时才取消并记录 `ABORTED_BEFORE_COMMIT`。
- 生产启动同时核对固定 CWL 镜像和 release lock。API 与独立 Worker 使用同一描述符和语义环境，目录或镜像错配不能形成当前有效 PASS。
- Vite 报告单一 JS chunk 约 659.90 KiB。该警告不影响 A2 逻辑门禁；A2 没有前端性能目标，后续页面扩张时再按编辑器面板拆包。

## 范围说明

A2 支持受限基础图保存、结构预览和语法/契约正式校验。校验 PASS 只证明冻结 source 能被当前受信编译器和 cwltool 接受，不证明算子已经执行，也不证明科学结果正确。

A3 的候选、真实试运行、样例 verdict 和工作流版本发布均未实现。A5 的条件、scatter、子工作流和受限源码导入也未提前开放。

## 阶段结论

A2 通过，版本为 `0.14.1`，Platform schema 为 `0020_authoring_validation`。代码、迁移、真实容器、独立发行安装、历史闭环和结构化发布证据均已交付，本轮停在 A2。
