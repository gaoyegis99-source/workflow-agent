# Capability Studio 构建与部署

## 构建

Capability Studio 使用 Node `>=22.12.0`，依赖由
`apps/capability-studio/package-lock.json` 精确锁定。统一构建入口为：

```bash
../runtime/toolchain/v7/bin/python scripts/build/build_studio.py
V7_STUDIO_DIST=../runtime/work/capability-studio \
  ../runtime/toolchain/v7/bin/python scripts/checks/check_studio_distribution.py
```

构建结果只写入 `runtime/work/capability-studio` 或显式的 `--output`，不会回写 Python 源码。
API wheel 的 Hatch 构建钩子拒绝缺失资源、软链接和 sourcemap，并把已验证目录装入
`platform_api/studio_static`。分发检查会比较 wheel 与外部目录的完整文件集合及 SHA-256。

## 部署

```text
构建 Studio 与 wheel / 镜像
→ 使用显式 artifact 目录和镜像配置新的 Direct Compose 实例
→ 运行 schema migration 与 readiness
→ 核对三个 Catalog、Studio 页面和业务 smoke
→ 切换入口
```

生产镜像构建上下文必须显式提供同一份目录为 `studio/`。镜像将其复制到
`/opt/v7-studio`，设置 `V7_STUDIO_ROOT`，并在安装 workspace wheel 时再次检查入口、资源、
软链接和 sourcemap。Direct Compose 启动时缺少该目录会失败关闭。

## 运行边界

- `/` 与 `/projects/*` 由同一 Studio bundle 提供，Bearer Token 只保存在页面内存中。
- 浏览器刷新后必须重新输入 Token；Token 不写 URL、localStorage 或 sessionStorage。
- A1 草稿不需要 Data、Execution、Knowledge 或 Docker 服务。
- 旧 `/ui/*` 静态路径不存在。

## 回退

V7 不再执行 Release-Lock 自动回退。失败实例保持停止，使用已经验证的冷备在隔离目录创建新实例，
重新完成 readiness、Studio 页面、任务与产物验证后再切换入口；不得让两个实例同时写同一状态目录。
