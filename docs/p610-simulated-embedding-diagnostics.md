# P6.10 / 0.9.3 模拟 LLM 与 Embedding 诊断

本轮使用确定性模拟模型重新执行完整 P6，并使用局域网 OpenAI-compatible Embedding 服务对 A01–A15 做三轮跨语言候选检索。Embedding 只用于发现候选，不进入授权、预算、Action、工具参数、策略判断或验收事实。

## 运行边界

`platform-core` 新增纯标准库 `EmbeddingRequest`、`EmbeddingBatch` 和 `EmbeddingPort`。网络适配器位于 `groundwater-agent-runtime`，固定请求：

```json
{
  "model": "qwen3-embedding-4b-q4",
  "input": ["文本一", "文本二"],
  "dimensions": 1024
}
```

适配器严格检查模型、返回项数量、索引、维度、有限数、非零向量、usage 和网关元数据。单批最多 128 条、每条最多 32,000 字符、总计最多 256,000 字符，响应最大 64 MiB。默认单请求墙钟上限 60 秒，不做隐藏重试。

配置：

```bash
export PLATFORM_EMBEDDING_BASE_URL=http://192.168.167.89:38173
export PLATFORM_EMBEDDING_MODEL=qwen3-embedding-4b-q4   # 可省略
export PLATFORM_EMBEDDING_DIMENSIONS=1024               # 可省略
bash scripts/check_p6_diagnostics.sh
```

客户端使用空 `ProxyHandler`，不继承 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。真实测试在代理指向不可达地址并清空 `NO_PROXY` 的情况下仍成功请求局域网服务。

## 评测语义

冻结集为 15 张英文 P6 处置卡和 15 条中文改写查询，覆盖计划、澄清、P3、P4、Knowledge、证据撤回、地下水策略、精确成果、响应丢失、checkpoint、取消、有限修复和提示注入。

选定模型门槛：Top-1 不低于 73%，Recall@3 不低于 93%，MRR 不低于 0.82，批量与单条余弦一致性不低于 0.999。门槛只判断候选检索质量，不证明可以自动执行候选动作。

`qwen3-embedding-4b-q4` 在三轮中均为 Top-1 100%、Recall@3 100%、MRR 1.0。`qwen3-embedding-0.6b-q8` 和 `bge-m3` 均把 A08 垂向基准冲突排在 A02 一般语义澄清之后，说明相似度无法替代确定性地下水策略。

Embedding 当前没有接入 Knowledge 生产检索，也不是 Agent 启动前置依赖。要进入生产检索，还需要独立设计索引版本、资产生命周期、撤回传播、缓存、服务认证和不可变模型制品身份。
