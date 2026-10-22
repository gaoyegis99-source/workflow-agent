# P8.2 线上整改报告（历史记录）

> **HISTORICAL / SUPERSEDED。** 本报告记录旧的 `0.15.1` 运行态整改，已由 [P8.2 / 0.15.3 修复与发布报告](p82-0153-remediation-report.md) 取代。当前在线身份以 [p82-current-release.json](p82-current-release.json) 为准；本页不再代表当前生产 Release。

**历史结论：当时的六项运行态问题已修复并部署，Capability Studio 已完成工作台式 UI 升级。** 当时生产实例使用 `.p8-runtime/instances/production-final/p8.env`，局域网 gateway 为 `192.168.167.87:18080`，当时 Release Lock 为 `sha256:45bac97794b4342f1b236e98337b85a6c88ee28e707d0d0d0ed81e5258929a8e`。当前机器可读身份见 [p82-current-release.json](p82-current-release.json)。

## 整改内容

1. 以新不可变 Release 启动默认生产实例，停止 `18120`、`18140` 两个历史候选。原实例持久目录保留，未删除数据库数据。
2. Platform Worker 空闲轮询改为 `0.2s` 起步、指数退避、`5s` 封顶；处理到工作后立即重置。生产入口把 `httpx` 成功请求日志降到 `WARNING`。
3. README 主流程改为 `build_p8_release.sh`、`configure_p8.py`、`p8.compose.yaml` 和端口 `18080`，并要求使用构建输出的完整 Release 目录。
4. Capability Studio 生产构建关闭 source map；发布锁和运行时校验不再对 `.map` 作特殊排除，因此任何意外出现的 map 也必须被 Release Lock 锁定。
5. gateway 不再转发上游 `date`、`server`，由外层 Uvicorn 各生成一份。
6. `/api/v1/health` 更新为 `phase=P8.2`，OpenAPI 描述更新为 `P8.2 / A7`。
## 附带 UI 变更

Capability Studio 从基础表单布局升级为响应式研究工作台：增加品牌主视觉、连接状态、模块导航、运行统计和分层工作区，并保留原有 API 与自动化测试契约。该项是 UI 变更，不计入上面的六项运行态问题。

## 验证结果

- Capability Studio：7 项前端测试、TypeScript 检查和生产构建通过；A1/A5/A7 浏览器与 HTTP 回归 5 项通过；桌面与移动端均无横向溢出。生产目录只有 `index.html`、JS、CSS 三个文件。
- 聚焦后端回归：41 项通过；包括 Worker 退避/重置、gateway 响应头、P8.2 元数据、source map 404 和 Release 资产一致性。
- P8 升级/回滚门禁：19 项通过。
- 运行态：当时 health、ready、OpenAPI 均为 HTTP 200 且响应摘要与当时 Release 相同；source map 为 404；`date`、`server` 各一份。
- 空闲 Platform Worker 的三次 CPU 抽样为 `1.03% / 0.00% / 0.00%`，连续运行超过一分钟后，最近 60 秒没有 `httpx` 成功请求日志。稳定空闲时远程 lifecycle claim 最多每 5 秒一次，即每分钟约 12 次。

## 局域网入口

gateway 现绑定 `0.0.0.0:18080`，可从受信局域网通过 `http://192.168.167.87:18080/` 访问；本机 `127.0.0.1:18080` 仍可用。Execution `18082` 和 PostgreSQL `55450` 继续只监听 `127.0.0.1`。业务 API 的匿名请求仍返回 `401`，生产 source map 仍返回 `404`。UFW 配置为禁用状态；如上游网络设备另有访问控制，需要允许受信子网 `192.168.167.0/24` 访问 TCP `18080`。

原 [P8.2 发布报告](p82-release-report.md)和结构化证据继续保留，作为维护窗口升级/N-1 恢复的原始验收记录；其中的 `sha256:360216…` 不再代表当前在线实例。
