# P5.3 运行、升级与回退

当前补丁版本为 `0.6.3`。先等待活动检查、执行和发布收敛，再停止 Platform API、Platform Worker 与 Knowledge 服务。安装九个发行包，依次显式升级 Platform 到 `0008_open_acceptance`、Knowledge 到 `0002_lifecycle_outbox`；Data 保持 `0004_document_assets`，Execution 保持 `0006_remote_reliability`。`0.6.2→0.6.3` 没有数据库迁移，启动前仍需执行各服务 schema check。

固定 checker 镜像通过 `bash scripts/build_p3_images.sh` 构建，生产标签为
`groundwater-v7-checker:0.6.3`。开放检查与 P3 Decimal 检查共享受限镜像，但使用不同的实现 ID 和协议。容器无网络、只读根目录、删除 capabilities，并限制 CPU、内存和进程数。镜像的版本和源码摘要 OCI 标签必须与 [P5-R0 结构化证据](verification/p5-r0-release-evidence.json)一致。

Platform Worker 配置 `V7_KNOWLEDGE_URL` 和服务凭据后领取 Knowledge 生命周期 outbox。事件在 Platform inbox 去重，并按文档版本的生命周期修订排序。Knowledge ack 响应丢失时会重投；Platform 不会重复追加失效原因。
消费者兼容 P5.1 历史记录使用的 `IMPORTED` 事件名；新导入统一产生 `CREATED`。升级后应先让积压的创建事件被确认，再核对后续 `WITHDRAWN` 已推进到 Platform lifecycle cursor。

统一验收入口：

```bash
bash scripts/check_p5.sh --phase all
```

定向检查可用：

```bash
uv run --locked pytest -q tests/p53
uv run --locked python scripts/check_p53_checker.py
```

回退到 `0.6.2` 不需要数据库降级，但必须先停止新验收并让活动 checker 收敛，再恢复九个 `0.6.2` 包和对应 checker 镜像。继续回退到 `0.6.1` 时，代码回退需要先将 Knowledge 降到
`0001_knowledge`、Platform 降到 `0007_research_policy`。降级只删除 P5.3 计划、评审、有效性投影和 outbox 租约字段；已有任务、成果、P3 报告、研究协议和 Knowledge 正文不重写。
