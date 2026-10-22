# P8.2 / 0.15.2 统一企业工作区发布报告（历史记录）

> **HISTORICAL / SUPERSEDED。** 本报告记录 `0.15.2` 的 UI 发布，当前在线身份以 [p82-current-release.json](p82-current-release.json) 为准；最新修复与发布见 [P8.2 / 0.15.3 修复与发布报告](p82-0153-remediation-report.md)。

日期：2026-10-17

## 结论

V7 统一企业数据平台 UI 已构建为不可变 `0.15.2` Release，并通过维护窗口从 `0.15.1` 切换到生产实例 `production-final`。当时 LAN 入口为 `http://192.168.167.87:18080`，只公开 gateway。

- Release digest：`sha256:598c857ba69ddb285df96581f3e49627076e78a89818d6a609ee7affb2fc2f49`
- 应用镜像：`sha256:1304df32d9213643575ed42da3c487b9e5fe9be6842d8b13e03466383f50b560`
- 源码摘要：`sha256:5a97b780725204bbeea91661525f590b17e3e43179c54c34689c40e8aca6b730`
- Release 目录：`.p8-runtime/releases/0.15.2-598c857ba69ddb285df96581f3e49627076e78a89818d6a609ee7affb2fc2f49`
- 升级操作：`.p8-runtime/upgrades/v0152-ui-20261017`，最终状态 `COMMITTED`
- N-1：`sha256:45bac97794b4342f1b236e98337b85a6c88ee28e707d0d0ed81e5258929a8e`

当时权威运行指针见 [p82-current-release.json](p82-current-release.json)；该指针随后已更新。

## 验证结果

- 前端 TypeScript、11 项 Vitest、生产构建通过；入口 gzip 为 116.18 KiB，React Flow、Monaco 和 YAML Worker 为独立懒加载块，生产发布物没有 `.map`。
- A1–A7 作者工作台 120 项测试通过。
- 初次全量回归中的 11 个失败项全部定向复跑通过，包括 OpenAPI 路由、Compose 部署、P3/P4/P4.2/P4.2.2/P6.8 浏览器闭环。
- 浏览器覆盖连接与刷新清空令牌、上传与语义修订、DIRECT/AGENT 任务、授权、运行、工作流节点、工作流端口交付、验收、下载和可信静态地图预览。
- 新 `workspace-summary` 与 `executions` 接口通过 VIEWER 鉴权、完整零计数、依赖不可用、筛选、稳定分页和内部字段脱敏测试。
- 1280、1440、1920 无横向溢出；1099 只显示桌面浏览器提示。
- 当时生产根入口与 LAN 入口均返回 200；Release Header 与本报告 digest 一致；健康接口返回 `phase=P8.2`，OpenAPI 返回 `version=0.15.2` 与 A7 描述。
- `/studio/`、`/ui/` 和未知路径返回 404；入口资源的 `.map` 返回 404。
- CSP 允许同源 blob Worker，禁止外部脚本、对象和嵌入；响应仅各有一份 `date` 与 `server`。
- 所有 API、Worker、gateway 与 PostgreSQL 容器正常；空闲 `platform-worker` 采样为 0.00% CPU，最近一分钟没有 claim/HTTP 成功轮询日志。

## 升级与恢复证据

升级计划确认 API 契约、四个数据库 schema 与备份格式兼容。停车时活动 Action、Execution、Agent、导入、发布和 outbox 均为 0。冷备份清单摘要为 `sha256:8eb54ab9f8a63c1c726007aaa13b6ce6a39f38ff6b1309d0cf80341f69ba967d`，已在提交前验证。上一 `0.15.1` Release、源环境快照和冷备份均保留，不删除历史发布物。
