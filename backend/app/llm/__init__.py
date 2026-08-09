"""LLM runtime (stage 3C.2.1): Settings → provider adapter 的最小工厂与契约。

Evidence domain 通过 `EvidenceExtractionModel` Protocol 与 LLM 交互，**不直接
读取环境变量 / Settings**；`app.llm.factory` 是唯一把 Settings 分派到具体
provider adapter 的入口（第一版仅 DeepSeek）。
"""
