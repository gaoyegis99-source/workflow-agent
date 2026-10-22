# A2 / 0.14.1 部署与回退

## 升级

1. 停止 Platform API 和 Platform Worker，导出待恢复的 A1 草稿并备份 Platform 数据库。
2. 安装完整 `0.14.1` wheel 集和 Capability Studio 静态资源。
3. 显式执行 Platform 迁移到 `0020_authoring_validation`；Data、Execution、Knowledge schema 不变。
4. 配置与 release lock 一致的 `V7_CWL_IMAGE` 固定摘要，核对 Operator、Checker、Tool Catalog 和镜像。
5. 启动 API、`platform-authoring-worker` 与其他 Worker，运行 `bash scripts/check_authoring.sh --phase editor`。

启动不会自动迁移。没有固定 CWL 镜像时，草稿和预览仍可读取，但正式校验明确返回依赖不可用；生产部署不得省略该镜像。
正式校验容器固定为 1 CPU、512 MiB、64 个进程，禁用网络，使用只读根目录，删除全部 Linux capability，并启用 `no-new-privileges`。这些限制属于受信校验环境，不能由请求覆盖。

## 恢复语义

正式校验冻结创建时的 source 和语义环境。Worker 崩溃后，租约到期由新 generation 接管；旧 generation 的迟到结果被拒绝。校验失败可以使用新幂等键创建新 attempt，历史记录不改写。

## 回退

优先前向修复。回退前停止 authoring 写入并归档 `authoring_validations`。只有确认升级后没有必须保留的新校验记录，且数据库备份与 `0.14.0` 全套发行物匹配时，才恢复升级前备份。不要在运行中混用 `0.14.0` Worker 与 `0.14.1` schema、前端或目录。
