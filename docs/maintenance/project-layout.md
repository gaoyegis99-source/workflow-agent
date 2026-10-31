# V7 项目布局契约

V7 的 Git 源码根固定为 `groundwater-v7/source/`。项目一级目录只承载机器可读布局、
稳定 `bin/` 入口和本地运行态；不要把源码扁平化到项目一级，也不要在两层之间建立软链接。

```text
groundwater-v7/
├── source/
│   ├── apps/ packages/ runtimes/    # 产品代码与 workspace 成员
│   ├── deploy/                      # 当前部署声明
│   ├── scripts/                     # 分类维护命令
│   ├── tests/                       # 当前行为与边界回归
│   └── docs/                        # 当前规范、决策与运维说明
├── runtime/
│   ├── work/                        # 可丢弃、可再生的构建和测试产物
│   ├── active/                      # 显式激活的运行制品
│   ├── archive/                     # 本地短期隔离区，不进入检索或发布输入
│   ├── toolchain/v7/                # 从锁文件重建的 Python 环境
│   ├── docker/                      # V7 rootless Docker 数据根
│   └── p8/                          # Direct Compose 实例、冷备与本地日志
└── bin/                             # 稳定运维入口
```

`project-layout.json` 是机器可读契约。`scripts/maintenance/check_internal_layout.py`
检查目录、布局版本、旧源码内 runtime 和越界软链接。源码中不得保留 `archive/`、
`evidence/`、`docs/verification/` 或生成的 `studio_static/`；它们已由 `.gitignore` 与
`.ignore` 同时排除。测试报告写到临时目录或 `runtime/work/`，长期审计材料交给外部制品系统。

## Direct Compose

`runtime/p8/` 是本地运行态，不是 Git 源码，也不是部署输入发现机制。新实例只能由
`bin/configure-p8` 使用明确的 artifact 目录、镜像身份和网络参数创建；不得从目录名、
镜像标签、旧清单或候选制品推断输入。所有 Docker 调用都经项目 `bin/docker` 和 rootless
socket，不能回退到宿主 socket。

当前没有配置好的 P8 实例，因此需要实例的发布级检查应报告 `BLOCKED`，不能伪造状态。

## 干净克隆恢复

在源码根执行：

```bash
python3 -B scripts/maintenance/bootstrap_project.py --plan
python3 -B scripts/maintenance/bootstrap_project.py --execute
python3 -B scripts/maintenance/bootstrap_project.py --check
UV_PROJECT_ENVIRONMENT=../runtime/toolchain/v7 UV_LINK_MODE=copy uv sync --locked
```

引导程序只创建缺失的项目入口和布局；不同内容会报冲突，不覆盖实例、Docker 数据、工具链或
发布状态。源码检查使用 `check_internal_layout.py --scope source`；包含运行态的检查使用
`--scope all`。
