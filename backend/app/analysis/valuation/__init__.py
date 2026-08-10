"""Structured Relative Valuation Analysis (stage 4C.2B.2).

`app/analysis/valuation/` 把 4C.2B.1 的 RelativeValuationComparison provenance
接上 DeepSeek structured 分析：`Comparison[] + research question + analysis_as_of`
→ LLM 判断（assessment / confidence / importance / comparison relations）→
确定性校验（no cherry-picking / direction consistency / uncertain policy）→
确定性 statement 渲染 → ValuationClaimDraft(schema v7) → ValuationClaimService
→ Relative Valuation Claim。

**LLM 不负责**：计算 median / premium、选择 peers、生成数值、生成 Claim
statement、生成 target price / fair value / 交易建议。
"""
