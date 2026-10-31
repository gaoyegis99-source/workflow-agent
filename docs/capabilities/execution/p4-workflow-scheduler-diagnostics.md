# Workflow 调度诊断

调度诊断记录 Execution 自有的性能事实，不参与任务状态、预算、授权、恢复或发布决策，也不通过
公开业务 API 读取。每次结果只属于对应运行号和机器负载；不得把旧结果提升为当前通过。

## 事件与指标

每个工作流至少记录提交、领取、引擎启动、节点开始/结束、收集和发布事件，并以稳定 workflow、
node、attempt 和 worker identity 关联。诊断计算：

- 排队延迟 p50/p95/p99；
- 节点运行、gather、发布和总耗时；
- 实际并行度与声明并发上限的利用率；
- Worker 角色领取分布、空闲与饥饿；
- bridge/tagger 的 RSS 与采样开销；
- 重复提交、重复预算、进程组残留和恢复告警。

缺事件、时间倒序、身份不一致、非零进程残留或测量开销超限都会使诊断失败。内存建议由本次
实测 p99 加安全系数得出，只是配置建议，不是部署授权。

## 执行

```bash
bash scripts/checks/check_p4_scheduler_diagnostics.sh --profile quick
bash scripts/checks/check_p4_scheduler_diagnostics.sh --profile standard
```

入口创建本次运行所需数据库、目录和容器，并把 JSON 与 Markdown 输出写到
`${V7_P4_RUNTIME_ROOT}/verification/`。`quick` 用于开发预检；`standard` 才能形成可比较的本机
基准。任何用于发布决策的结果都应在独占或稳定负载窗口重新运行，并上传外部制品系统。

后续调度实验从同一 runtime 根读取显式前序结果。基线不存在或身份不匹配时必须失败，不能读取
仓库内冻结报告或自动选择最近文件。
