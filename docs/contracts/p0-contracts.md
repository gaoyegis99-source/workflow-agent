# P0 契约与可用接口

历史范围说明：本文件记录 P0 交付时的接口；当前已扩展至 [P2 控制核心](p2-control-contracts.md)。首批引用类型和错误封套继续使用本文件的约束，当前路由与健康阶段以 P2 文档为准。

日期：2026-10-12。契约版本：`platform_contracts.v1`，发行包版本 `0.1.0`。

本文记录已实现范围。架构文档中的项目、任务、命令、计算等路由尚不可调用；这里只定义这些对象的首批数据形状，不登记或执行它们。

## 1 实际 HTTP 接口

| 方法与路径 | 返回 | 含义 |
| --- | --- | --- |
| GET `/api/v1/health` | 200 `HealthResponse` | 进程可以响应；没有外部依赖就绪检查 |
| GET `/api/v1/openapi.json` | 200 OpenAPI | 仅列出已实现的健康检查操作 |

服务默认地址为 `127.0.0.1:8007`。其他路径返回 404；健康路径的不支持方法返回 405 并保留 `Allow`。不启用隐式尾斜杠跳转。P0 不提供 202 命令接受接口，也不提供计算或模型的模拟成功接口。

每次 HTTP 请求生成新的 `X-Request-ID`；P0 不把客户端传入的请求 ID 作为可信标识。错误封套中的 `request_id` 与响应头一致。请求 ID 不承担幂等键作用。

## 2 类型和范围

代码源为 [v1.py](../../packages/contracts/src/platform_contracts/v1.py)，可执行样例为 [examples.json](../../tests/contracts/examples.json)。样例中的算子、资产与包身份仅用于契约测试，并未被平台登记。

| 类型 | 必填内容 | 约束与含义 |
| --- | --- | --- |
| `TaskRef` | `task_id`, `spec_revision` | 项目内引用，项目由外层资源或可信上下文确定；不是独立跨项目地址 |
| `AssetRef` | `project_id`, `asset_id`, `version_id`, `version_manifest_digest` | 精确资产版本；摘要指版本 manifest，不能当作某个数据文件的摘要 |
| `PackageRef` | `package_id`, `version_id`, `manifest_digest` | 冻结包引用；项目范围由外层上下文确定 |
| `ExecutionRef` | `execution_id` | 命令结果需要的最小项目内身份，不含执行状态 |
| `ComputationTarget` | 下表中的封闭联合 | 区分算子、工作流包和代码包 |
| `CommandReceipt` | 命令身份、状态、任务引用、查询路径、请求 ID | 命令投影，不表示科学计算成功 |
| `ErrorEnvelope` | 错误码、消息、请求 ID、重试方式 | 可选字段位置和相关引用 |

普通身份为 1–128 位 ASCII 字符，以字母或数字开头，其余只允许字母、数字、`_`、`-`。版本身份还允许 `.`，以支持 `1.0.0`，但拒绝大小写任意组合的 `latest`。身份不接受文件路径、服务 URL、空白或尾部换行。

修订号必须是大于等于 1 的整数，拒绝字符串 `"3"`、布尔值、浮点 `3.0` 和非有限数值。摘要必须为 `sha256:` 后接 64 位小写十六进制字符。校验摘要格式不等于验证真实字节；内容校验由后续 Data／Execution 阶段实现。

所有 DTO 拒绝未知字段和自动类型转换。DTO 禁止直接重新赋值；其内部列表并不因此成为持久不可变存储。P0 不实现对象存在性、访问权限、版本登记或摘要真实性验证。

## 3 计算目标

| `kind` | 其他必填字段 | 绑定 |
| --- | --- | --- |
| `OPERATOR` | `operator_id`, `version_id`, `execution_binding` | `LOCAL` 必须有 `implementation_id`；`REMOTE` 必须有 `service_binding_id` 和正整数 `binding_revision` |
| `WORKFLOW` | `package_ref` | 使用 `PackageRef` |
| `CODE_PACKAGE` | `package_ref` | 使用 `PackageRef` |

本地绑定不能带远程字段，远程绑定不能带本地实现字段。目标不能夹带 `url`、`path`、镜像、命令或源码。此处定义类型不表示 P0 已经具备本地执行、远程执行、CWL 或代码运行能力。

