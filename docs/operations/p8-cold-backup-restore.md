# P8.2 冷备份、停车快照与干净恢复

普通冷备份仍以可校验、可在全新实例中恢复的静止快照为目标：所有 Action、导入、检查、原子发布、Agent 调用和 Studio 判定必须先收敛。P8.2 的维护升级流程另行生成 schema v2 备份；它只允许 `park-manifest.json` 已精确证明停车、且恢复身份和目录摘要完整的在途事实进入备份。

## 创建冷备份

```bash
bash scripts/p8.sh backup \
  .p8-runtime/instances/production/p8.env \
  .p8-runtime/instances/production/backups/backup-20261017
```

命令按以下顺序执行：

1. gateway 创建维护标记，所有公开 `POST/PUT/PATCH/DELETE` 返回 `503 MAINTENANCE_IN_PROGRESS`；读取仍可用。
2. 等待 Platform、Data、Execution、Knowledge 的持久工作全部收敛。
3. 停止 Agent 和 Authoring 等生产者。
4. 再次检查收敛状态，然后停止结算 Worker 和 API。
5. 分别以只读 `REPEATABLE READ` 事务导出四个 PostgreSQL 数据库。每张表使用 PostgreSQL binary COPY，记录表名、恢复顺序、行数和 SHA-256。
6. 复制 Data 字节、Knowledge 正文、Execution 工作目录、Toil JobStore 和空间检查目录。符号链接只保存链接文本，绝不跟随到树外。
7. 复制 Release Lock 和 Operator、Checker、Tool Catalog；Release Lock 固定所有运行镜像摘要。
8. 写入 manifest，`fsync` 后原子重命名备份目录。

备份完成后实例保持停止和维护状态。先验证备份，再决定恢复原实例：

```bash
bash scripts/p8.sh verify \
  .p8-runtime/instances/production/p8.env \
  .p8-runtime/instances/production/backups/backup-20261017

bash scripts/p8.sh resume .p8-runtime/instances/production/p8.env
```

`verify` 会重新计算数据库 COPY、状态文件、目录清单和配置摘要，并核对 Release Lock。任何已列文件、表快照、目录清单或配置发生变化都会失败。

## 在全新环境恢复

恢复目标必须使用新实例名、新端口、新数据库目录和新状态目录。不要把目标指向原实例路径。

```bash
uv run --locked python scripts/configure_p8.py \
  --instance restored \
  --gateway-port 18100 \
  --execution-port 18102 \
  --postgres-port 55470
```

将备份作为完整目录复制到新实例的 `backups` 目录，然后执行：

```bash
bash scripts/p8.sh restore \
  .p8-runtime/instances/restored/p8.env \
  .p8-runtime/instances/restored/backups/backup-20261017

bash scripts/p8.sh resume .p8-runtime/instances/restored/p8.env
```

恢复流程先启动空 PostgreSQL、创建四数据库和角色、运行与备份完全相同的 schema，再打开维护标记。恢复器在修改第一套数据库前预检四个目标 schema 和 Release Lock；随后清空业务表并按外键顺序导入 binary COPY，再替换四棵状态目录。恢复期间 API 与 Worker不运行。

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

- 普通 `p8.sh backup` 只接受完全收敛的实例；在途升级必须经 `p8-upgrade.sh prepare` 建立维护屏障、停车事实和 schema v2 备份。
- 已启动的本地科学容器最多等待 240 秒完成并交接输出；未完成时停车失败并恢复旧服务，不强杀后继续备份。
- 已被远端接受的 Action 保存原远程 Execution ID。普通 404、断线和响应丢失保持 `PARKED_UNKNOWN/UNKNOWN`，不得换身份重算。
- JobStore 关键文件缺失、摘要不符、执行身份冲突或兼容镜像缺失时停止恢复，不重新编译、不生成新节点，也不猜测结果。
- 同一持久目录不能由两个 deployment 同时接管；Release digest 不一致的服务不能进入 ready 状态。

该流程是单机维护窗口下的冷停车与恢复协议，不是在线热备份或蓝绿双写。灾难恢复的 RPO 仍取决于最近一次成功且已验证的备份。
