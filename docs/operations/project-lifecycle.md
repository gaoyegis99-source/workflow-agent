# V7 项目生命周期与工作树边界

V7 是独立维护的企业平台工作树。`apps/`、`packages/`、`runtimes/`、`deploy/`、
`scripts/`、`tests/`、当前文档和锁文件属于可审查 Git 基线。`experiments/p1/` 与
`runtimes/cwl/` 仍是现行验收入口的依赖。

以下内容不属于源码基线或检索上下文：

- 历史验证报告、Release Lock、发布指针和迁移快照；
- 候选发布包、生成的前端 bundle 与测试报告；
- `.venv/`、依赖缓存、浏览器产物和任何密钥配置；
- `runtime/` 下的本地工具链、构建输出、实例状态和 Docker 数据。

这些路径由 `.gitignore` 和 `.ignore` 双重排除。需要长期保存的验收与审计结果必须上传到
外部制品系统，并通过运行号或提交身份关联，不能回写源码目录。

## 运行态

`runtime/p8/` 只存放本机 Direct Compose 状态。当前没有已配置实例。创建实例时必须使用
`bin/configure-p8` 提供明确 artifact、不可变镜像身份和绑定地址；禁止扫描“最新”目录、
复用旧清单或把候选制品当成当前部署输入。

退役实例只能经 `bin/ops runtime retire-p8` 完成盘点、外部冷备、恢复演练、密钥托管确认、
隔离校验和显式清除。该流程不使用宿主 Docker、不做全局 prune，也不会自动创建替代实例。

## 门禁

在项目根运行：

```bash
runtime/toolchain/v7/bin/python source/scripts/maintenance/check_project_hygiene.py
runtime/toolchain/v7/bin/python source/scripts/maintenance/check_workspace_dependencies.py
runtime/toolchain/v7/bin/python source/scripts/maintenance/check_internal_layout.py --scope source
```

这些检查拒绝被跟踪的缓存、历史/候选材料、兄弟项目依赖、宿主 Docker socket、共享可写资源和
越界软链接。
