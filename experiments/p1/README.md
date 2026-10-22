# P1 执行组件验证

这里是独立的组件实验，不是 Execution 服务。它不被 Platform API、核心包或发行包导入。公开 API 仍只有 P0 健康检查和 OpenAPI；没有任务提交、数据库业务状态、算子迁移或模型接口。

## 运行

在 V7 根目录执行：

```bash
bash scripts/check_p1.sh --record docs/verification/p1-rootless-correction-evidence.json
```

该入口先完成 P0 回归与仓库外安装验证，再运行 13 项实验适配／选型单元测试，最后依次执行真实 Toil、Weaver、gVisor 验证。组件报告中保留失败项；总退出码由明确的阶段准入规则决定，并不要求两个候选都通过。不要设置 `PYTHONOPTIMIZE` 或使用 `python -O`。

要求 Linux x86_64、Python 3.12、uv，以及当前用户可访问的 Docker daemon 和 Compose v2。本轮验证的 Docker 为 28.1.1、Compose 为 2.35.1。第一次运行需要网络、镜像空间和安装依赖的时间；缓存后实际探针仍需数分钟。Weaver 使用临时 MongoDB，仅属于候选自身的状态存储，不是 V7 的业务数据库。

Toil 使用 `toil/pyproject.toml`、独立 `toil/uv.lock` 和 `toil/.venv`，不会加入根工作区运行依赖。镜像按 digest 固定在 `pins.json` 与 Compose 文件，gVisor 发行归档固定版本与 SHA-512。Weaver 启动仍会访问外部 OGC／GeoJSON Schema；这部分尚未冻结，所以当前 Weaver 环境不是完整离线复现环境。

