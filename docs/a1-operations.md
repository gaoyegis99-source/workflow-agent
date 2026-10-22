# A1 / 0.14.0 部署与回退

## 构建

Capability Studio 使用 Node `>=22.12.0`，依赖由
`apps/capability-studio/package-lock.json` 精确锁定：

```bash
npm --prefix apps/capability-studio ci
npm --prefix apps/capability-studio run typecheck
npm --prefix apps/capability-studio test
npm --prefix apps/capability-studio run build
uv build --all-packages --wheel --out-dir <wheel-dir>
```

前端构建结果进入 `groundwater-platform-api` wheel 的 `platform_api/studio_static`。生产
release lock 必须包含 11 个 `0.14.0` wheel 的摘要和所有非 sourcemap Studio 资源摘要。
缺少 wheel 目录、wheel 集不完整、前端资源缺失或资源摘要变化都会拒绝生成或启动。

## 升级

```text
等待在途 Agent、Acceptance、Execution 和发布收敛
→ 停止 Platform API 与 Platform Worker
→ 安装完整 0.14.0 wheel 集
→ platform-admin migrate
→ platform-admin check-schema
→ 核对 release lock、三个 Catalog 和 Studio bundle
→ 启动服务
→ bash scripts/check_authoring.sh --phase drafts
```

Platform schema 从 `0018_agent_acceptance_identity` 升为 `0019_authoring_drafts`。Data、
Execution 和 Knowledge schema 不变。启动仍不会自动迁移。

## 运行边界

- `/studio/` 与 API 同源，Bearer Token 只保存在页面内存中。
- 浏览器刷新后必须重新输入 Token；Token 不写 URL、localStorage 或 sessionStorage。
- A1 草稿不需要 Data、Execution、Knowledge 或 Docker 服务。
- 原 `/` 与 `/ui` 任务页保持可用。

## 回退

先停止 `0.14.0` 进程并确认不再写入草稿。导出需要保留的定义与修订后，执行 Alembic
downgrade 到 `0018_agent_acceptance_identity`，再安装完整 `0.13.0` wheel、目录、镜像和
release lock。降级只删除 A1 草稿表，不修改任务、执行、预算、知识或成果。
