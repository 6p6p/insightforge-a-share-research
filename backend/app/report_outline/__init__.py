"""Deterministic Report Outline (stage 5A): verified synthesis result → outline.

报告提纲是**确定性派生**产物：从已验证的 SynthesisResult（`VerifiedSynthesisResult`，
0 LLM）机械地映射为结构化提纲——每个 theme → 一个 theme section（按 persisted
normalized order）；conflicts / evidence_gaps → 末尾追加 risks_and_gaps section
（只存 indexes，不生成解释正文）。**不让 LLM 规划提纲**（0 planner model /
0 analyst version）。

提纲不可变：同 synthesis result + 同 schema + 同 normalized payload → 同
fingerprint → replay 同一行；SynthesisResult 变化 → 新提纲（无 update API）。
**不创建 Report / DraftSection / Audit 正文**。
"""
