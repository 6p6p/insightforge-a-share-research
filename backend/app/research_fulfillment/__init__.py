"""Research fulfillment (stage 7A.2A): 自动补证据（只消费 missing_needs）。

`fulfill_research_needs(research_plan_id)`：verify Plan + verify Route +
`prepare_research()` → 对每个 missing need 分发到 executor（document / financial
/ macro / valuation）自动补证据 → 重跑 `prepare_research()` → 产出
`ResearchFulfillmentResult`（schema v1，application output，不建表）。

scope = 从**现有**证据库自动补证据：Retrieval→Evidence / calculation→
re-preparation / macro Evidence replay / valuation manual_required。**不**做
全网无限搜索 / 复杂浏览器 agent / 自动 peer 选择 / Top-level Graph / live
provider fetch（0 real DeepSeek / 0 Retrieval / 0 Chroma query / 0 Web fetch
在确定性测试中）。
"""
