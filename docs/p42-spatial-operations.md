# P4.2.1 安装、迁移与运行

## 固定环境

需要 Python 3.12、uv、PostgreSQL 17 和 Docker。QGIS 镜像固定 QGIS `3.44.15`、GDAL `3.8.4`、PROJ `9.4.0`；Native 镜像固定 GDAL/PROJ且不安装 QGIS Python SDK。构建输出保存在 `.p4-runtime/spatial/images.json`，运行时使用不可变 Docker image ID。

```bash
uv sync --locked
bash scripts/build_p3_images.sh
bash scripts/build_p4_image.sh
bash scripts/build_p42_images.sh
```

构建失败必须停止发布，不能把缺失的固定版本替换为浮动包版本。

## 升级

等待活动执行和发布收敛后停止 API 与全部 Worker，安装 `0.4.0`，再显式迁移：

```bash
uv run --locked platform-admin migrate
uv run --locked data-admin migrate
uv run --locked execution-admin migrate
uv run --locked platform-admin check-schema
uv run --locked data-admin check-schema
uv run --locked execution-admin check-schema
```

预期 schema：Platform `0004_workflows`、Data `0003_spatial_import_runtime`、Execution `0004_execution_work_class`。Data 的新迁移只给上传增加空间检查运行事实；旧资产、发布、任务、预算、节点和 JobStore 不重写。启动过程仍不会自动迁移。

在已经配置的 V7 环境中运行：

```bash
set -a
source .p3-runtime/local/p3.env
set +a
bash scripts/run_p42.sh
```

该入口核验三个 schema，读取固定空间 image ID，启动 Data/Execution/Platform、表格与空间导入 Worker、角色化 Execution Worker 和 Platform Worker。空间检查工作目录可用 `V7_DATA_SPATIAL_WORK_ROOT` 指定；它必须是 V7 独立持久目录。

## 安全与恢复

QGIS 和 Native 计算容器使用非 root、无网络、只读根目录、只读输入、独立输出、能力删除、`no-new-privileges`、进程和资源限制。QGIS 只加载系统 Processing provider；不加载用户插件目录。

空间导入和计算都在启动前保存稳定容器身份、输入摘要和固定镜像。Docker 查询暂时失败时保持 UNKNOWN 和预算占用。已经开始的容器消失且没有完整输出时明确失败；不会换 Action、容器名或成果身份重算。取消只有在 inspect 确认容器停止、发布中止完成后才收敛。

## 验收与回退

统一验收：

```bash
bash scripts/check_p4.sh --phase spatial
```

它构建固定镜像，执行格式和依赖边界检查、完整 pytest、13 项 P1 规则、真实 PostgreSQL/QGIS/Native/Toil/浏览器故障回归，以及八个包的仓库外安装闭环。测试数据全部来自仓库内确定性合成夹具，不代表真实地下水观测。

回退到 `0.3.8` 前先让空间导入和空间工作流进入终态，停止所有服务，然后恢复旧发行和镜像。旧版本不了解空间运行事实，不能接管活动空间执行。数据库回退只能删除 `uploads.runtime` 诊断/恢复字段，不得删除任务、资产、预算、发布、节点或 JobStore；导出需要的空间诊断事实后才能执行 Data downgrade。

