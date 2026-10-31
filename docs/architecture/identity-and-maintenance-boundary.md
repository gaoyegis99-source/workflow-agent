# V7 身份与人工维护边界

V7 将“用户是谁”和“用户可在什么项目中做什么”分开处理。日常维护只需从项目根的
`bin/ops` 进入；它不会成为新的权限绕过层，也不会打印密钥、Token 或实例环境内容。

```text
浏览器 / CLI
  → token 或受信代理身份解析
  → 已存在 Principal
  → projects.members 中的 OWNER / EDITOR / VIEWER
  → 平台业务服务
```

## 两种用户身份模式

- `V7_IDENTITY_MODE=token` 是默认兼容模式。Platform API 继续验证 Bearer Token；Capability
  Studio 只把它保存在页面内存。
- `V7_IDENTITY_MODE=trusted-proxy` 适用于放在受信 TLS 边缘代理之后的部署。边缘代理使用不小于
  32 字符的 `V7_TRUSTED_PROXY_TOKEN` 证明自身，并提供 `X-Platform-Subject` 与
  `X-Platform-Display-Name`。Gateway 丢弃客户端提交的身份/角色头，只在验证代理证明后向私有
  Platform API 重建最小身份信封。

`X-Platform-Roles` 从不决定 V7 授权。受信代理 subject 必须事先由管理员绑定到一个已有
Principal；显示名不会用于自动匹配，也不会自动创建用户。项目成员表仍是唯一的角色事实来源。
管理员通过下列显式、审计友好的动作绑定 subject：

```bash
bin/ops identity bind \
  --principal-id PRINCIPAL_ID \
  --subject IDP_SUBJECT \
  --reviewer ADMIN_SUBJECT \
  --reason "approved identity-provider binding" \
  --execute
```

该操作在同一数据库事务中更新绑定并追加 `principal_identity_events` 审计记录；`--reviewer` 和
`--reason` 不能省略。创建 Principal 与绑定 subject 是两个独立步骤，避免按名称或临时显示信息
静默授予身份。

当前 schema head 包含 principal external subject 与 Worker lane lease。迁移只在 Direct Compose
维护窗口完成冷备与恢复预检后显式执行；源码整理不会连接或就地迁移任何实例数据库。

`GET /api/v1/me` 返回当前 Principal、认证模式及其服务端确认的项目角色。Capability Studio 在
受信代理模式下会用该接口建立内存会话，不再要求刷新后粘贴 Bearer Token；Token 模式的现有行为
不变。

## 保留的硬边界

这项整理没有移除服务间 Bearer Token、部署 revision/内部身份头校验、数据库迁移校验、成员角色复核、
冷备恢复验证或私有网络边界。它们保护的不是日常编辑摩擦，而是跨服务调用、部署身份与可恢复性。

当前没有配置好的 P8 实例。`bin/ops runtime ...`、`bin/ops check release` 与
`bin/ops release ...` 要求有效的 Direct Compose `compose.env`、项目 socket 和相应维护前置条件；
缺少这些条件会准确返回 `BLOCKED`，不读取旧发布指针或 Release Lock。
