# P3 合成数据

`monitoring.csv` 是人工构造的验收示例，不是真实地下水观测，也不来自在线下载。
所有列按 `string` 导入；声明 UTC、ISO8601、单位 m、指标 `water_level_elevation`，缺失标记 `["", "NA"]`。

固定范围：`[2024-01-01T00:00:00+00:00, 2024-01-05T00:00:00+00:00)`，日周期，明确分组 `["A", "B", "absent"]`，趋势最低 3 个不同有效时点。

- A：4 个预期周期、3 个有效周期、1 个缺失周期，完整率 0.75；每日斜率 2、年化斜率 730.485、首个时点截距 1、R²=1。
- B：1 个有效周期、3 个缺失周期、1 个空值，完整率 0.25；趋势 NOT_EVALUABLE / INSUFFICIENT_DISTINCT_TIMES。
- absent：0 个有效周期、4 个缺失周期，完整率 0；趋势 NOT_EVALUABLE / INSUFFICIENT_DISTINCT_TIMES。

其余生成规则和独立预期值位于 `test_science.py`：常量、重复、闰年、日/月/季度/年、DST、区间终点排除、非法数值。`harness.py` 使用本表 A 的前三行和 absent 组，专门验证两次计算加一次验收的三次预算闭环。
