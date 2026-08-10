# 阶段 5 计划概览

> 阶段 5 目标：把 Stage 4 已确认的 **SynthesisResult** 推进为**确定性、可追溯的报告产物**——ReportOutline（5A）→ DraftSection / Report（5B Writer）→ Deterministic Check（5C）→ Agent Audit（5D）→ Retry / Human confirmation（5E）。顶层证据链：**Source → Evidence → Claim → Report → Audit**；Report 生成与 Agent Audit 属于 Stage 5，**不写 5A=Writer、5B=Audit**。
>
> 总进度：**5A = completed（Deterministic ReportOutline Foundation，`REPORT_OUTLINE_SCHEMA_VERSION=1`、migration 0032，Gate 验收完成）**；**5B DraftSection（Writer）= next**；5C / 5D / 5E 未到验收门槛，不提前开工。

## 5A：Deterministic ReportOutline Foundation（completed）

- **原则**：**ReportOutline 由已验证的 SynthesisResult 确定性派生，不让 LLM 规划提纲**——0 planner model、0 analyst version。`create_or_get_outline(synthesis_result_id)` 只接收 synthesis_result_id，其余全部派生（company / question hash / cutoff / claim mapping / fingerprint）。
- **模型**：单表 `report_outlines`（migration 0032）：outline_id（UUID PK）、synthesis_result_id（FK RESTRICT）、company_id（FK RESTRICT）、research_question_sha256（CHAR(64)）、analysis_as_of（DATE）、outline_schema_version（INT）、outline_payload（JSONB）、outline_fingerprint（CHAR(64) UNIQUE）、created_at（TIMESTAMPTZ）；索引 synthesis_result_id / company_id / analysis_as_of；CHECK sha/fingerprint 64 lowercase hex、schema_version >= 1。downgrade 直接 drop（提纲确定性可重放）。
- **verify_result_integrity**（`SynthesisAnalysisService` public read-side）：verify SynthesisRun / result schema / analyst identity / 解析 payload / 验证 resolved claim IDs 全属 exact input set / 重算 result_fingerprint；损坏 → `SynthesisResultIntegrityError`。**不复制 replay 逻辑**。
- **确定性映射**（`report_outline/derive.py` 纯函数）：每个 theme → 一个 theme section（按 persisted normalized order；title 用 theme label 不重写；claim_ids = 该 theme 非 duplicate canonical Claims，canonical sort + dedupe）；有 conflicts / evidence_gaps 则末尾追加 risks_and_gaps section（只存 indexes，不生成解释正文）。
- **Coverage 硬边界**：所有 input Claims 必须属 theme section 或明确 duplicate_ref，否则 `ReportOutlineClaimCoverageError`。
- **Fingerprint / replay**：`outline_fingerprint` = canonical JSON SHA-256（含 schema version / synthesis_result_id / synthesis result fingerprint / company_id / question sha256 / analysis_as_of / normalized payload；不含 outline_id / created_at）。同 fingerprint → replay 同 outline_id；SynthesisResult 变化 → 新 Outline；**无 update API**。
- **0 LLM / 0 Chroma / 0 Retrieval / 0 Writer / 0 Audit**。
- 决策记录：按文档策略本轮不新建 ADR；migration 0032 docstring 记录 schema 与 downgrade 语义。

## 5B：DraftSection / Report（Writer，next）

- **范围**：ReportOutline → DraftSection（Writer 按 section 起草正文）→ Report 组装 → Deterministic Check（5C）。
- **不提前标记**：5B 未到验收门槛，不在本计划中定义细节；只有 5A 验收门槛全部关闭后才推进。
