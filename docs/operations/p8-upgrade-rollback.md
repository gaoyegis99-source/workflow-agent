# P8 直接 Compose 维护、更新与恢复

P8 不再执行 Release-Lock 升级状态机。新实例由 `bin/configure-p8` 使用显式 artifact
目录、应用镜像和科学镜像输入创建；部署标识只用于追踪和容器标签，不是启动门禁。旧
`release-lock.json`、发布目录和验证指针不属于当前部署输入或工作区语料。

本说明只定义维护者明确执行的单机流程。它不启动 Docker daemon，不允许使用宿主 Docker，
也不承诺跨版本自动回滚。

## 更新顺序

1. 使用项目 socket 执行 `bin/p8 preflight`，确认控制文件与 0600 服务环境完整。
2. 打开维护模式，完成可校验冷备并执行 `bin/p8 verify`。
3. 停止写入服务；`bin/p8 backup` 会在备份完成后保持实例停止和维护状态。
4. 使用新的 artifact 和明确镜像输入配置一个**新**实例目录；配置器不会覆盖旧实例。
5. 对新实例执行 `bin/p8 preflight`、`bin/p8 up`、readiness 和实际任务/产物验证。
6. 只有验证通过后才切换上游 Gateway 入口；旧实例和冷备保留为人工恢复材料。

```bash
old_env=runtime/p8/instances/production/compose.env
backup=runtime/p8/instances/production/backups/<run-id>-before-update

bin/p8 preflight "$old_env"
bin/p8 maintenance-on "$old_env"
bin/p8 backup "$old_env" "$backup"
bin/p8 verify "$old_env" "$backup"

bin/configure-p8 \
  --instance production-next \
  --artifact-dir runtime/active/artifacts/production-next \
  --app-image groundwater-v7-app:next \
  --operator-image sha256:<operator> \
  --checker-image sha256:<checker> \
  --gdal-checker-image sha256:<gdal-checker> \
  --spatial-qgis-image sha256:<qgis> \
  --spatial-native-image sha256:<native> \
  --tabular-image sha256:<tabular> \
  --cwl-image sha256:<cwl>

new_env=runtime/p8/instances/production-next/compose.env
bin/p8 preflight "$new_env"
bin/p8 up "$new_env"
bin/p8 status "$new_env"
```

## 失败与恢复

在新实例的 readiness 或业务验证失败时，不让新旧实例同时写同一份数据库或状态目录。
保持失败实例停止，用已验证冷备恢复到隔离的新实例目录，并重新完成 readiness、任务和产物
验证。恢复是显式维护操作：没有通过哈希、数据库和对象验证的冷备不得使用。

锁时代的 `p8-upgrade.sh`、`build_p8_release.sh` 和 P7 lock builder 均会失败关闭，防止旧自动化
重新生成候选发布目录。
