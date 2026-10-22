# P6 model catalogue

`model-catalog.deepseek-flash.json` freezes the real-model acceptance profile used on 2026-10-15. It contains no credential. Set `V7_MODEL_CATALOG` to this file and provide `DEEPSEEK_API_KEY` only through the process environment or a restricted secret file.

The profile uses the provider's OpenAI-compatible Responses endpoint, disables reasoning for the bounded decision suite, records the conservative peak CNY prices published on the evaluation date, and treats the provider as stateless (`supports_query_by_response_id=false`). A transport-unknown call is therefore not resubmitted automatically.

`deepseek-flash` is a provider alias rather than an immutable weight snapshot. The profile digest freezes V7 request limits, endpoint, pricing revision and model name; it cannot prevent the provider from changing the implementation behind that alias. Release evidence must retain the evaluation date, returned model name, catalogue digest and raw per-run usage.

P6.10 adds an optional embedding diagnostic adapter. Configure its origin without the `/v1/embeddings` suffix:

```bash
export PLATFORM_EMBEDDING_BASE_URL=http://192.168.167.89:38173
export PLATFORM_EMBEDDING_MODEL=qwen3-embedding-4b-q4
export PLATFORM_EMBEDDING_DIMENSIONS=1024
```

The adapter disables environment proxy inheritance and always calls `POST /v1/embeddings`. Embedding results are candidate signals only; they cannot authorize tools, satisfy policies or create acceptance facts.