Weaver 的结果地址需由部署方提供文件服务，应用自身不提供该下载端点，见 [官方配置说明](https://pavics-weaver.readthedocs.io/en/stable/configuration.html)。实验用固定 Python 镜像启动只读 HTTP 文件服务，只发布临时本机端口。Manager 与 Worker 将输出目录映射为 `/v7-p1-outputs`，避免隐藏目录名称被上游路径清理改写。该文件服务只用于受信任探针，不是平台的资产发布服务。

仅运行不启动外部组件的检查：

```bash
uv run --locked python -m unittest discover -s experiments/p1 -p 'test_*.py' -v
```

独立调试命令如下；每次使用新的结果目录，不并发运行这些实验（Weaver 的输出与临时文件根目录是共享的）：

```bash
uv sync --locked --project experiments/p1/toil
experiments/p1/toil/.venv/bin/python experiments/p1/toil_probe.py suite .p1-runtime/manual-toil-1
uv run --locked python experiments/p1/weaver_probe.py .p1-runtime/manual-weaver-1
uv run --locked python experiments/p1/isolation_probe.py .p1-runtime/manual-isolation-1
```

## 可核对的证据

`run.py` 每次生成独立 `.p1-runtime/verification-<id>/`，包含各探针 stdout／stderr、JSON 报告和总决策。总决策记录版本、环境、开始结束时间（Asia/Shanghai）、源码和锁文件摘要。`--record` 将精简证据额外写到指定路径，默认原始运行材料留在被 Git 忽略的 `.p1-runtime/`。复制精简证据不会同时复制原始日志与 JobStore。

2026-10-12 的重新验证及 rootless 探针修正见 [复验报告](../../docs/verification/p1-p3-runtime-followup-report.md)和 [最新结构化证据](../../docs/verification/p1-rootless-correction-evidence.json)；原 `p1-evidence.json` 与首次复验 `p1-followup-evidence.json` 保持原样。

| 实验 | 验证方法 |
| --- | --- |
| Toil 提交／查询／重定位 | 新客户端进程读取保存的句柄；重复提交必须指向原引擎；用 PID、启动 tick、boot ID 联合确认进程 |
| Toil 步骤身份 | 嵌套流程内 scatter 输入 `[7, 7]`；验证两个引擎 job ID、两个调用身份及结果 `[14, 14]`，不按输入值去重 |
| Toil 恢复 | 固定工具第一次退出 17；同一 JobStore `--restart` 后成功，工具 job ID 保持不变，attempt 为 1、2 |
| Toil 取消 | 等到真实工具写入启动标记；发出取消后核对引擎及工具后代停止、无输出；重复取消不再次发信号 |
| Weaver | 真实 REST 部署／提交／查询／下载，嵌套重复值 scatter，API 进程重启后按 job ID 查询，取消后检查实际 Docker 工具是否停止 |
| gVisor | 只读根与输入、宿主文件不可见、网络隔离、16 MiB 临时目录上限、实际 guest 取消后无延迟输出 |

Toil 使用公开消息总线读取 job 身份。9.5.0 在嵌套 scatter 中缩短父路径，因此实验将**固定夹具**的静态调用路径编码为唯一 tool ID，并保存映射。该编译器仅支持仓库内这组固定样例；不能作为一般 CWL 编译器或多层 scatter 完整身份方案。工具自己输出的身份还会与引擎作业记录交叉检查。

Weaver [官方 API 文档](https://pavics-weaver.readthedocs.io/en/stable/api.html)说明，底层服务不支持取消时，dismiss 可能只停止跟踪作业。因此探针必须检查实际 Docker 工具停止；单凭 HTTP 状态 `dismissed` 不能使候选通过。当前选型使用已通过此门槛的 Toil，Weaver 仅作为对照候选。

保存的 JSON sidecar 是实验定位材料，不是生产任务账本。提交途中断裂保持 `UNKNOWN`，不得重新提交、恢复或取消身份不明的进程。它没有 P2 的事务、租约、outbox、授权和预算，也没有覆盖整个主机掉电、磁盘损坏或网络分区。

## 隔离配置的适用边界

Toil 本轮用 `--no-container` 执行受信任的小型夹具；Weaver 的受信任 Worker 挂载 Docker socket 和只读 Docker CLI，工具容器不挂载该 socket。两者都不能据此接收不可信用户代码。

gVisor 单独验证：专用外层 Docker 容器以 `SYS_ADMIN`、`SYS_PTRACE` 和放宽的 seccomp／AppArmor profile 启动可信 runsc；外层无网络、只读根、512 MiB 内存、1 CPU、128 PID 限制。**外层权限属于可信执行基础设施。** guest 为 uid 65534、无 capabilities、no-new-privileges、只读根、无宿主挂载和 Docker socket、仅 loopback，临时目录 16 MiB。宿主 Docker daemon 配置没有修改，也没有注册全局运行时。

本机 rootless 分两项记录：`runsc --rootless do` 是受限的直接命令模式；OCI user namespace 映射使用另一条原生 rootless 路径，不附加 `--rootless`。纠正两种模式混用后，以 `--ignore-cgroups` 验证了 OCI 启动、状态查询和实际取消。宿主当前用户不能创建所需的 memory cgroup，因此该结果**不验证 CPU／内存限额**，不能作为完整隔离部署验收。gVisor [官方 rootless 文档](https://gvisor.dev/docs/user_guide/rootless/)区分了这两种模式。通过完整隔离与取消测试的配置仍是上述专用包装容器。外层 CPU／内存限额已配置，但本轮没有 OOM／CPU 压力或逃逸攻击验收；磁盘临时目录上限已实际触发验证。Toil 与 gVisor 尚未串接执行，正式集成和更全面故障测试属于 P4。

探针正常结束及普通异常路径会清理自身容器、Compose 网络和数据库卷；不会执行全局 prune。硬杀验证进程或主机故障可能留下资源，需要按该次项目名、容器 ID 和运行目录核对后清理，不能只凭 `P1` 标签批量删除其他运行。下载缓存和原始证据保留；Weaver 镜像以 root 写出的部分临时目录需要 Docker 管理权限才能清理。

## 依据

接口设计依据 [Toil CWL 文档](https://toil.readthedocs.io/en/latest/cwl/running.html)、[Toil CLI](https://toil.readthedocs.io/en/latest/running/cliOptions.html)、[Weaver 安装](https://pavics-weaver.readthedocs.io/en/latest/installation.html) 与 [配置](https://pavics-weaver.readthedocs.io/en/latest/configuration.html)、[gVisor 安装说明](https://gvisor.dev/docs/user_guide/install/)。这些滚动文档仅作参考；验收以固定版本的实际运行、源码摘要和 [P1 选型记录](../../docs/decisions/p1-execution-components.md) 为准。
