# Groundwater Platform V7

V7 是独立的企业平台源码树。本仓库只保留当前实现、当前契约、已接受决策和可执行运维说明；
历史验证报告、旧 Release Lock、发布指针、迁移快照、候选构建和生成前端包不属于当前检索语料。

V7 不加载 V5/V6 的源码、Python 包、Docker socket、卷、数据库或运行目录。跨产品输入必须经过
版本化导入契约和重新授权，不能通过工作区路径或共享可写状态隐式接入。

## 当前权威边界

- `apps/`：Platform、Data、Execution、Knowledge、Agent 与 Capability Studio。
- `packages/`：领域核心、基础设施、契约、算子、检查器和 Temporal 工作流运行时。
- `runtimes/`：受维护的执行引擎声明。
- `deploy/`：当前部署配置与镜像定义。
- `scripts/`：构建、检查、配置和运维入口。
- `tests/`：当前行为和边界回归。
- `docs/architecture`、`docs/capabilities`、`docs/contracts`、`docs/decisions`、
  `docs/operations`：当前规范。

`docs/verification/`、`archive/`、`evidence/` 和源码内生成的 `studio_static/` 被明确排除；
验收结果应由 CI 或外部制品系统保存，不回写到源码检索上下文。

## 安装与检查

要求 Python 3.12、uv >= 0.8.14、Node.js >= 22.12 和 Docker Engine/Compose。

```bash
UV_PROJECT_ENVIRONMENT=../runtime/toolchain/v7 uv sync --locked
../bin/ops status
../bin/ops check fast --execute
```

按能力运行回归：

```bash
bash scripts/checks/check_p8.sh --phase upgrade
bash scripts/checks/check_p7.sh --phase compatibility
bash scripts/checks/check_p6.sh --phase all
bash scripts/checks/check_p5.sh --phase all
bash scripts/checks/check_p4.sh --phase cartography
bash scripts/checks/check_authoring.sh --phase release-library
```

测试、Lint 和基准输出写入临时目录或 `runtime/work/`，不得写入 `docs/`。

## 前端

Capability Studio 源码位于 `apps/capability-studio/`。生成包不是源码；构建时应输出到部署制品目录：

```bash
../runtime/toolchain/v7/bin/python scripts/build/build_studio.py
V7_STUDIO_DIST=../runtime/work/capability-studio \
  ../runtime/toolchain/v7/bin/python scripts/checks/check_studio_distribution.py
```

第一条命令按锁文件安装依赖并完成类型检查、测试和外部目录构建；第二条命令构建
`groundwater-platform-api` wheel 并逐文件核对其中的 Studio 包。正式镜像的构建上下文还必须把
同一目录命名为 `studio/`；`deploy/production/app.Dockerfile` 会显式复制并校验它，不能读取仓库中
的旧静态包。

## Direct Compose

新实例只由明确输入创建，不按标签、“最新目录”或历史指针猜测：

```bash
cd ..
bin/configure-p8 \
  --instance production \
  --artifact-dir runtime/active/artifacts/production \
  --app-image groundwater-v7-app:production \
  --operator-image sha256:<operator> \
  --checker-image sha256:<checker> \
  --gdal-checker-image sha256:<gdal-checker> \
  --spatial-qgis-image sha256:<qgis> \
  --spatial-native-image sha256:<native> \
  --tabular-image sha256:<tabular> \
  --cwl-image sha256:<cwl> \
  --gateway-bind 127.0.0.1 \
  --gateway-port 18080
bin/p8 preflight runtime/p8/instances/production/compose.env
```

当前部署流程见 [P8 Direct Compose](docs/operations/p8-unified-deployment.md)，工作区边界见
[项目生命周期](docs/operations/project-lifecycle.md)和[依赖边界](docs/operations/workspace-dependencies.md)。

## 关键设计

- [产品线边界](docs/architecture/product-line-boundary.md)
- [模块与接口地图](docs/architecture/module-logic-and-interface-map.md)
- [身份与维护边界](docs/architecture/identity-and-maintenance-boundary.md)
- [Temporal 通用工作流决策](docs/decisions/p9-temporal-general-workflows.md)
- [维护命令目录](docs/maintenance/command-directory.md)
