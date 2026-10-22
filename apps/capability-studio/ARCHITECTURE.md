# Capability Studio 前端结构

前端按职责分为四层：

```text
src/
├── app/                         浏览器入口、路由组合与应用外壳
│   ├── App.tsx                  路由组合根
│   ├── bootstrap.tsx            浏览器入口与全局 Provider
│   ├── connection.tsx           内存令牌连接页
│   └── shell.tsx                侧栏、顶栏与项目框架
├── core/                        跨功能运行时能力
│   ├── session.tsx              内存会话唯一所有者
│   └── support.tsx              项目摘要、权限与页面反馈组件
├── features/                    可独立加载的业务切片
│   ├── overview/
│   ├── assets/
│   ├── tasks/
│   ├── runs/
│   └── governance/              Release、算子及工作流治理
│       └── studio/              画布、源码模型与 Monaco 编辑器
├── shared/                      无业务页面依赖的公共基础层
│   ├── api.ts                   HTTP、错误和幂等请求
│   ├── types.ts                 平台共享领域类型
│   └── ui.tsx                   内部语义组件库
└── styles/                      平台与工作流样式
```

依赖方向固定为：

```text
bootstrap -> app -> features -> core -> shared
             └--------------------------> shared
```

边界规则：

- `App.tsx` 只组合路由，不保存业务状态或调用业务接口。
- `features/*` 之间不得直接导入；跨功能复用必须下沉到 `core/` 或 `shared/`。
- Bearer Token 和项目列表只属于 `SessionProvider`，不得复制到功能模块或浏览器持久存储。
- 服务端摘要是角色与 Release 身份的最终来源；会话项目角色只用于摘要加载前的界面回退。
- 每个功能路由独立懒加载；工作流画布和 Monaco 使用第二级懒加载。
- 权限按钮仅提供界面提示，服务端逐次鉴权仍是权威。
