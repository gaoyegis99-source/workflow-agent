# P8 冷备份、停车快照与干净恢复

P8 使用直接 Compose 实例。控制文件 `compose.env` 与服务级环境文件是部署描述的一部分，
但不使用 Release Lock 作为启动或恢复门禁。运行时操作必须由维护者明确执行；Docker socket
不可用时命令阻断，不启动 daemon 或回退宿主 Docker。

普通冷备份以可校验、可在全新实例中恢复的静止快照为目标：所有 Action、导入、检查、原子
发布、Agent 调用和 Studio 判定必须先收敛。若存在在途事实，`park-manifest.json` 必须精确
证明停车，且恢复身份和目录摘要完整；否则备份应失败关闭。

## 创建冷备份

```bash
bin/p8 backup \
  runtime/p8/instances/production/compose.env \
  runtime/p8/instances/production/backups/backup-20261017
```

命令按以下顺序执行：

1. gateway 创建维护标记，所有公开 `POST/PUT/PATCH/DELETE` 返回 `503 MAINTENANCE_IN_PROGRESS`；读取仍可用。
2. 等待 Platform、Data、Execution、Knowledge 的持久工作全部收敛。
3. 停止 Agent 和 Authoring 等生产者。
4. 再次检查收敛状态，然后停止结算 Worker 和 API。
5. 分别以只读 `REPEATABLE READ` 事务导出四个 PostgreSQL 数据库。每张表使用 PostgreSQL binary COPY，记录表名、恢复顺序、行数和 SHA-256。
6. 复制 Data 字节、Knowledge 正文、Execution 工作目录、Toil JobStore 和空间检查目录。符号链接只保存链接文本，绝不跟随到树外。
7. 复制实例部署描述、Operator、Checker、Tool、Model Catalog 与 Runtime Profile；记录
   artifact 文件摘要、部署 revision 和显式镜像输入。
8. 写入 manifest，`fsync` 后原子重命名备份目录。

备份完成后实例保持停止和维护状态。先验证备份，再决定恢复原实例：

```bash
bin/p8 verify \
  runtime/p8/instances/production/compose.env \
  runtime/p8/instances/production/backups/backup-20261017

bin/p8 resume runtime/p8/instances/production/compose.env
```

`verify` 会重新计算数据库 COPY、状态文件、目录清单和部署描述摘要。任何已列文件、表快照、
目录清单或配置发生变化都会失败。

## 在全新环境恢复

恢复目标必须使用新实例名、新端口、新数据库目录和新状态目录。不要把目标指向原实例路径。

```bash
bin/configure-p8 \
  --instance restored \
  --artifact-dir runtime/active/artifacts/restored \
  --app-image groundwater-v7-app:restored \
  --operator-image sha256:<operator> \
  --checker-image sha256:<checker> \
  --gdal-checker-image sha256:<gdal-checker> \
  --spatial-qgis-image sha256:<qgis> \
  --spatial-native-image sha256:<native> \
  --tabular-image sha256:<tabular> \
  --cwl-image sha256:<cwl> \
  --gateway-port 18100
```

将备份作为完整目录复制到新实例的 `backups` 目录，然后执行：

```bash
bin/p8 restore \
  runtime/p8/instances/restored/compose.env \
  runtime/p8/instances/restored/backups/backup-20261017

bin/p8 resume runtime/p8/instances/restored/compose.env
```

恢复流程先启动空 PostgreSQL、创建四数据库和角色、运行与备份兼容的 schema，再打开维护标记。
恢复器在修改第一套数据库前预检四个目标 schema、实例部署描述和 artifact 摘要；随后清空业务表
并按外键顺序导入 binary COPY，再替换四棵状态目录。恢复期间 API 与 Worker 不运行。

恢复完成后应核对：

- Task、预算、Action、Execution、节点和远程调用身份；
- AssetRef、版本、内容摘要、Publication 和输出成员；
- DocumentVersion、EvidenceRef、读取回执和生命周期；
- Deliverable、Acceptance、Completion 和 checker 运行；
- AgentRun、冻结 Tool/Operator/Model/Runtime 身份；
- Workflow Definition、Candidate、Trial、Release、PackageRef 和生命周期修订。

## 在途恢复与边界

P8.2 已真实验证活动 QGIS/Native/StaticMap 工作流的维护停车与升级恢复：Toil leader 在节点间安全点停车，备份保留原父 Action、节点 Action、JobStore 和成果身份；目标版本以新 epoch 接管并完成原来的三个节点。停车前后没有重复 Execution、预算结算或 Publication。

协议仍保持以下保守边界：

- 普通 `bin/p8 backup` 只接受完全收敛的实例；在途工作必须先建立维护屏障和可验证的
  停车事实，不能使用已退役的 `p8-upgrade.sh`。
- 已启动的本地科学容器最多等待 240 秒完成并交接输出；未完成时停车失败并恢复旧服务，不强杀后继续备份。
- 已被远端接受的 Action 保存原远程 Execution ID。普通 404、断线和响应丢失保持 `PARKED_UNKNOWN/UNKNOWN`，不得换身份重算。
- JobStore 关键文件缺失、摘要不符、执行身份冲突或兼容镜像缺失时停止恢复，不重新编译、不生成新节点，也不猜测结果。
- 同一持久目录不能由两个 deployment 同时接管；部署 revision 不一致的服务不能进入 ready 状态。

该流程是单机维护窗口下的冷停车与恢复协议，不是在线热备份或蓝绿双写。灾难恢复的 RPO 仍取决于最近一次成功且已验证的备份。
