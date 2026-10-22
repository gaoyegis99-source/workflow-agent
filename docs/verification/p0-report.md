# P0 阶段报告

> **HISTORICAL.** 本报告是该阶段的冻结实现或验收记录，不代表当前活动 Release 或当前实现状态。当前发行身份见 [当前发布指针](p82-current-release.json)。

完成日期：2026-10-12。结论：**P0 通过，本轮结束。P1 尚未开始。**

## 完成内容

- 建立独立 uv 工作区，固定 Python 3.12.8，生成并验证 `uv.lock`；运行依赖与测试依赖分别声明。
- 建立三个可独立构建的发行包：`groundwater-platform-api`、`groundwater-platform-core`、`groundwater-platform-contracts`。
- 实现 `platform-api` 启动命令、应用工厂、`GET /api/v1/health` 和 `GET /api/v1/openapi.json`。
- 实现 `TaskRef`、`AssetRef`、计算目标、命令回执与错误封套，包含支撑它们的包引用和执行引用；提供代码生成 JSON Schema 的入口。
- 实现服务端请求 ID、404／405／400／500 错误封套与响应脱敏。未来业务路由返回 404，没有模拟成功接口。
- 建立领域／应用／HTTP 依赖方向检查、契约合法与非法样例、HTTP 错误测试及仓库外构建安装验证。
- 提供 [启动说明](../../README.md)、[契约说明](../contracts/p0-contracts.md)、[阶段路线](../implementation-plan.md) 和统一检查入口。

核心包只有标准库依赖；API 通过应用查询访问核心，领域代码不依赖框架、SDK 或 wire DTO。尚无实际外部依赖，未创建空的持久化、模型或 CWL 端口。

## 测试结果

环境：Linux，Python 3.12.8，uv 0.8.14。最终执行统一入口 `bash scripts/check_p0.sh`，退出码 **0**。

| 命令或检查 | 最终结果 |
| --- | --- |
| `uv sync --locked` | 成功，锁文件与工程声明一致 |
| `uv run --locked ruff check .` | 通过 |
| `uv run --locked ruff format --check .` | 通过 |
| `uv run --locked pytest -q` | **100 passed，0 failed**；1 条第三方弃用警告 |
| `uv run --locked python scripts/verify_distribution.py` | 通过以下四组发行与运行检查 |
| 构建 | 生成 3 个 wheel 和 3 个 sdist |
| 核心包单独安装 | 全新虚拟环境只安装核心 wheel，应用查询成功，环境无 FastAPI／Pydantic |
| API 独立安装 | 仓库外临时虚拟环境安装带哈希的锁定运行依赖及 wheel，依赖完整性检查通过，模块来源均在该环境内 |
| 真实进程检查 | 安装后的 CLI 成功启动，真实 HTTP 健康检查／OpenAPI／404 通过，SIGTERM 后确认应用完成关闭 |
| `uv run --locked python -m platform_contracts.schema` | 成功输出版本 1 的六个具名 Schema，并单独解析验证为 JSON |

测试包含字符串／布尔值冒充修订号、非有限数值、未知字段、非法身份与摘要、`latest`、绑定混填、包路径注入、回执状态／结果冲突、错误字段脱敏、错误请求后的健康恢复、工厂实例隔离及依赖边界反例。测试专用输入／故障路由只在测试中注册，不出现在发行 API 中。

构建检查清除了子进程的 `PYTHONPATH`、`PYTHONHOME` 等覆盖项，以非 editable wheel 从仓库外启动。临时环境和服务在检查结束后清理，没有留下后台服务器。

未运行真实模型、数据库、CWL、算子、科学数值、长任务恢复、发布竞争或性能测试：这些功能均不在 P0 范围，当前未实现，因此不能声称它们已通过验证。其他操作系统与 Python 次版本也未验收。

## 问题处理

1. **标识符尾部换行的 Schema 差异。** 首轮发现 Python 契约拒绝该输入，但部分 JSON Schema 正则判定会接受。改用两端一致的绝对结尾约束，并增加版本、摘要、命令查询路径的换行反例，最终全部通过。
2. **关闭验证错误假设。** 初始发行验证要求 SIGTERM 后退出码必须为 0。核对所安装 Uvicorn 的信号处理代码后，确认它会完成 lifespan 关闭再重新触发原信号。验证改为同时检查允许的退出状态和两个关闭完成日志，不将单纯进程退出当作成功。复验通过。
3. **代码与文档示例格式。** 修复静态检查发现的测试参数声明与文档代码块格式问题，统一入口通过。

没有影响 P0 验收的未解决缺口。保留一项非阻断记录：锁定的 Starlette 测试客户端使用了 AnyIO 的弃用别名，pytest 发出 1 条 `DeprecationWarning`。未屏蔽警告；它来自测试依赖调用，不影响本轮运行结果，后续依赖升级时复核。

## 范围和限制

P0 当前可以安装、启动、报告进程存活、导出接口描述与严格校验首批数据形状。它不能创建持久任务、接受计算、验证引用对象真实存在、核实文件摘要、发布资产或运行 Agent。

JSON Schema 不能完整表达本阶段的运行时约束：例如 `3.0` 的严格整数判定，以及回执 URL 与命令身份之间的关系，仍须执行 Python 服务端校验。字段身份合格也不构成授权。

构建／运行入口与锁文件没有 V6 路径或依赖。本轮未写入 V6 的代码、部署与运行数据；只读核对的 V6 提交前后均为 `839c4f5aa8edb69bcd180bf24b45ead7dd20421b`。旧架构文档中的 V6 链接是设计溯源材料，不是运行依赖。

下一轮范围是 P1 执行组件验证，仍需逐项实验后形成选型记录。本轮没有安装 Toil／Weaver，没有预先构建控制核心、数据服务、知识服务或模型适配。首个可用业务闭环仍安排在 P3。
