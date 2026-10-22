# 工作区依赖边界

V7 的运行源码、Release 构建输入和 `.p8-runtime` 都位于本项目内。运行代码、脚本和
Compose/配置文件不得引用同一工作区中的 V5、V6 或其他兄弟工程。

`uv.lock` 是 Python 依赖的可重建身份；本地 `.venv` 可引用用户级 uv Python 缓存，
不把该缓存复制进项目。Docker 镜像、卷、部署密钥和生产状态由 P8 部署流程管理，
同样不属于源码工作树依赖。

历史验证材料可保留旧项目的绝对路径，用于说明当时的来源和复核范围；这些材料位于
`docs/verification/` 或 `.p8-runtime/verification/`，不参与当前运行配置。

执行以下命令检查当前边界：

```bash
uv run --locked python scripts/check_workspace_dependencies.py
```

该检查会拒绝运行源码、脚本与配置中的工作区兄弟路径，以及逃离项目根目录的符号链接。
