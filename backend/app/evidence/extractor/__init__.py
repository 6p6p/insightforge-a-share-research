"""Structured evidence extractor（Stage 3C.2）：RetrievalHit + LLM → EvidenceCard。

Extractor 只负责语义抽取（relevance / evidence_statement / evidence_type /
confidence / 逐字 quote_text）；quote_start/end、locator、provenance、
fingerprint 继续由确定性代码（3C.1 EvidenceCardService）负责。
不创建 Claim、不接 LangGraph、不调用 RetrievalService。
"""
