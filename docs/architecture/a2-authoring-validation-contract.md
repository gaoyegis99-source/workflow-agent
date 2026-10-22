# A2 / 0.14.1 编辑与受信校验契约

A2 把不可变草稿扩展为受限图编辑。可编辑范围只有根输入、登记算子、数据边、固定参数和最终输出。条件、scatter、子工作流、任意表达式、Shell、外部引用、镜像和宿主路径不能由 A2 写入；无法无损投影的历史源码只读展示。

## Source 与画布

`v7-cwl-p4-v1` source 是权威对象，画布是确定性投影。保存时客户端提交 source、metadata 和 layout；Platform 在事务中重新检查 head、旧 source digest 和受限 source envelope，再追加不可变 revision。layout 只保存位置等展示事实，不进入 source digest。

算子实现由服务端描述符决定。客户端不能提交目录之外的算子、实现、镜像、命令或资源。固定参数由 JSON Schema 编辑，但最终有效性始终由 Pydantic DTO 的服务端校验决定。

## 结构预览

`POST /api/v1/projects/{project_id}/authoring/previews` 接受精确 revision 引用，或一份尚未保存的受限 source；两者必须且只能选择一个。可选样例绑定最多 64 个，按当前身份从 Data 解析。跨项目、错摘要和不可读资产由权威服务拒绝。

预览不持久化，不创建 Action、Command、outbox、预算或容器。它只传播受信结构事实，并返回 `COMPATIBLE`、`INCOMPATIBLE` 或 `REQUIRES_BINDING`。未知结构保持未知；不会为了让连线变绿而猜测字段、CRS 或业务语义。

## 正式校验

`POST .../workflow-definitions/{definition_id}/validations` 为精确 revision 创建异步校验。记录冻结 source、Operator Catalog、语义契约、编译器身份、cwltool 镜像和验证上下文摘要。Worker 用 `FOR UPDATE SKIP LOCKED`、租约和递增 generation 领取；旧 Worker 不能回写。

Worker 先执行同一受限图检查，再生成 CWL 包并用固定、无网络、只读文件系统的 cwltool 容器校验。语义环境在排队期间变化时结果为 `ERROR / VALIDATION_ENVIRONMENT_CHANGED`；纯展示描述变化不会使语义结果过期。Docker 或 validator 不可用返回可重试 ERROR，不伪装成 source REJECTED。

校验成功仅证明当前受限图能被受信编译器和 cwltool 接受，不代表执行结果或科学结论正确。A3 才会引入候选、真实试运行和发布。
