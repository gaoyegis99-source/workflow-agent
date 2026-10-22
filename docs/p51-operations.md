# P5.1 安装、迁移与运行

P5.1 版本为 `0.6.0`。Platform、Data、Execution、Knowledge 使用独立数据库与角色；Knowledge 另有独立正文目录。启动不会自动迁移。

## 升级

等待活动导入、执行和发布收敛后停止 API 与 Worker，安装锁定发行包，然后显式迁移：

```bash
uv sync --locked
uv run platform-admin migrate
uv run data-admin migrate
uv run execution-admin migrate
uv run knowledge-admin migrate
uv run platform-admin check-schema
uv run data-admin check-schema
uv run execution-admin check-schema
uv run knowledge-admin check-schema
```

预期版本为 Platform `0006_service_binding_scope`、Data `0004_document_assets`、Execution `0006_remote_reliability`、Knowledge `0001_knowledge`。Data 的 `0004` 只增加文档队列索引，不改写已有上传、资产或发布记录。

## 配置与进程

Knowledge 至少需要：

```text
V7_KNOWLEDGE_DATABASE_URL=postgresql+psycopg://...
V7_KNOWLEDGE_STORAGE=/srv/v7/knowledge
V7_KNOWLEDGE_URL=http://knowledge:8013
V7_PLATFORM_KNOWLEDGE_TOKEN=<高熵服务令牌>
V7_KNOWLEDGE_DATA_TOKEN=<高熵服务令牌>
```

Data 的内部凭据允许 `knowledge` 身份读取精确资产描述和表示内容。Platform 使用单独凭据调用 Knowledge。不要在日志、配置样例或证据文件中记录明文令牌。

启动顺序：

```bash
uv run data-service
uv run knowledge-service
uv run data-document-worker
uv run knowledge-worker
uv run platform-api
```

表格 Worker 继续只领取 CSV/Parquet，空间 Worker 继续只领取 GeoPackage/GeoTIFF，文档 Worker 只领取 Markdown/TXT。Knowledge Worker 使用 30 秒租约和 generation fencing；进程重启后接管原导入。依赖暂时不可达时导入保持可恢复，永久编码或摘要错误才失败。

## 验收和回退

统一验收：

```bash
bash scripts/check_p5.sh --phase knowledge
```

该入口构建当前固定容器、运行完整 P0–P5.1 pytest、13 项 P1 规则和仓库外发行安装。定向开发检查可以运行 `uv run --locked pytest -q tests/p51`。

回退前停止新文档导入并等待在途导入收敛。回退 Data 时将 `0004_document_assets` 降到 `0003_spatial_import_runtime`；该操作只删除索引。Knowledge 是新增服务，可以停止但不得删除数据库和正文目录，否则历史精确引用将无法复核。P4 任务、执行、资产与验收记录不参与 Knowledge 迁移。

