# Open Acceptance 运行与维护

Open Acceptance 使用固定 checker 镜像。开放检查与 Decimal 检查可以共享受限镜像，但必须使用
不同 implementation ID 和协议。容器无网络、只读根目录、删除 capabilities，并限制 CPU、内存和
进程数；生产配置使用不可变 image ID，不依赖标签或旧证据推断身份。

Platform Worker 通过 `V7_KNOWLEDGE_URL` 和服务凭据领取 Knowledge 生命周期 outbox。事件在
Platform inbox 去重，并按文档版本的 lifecycle revision 排序。ack 响应丢失时允许重投，但不得
重复追加失效原因。

## 验收

```bash
bash scripts/checks/check_p5.sh --phase all
uv run --locked pytest -q tests/knowledge/acceptance
uv run --locked python scripts/checks/check_p53_checker.py
```

结果写入 `${V7_P5_RUNTIME_ROOT}/verification/`。需要长期保存时上传外部制品系统，不回写源码文档。

## 更新与回退

更新前停止新验收，等待活动 checker、执行和发布收敛，完成冷备，然后整体替换包、Catalog 和
checker 镜像。显式执行 Platform 与 Knowledge 迁移及 schema check 后再恢复 Worker。

回退必须先停止所有相关服务，并确认目标版本能够读取现存 lifecycle 事件和验收状态。降级不得
重写已有任务、成果、验收报告、研究协议或 Knowledge 正文；无法安全解释的活动工作应从经验证的
冷备恢复到隔离实例。