```python
from pydantic import TypeAdapter
from platform_contracts.v1 import ComputationTarget

target = TypeAdapter(ComputationTarget).validate_python(
    {
        "kind": "OPERATOR",
        "operator_id": "linear-trend",
        "version_id": "1.0.0",
        "execution_binding": {
            "kind": "LOCAL",
            "implementation_id": "native-trend-1",
        },
    }
)
```

## 4 命令回执

必填：`command_id`、`status`、`task_ref`、`status_url`、`request_id`。可选：`action_id`、`result`、`error`，默认为 `null`。

| 状态 | P0 结构约束 |
| --- | --- |
| `RECORDED` / `DISPATCHING` / `RECONCILING` | 不携带 `result` 或 `error`；不能编造执行身份 |
| `RESOLVED` | 可携带 `ExecutionRef`，不得携带 `error`；无需执行关联的命令允许空结果 |
| `REJECTED` | 必须有 `ErrorEnvelope`，不得同时有 `result` |

`status_url` 必须形如 `/api/v1/projects/{project_id}/commands/{command_id}`，末尾命令身份必须与 `command_id` 一致。它是相对资源路径，不接受任意外部 URL。P0 中该命令查询路由尚未实现。

`RESOLVED` 只说明命令结果已确定；它不说明 Execution 结束、资产发布、验收通过或 Task 完成。后续命令若需要其他结果类型，应先扩展明确契约并补充兼容性测试，不引入无约束的任意结果字典。

## 5 错误封套

实际 404 示例：

```json
{
  "code": "RESOURCE_NOT_FOUND",
  "message": "Resource not found",
  "request_id": "request-example",
  "retry_mode": "NONE",
  "locations": [],
  "related_refs": []
}
```

P0 的 HTTP 处理器覆盖：404 `RESOURCE_NOT_FOUND`、405 `METHOD_NOT_ALLOWED`、请求校验失败时的 400 `MALFORMED_REQUEST`、未处理异常的 500 `INTERNAL_ERROR`。当前健康检查没有业务请求体，400 路径通过仅在测试中添加的输入端点验证。错误响应不回显原始请求、框架错误详情、堆栈或宿主机路径；内部日志保留诊断与请求 ID。

首批错误枚举还包含目标架构规定的 `AUTHENTICATION_REQUIRED`、`SCOPE_DENIED`、`REVISION_CONFLICT`、`IDEMPOTENCY_CONFLICT`、`PRECONDITION_UNRESOLVED`、`BUDGET_BLOCKED`、`EVENT_CURSOR_EXPIRED`、`INVALID_BINDING`、`UNSUPPORTED_TARGET`、`RATE_LIMITED`、`DEPENDENCY_UNAVAILABLE`。定义枚举不表示相关业务处理器已实现；需要业务语义才能选择的错误映射留待对应阶段。

`retry_mode` 为 `NONE`、`SAME_KEY` 或 `AFTER_STATE_CHANGE`。P0 实际 HTTP 错误均为 `NONE`；其他值已有契约测试，但没有自动重试机制。客户端收到传输超时后不能凭 P0 健康状态推断后续业务命令是否已被接受。

`locations` 最多 32 项，每项 `path` 最多 16 段，内容是字段身份或整数位置；不能放提交值、文件路径或秘密。`related_refs` 最多 16 项，目前接受 `TaskRef` 或 `AssetRef`。消息与位置由受信任适配层构造；Schema 本身不能识别任意字符串是否包含秘密。

## 6 Schema 与兼容性

```bash
uv run --locked python -m platform_contracts.schema
```

输出一个包含 `contract_version` 和六个具名 Schema 的 JSON 对象；每个 Schema 自包含所需 `$defs`。无需启动 API，也无需访问 V6。JSON Schema 不通过虚构业务端点塞入 OpenAPI。

P0 同时验证运行时契约与导出的 Schema，包括未知字段、联合类型分支、固定版本、摘要和换行边界。两种校验有明确边界：JSON Schema 将 `3.0` 视为整数，而运行时要求真实整数；回执字段间关系（例如路径末尾身份一致）还必须执行 Python 校验。外部 Schema 校验不能替代服务端验证。

`v1` 是当前初始线格式，不代表所有未来业务协议已冻结。任何字段变更先更新契约、样例和反例，再由实际提供该能力的阶段接入；不得因为一个 DTO 可校验就对外宣称服务可用。
