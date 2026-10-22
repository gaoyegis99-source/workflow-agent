# P4.2.2 安装、升级和运行

## 固定环境

需要 Python 3.12、uv、PostgreSQL 17 和 Docker。QGIS 镜像固定 QGIS `3.44.15`、GDAL `3.8.4`、PROJ `9.4.0` 和 `fonts-noto-cjk=1:20230817+repack1-3`；Native 镜像仍不安装 QGIS SDK。镜像构建后以不可变 image ID 记录在 `.p4-runtime/spatial/images.json`。

```bash
uv sync --locked
bash scripts/build_p3_images.sh
bash scripts/build_p4_image.sh
bash scripts/build_p42_images.sh
```

容器使用非 root、无网络、只读根目录和输入、独立输出、能力删除、`no-new-privileges`、128 进程、1 CPU、2 GiB、180 秒。PNG 上限 20 MiB，成果集合上限 100 MiB。QGIS 只加载系统 Processing provider。

## 升级

等待活动执行和发布收敛，停止 API 和 Worker，安装 `0.4.1` 并重建固定镜像。没有新迁移，但仍须显式核验：

```bash
uv run --locked platform-admin check-schema
uv run --locked data-admin check-schema
uv run --locked execution-admin check-schema
```

预期为 Platform `0004_workflows`、Data `0003_spatial_import_runtime`、Execution `0004_execution_work_class`。启动不会自动迁移。继续使用 `bash scripts/run_p42.sh` 启动同一组服务和角色 Worker。

新制图执行冻结 Action、容器、镜像、模板、输入协议、资源和成果身份。Docker 暂时不可查询时保持 UNKNOWN；恢复不得换身份重算。取消必须等待容器和发布仲裁形成可核实事实。

## 验收与回退

```bash
bash scripts/check_p4.sh --phase cartography
bash scripts/check_p4.sh --phase spatial
bash scripts/check_p4.sh --phase workflows
bash scripts/check_p3.sh
```

制图入口执行格式检查、完整 pytest、真实 PostgreSQL/QGIS/Native/Toil/浏览器闭环、13 项 P1 规则和八个发行包的仓库外安装。测试只使用仓库内确定性合成空间数据。

回退到 `0.4.0` 前，所有制图执行必须离开运行和发布状态。既有地图资产是不可变普通资产，仍可查询和下载；旧版本不能恢复活动的制图执行。回退不需要数据库降级，也不能删除任务、预算、JobStore、节点账本或成果。
