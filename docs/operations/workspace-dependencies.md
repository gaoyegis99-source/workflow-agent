# 工作区依赖边界

V7 的运行源码、构建输入和项目运行态都位于 `groundwater-v7/` 边界内。运行代码、脚本、
Compose 与配置文件不得引用同一工作区中的 V5、V6 或其他兄弟工程。

`uv.lock` 是 Python 依赖的可重建身份；本地虚拟环境和用户级包缓存不进入项目。Docker 镜像、
卷、部署密钥和生产状态由项目运行流程管理，也不属于源码依赖。历史报告、旧路径、候选清单和
生成制品不得作为例外留在源码树中；调查需要时应在工作区外读取。

执行以下命令检查当前边界：

```bash
uv run --locked python scripts/maintenance/check_workspace_dependencies.py
```

该检查拒绝运行源码、脚本与配置中的兄弟项目路径、宿主 Docker socket、跨产品导入，以及逃离
项目根目录的符号链接。
