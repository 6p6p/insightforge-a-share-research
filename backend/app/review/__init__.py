"""Report review routing + human confirmation (stage 5E.1).

从 `VerifiedReportAudit` 确定性派生出稳定的控制层 artifact（ReviewActionPlan /
human review request / human decision）并持久化。**0 LLM / 0 rewrite /
0 research / 0 LangGraph**（spec A/B）。
"""
