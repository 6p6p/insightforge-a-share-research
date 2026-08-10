"""Claim Synthesis Input & Provenance Foundation (stage 4D.1A).

把调用方显式选出的 2..50 条已验证 Claim + company + research_question +
analysis_as_of 登记为一个不可变 SynthesisRun（input set boundary），供未来
LangGraph 合成节点消费。**不创建 Report / DraftSection**；不调用 LLM。
"""
