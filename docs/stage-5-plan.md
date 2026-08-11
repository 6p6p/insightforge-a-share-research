# 阶段 5 计划概览

> 阶段 5 目标：把 Stage 4 已确认的 **SynthesisResult** 推进为**确定性、可追溯的报告产物**——ReportOutline（5A）→ DraftSection / Report（5B Writer）→ Deterministic Check（5C）→ Agent Audit（5D）→ Retry / Human confirmation（5E）。顶层证据链：**Source → Evidence → Claim → Report → Audit**；Report 生成与 Agent Audit 属于 Stage 5，**不写 5A=Writer、5B=Audit**。
>
> 总进度：**5A = FINAL（Deterministic ReportOutline Foundation，`REPORT_OUTLINE_SCHEMA_VERSION=1`、migration 0032，Gate 验收完成）**；**5B = FINAL（Evidence-bound DraftSection Writer，`DRAFT_SECTION_SCHEMA_VERSION=1`、migration 0033，Gate 验收完成）**；**5C = FINAL（Deterministic Report Assembly + Check，`REPORT_SCHEMA_VERSION=1`、`REPORT_CHECK_SCHEMA_VERSION=1`、migration 0034，Gate 验收完成）**；**5D = completed（Evidence-bound Agent Audit，`REPORT_AUDIT_SCHEMA_VERSION=1`、migration 0035）**；**5E = FINAL（Rewrite / Human confirmation / Research backflow，`REVISION_SCHEMA_VERSION=1`、`RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION=1`、migration 0036–0038，Gate 验收完成）**。

## 5A：Deterministic ReportOutline Foundation（completed，FINAL）

- **原则**：**ReportOutline 由已验证的 SynthesisResult 确定性派生，不让 LLM 规划提纲**——0 planner model、0 analyst version。`create_or_get_outline(synthesis_result_id)` 只接收 synthesis_result_id，其余全部派生（company / question hash / cutoff / claim mapping / fingerprint）。
- **模型**：单表 `report_outlines`（migration 0032）：outline_id（UUID PK）、synthesis_result_id（FK RESTRICT）、company_id（FK RESTRICT）、research_question_sha256（CHAR(64)）、analysis_as_of（DATE）、outline_schema_version（INT）、outline_payload（JSONB）、outline_fingerprint（CHAR(64) UNIQUE）、created_at（TIMESTAMPTZ）；索引 synthesis_result_id / company_id / analysis_as_of；CHECK sha/fingerprint 64 lowercase hex、schema_version >= 1。downgrade 直接 drop（提纲确定性可重放）。
- **verify_result_integrity**（`SynthesisAnalysisService` public read-side）：verify SynthesisRun / result schema / analyst identity / 解析 payload / 验证 resolved claim IDs 全属 exact input set / 重算 result_fingerprint；损坏 → `SynthesisResultIntegrityError`。**不复制 replay 逻辑**。
- **确定性映射**（`report_outline/derive.py` 纯函数）：每个 theme → 一个 theme section（按 persisted normalized order；title 用 theme label 不重写；claim_ids = 该 theme 非 duplicate canonical Claims，canonical sort + dedupe）；有 conflicts / evidence_gaps 则末尾追加 risks_and_gaps section（只存 indexes，不生成解释正文）。
- **Coverage 硬边界**：所有 input Claims 必须属 theme section 或明确 duplicate_ref，否则 `ReportOutlineClaimCoverageError`。
- **Fingerprint / replay**：`outline_fingerprint` = canonical JSON SHA-256（含 schema version / synthesis_result_id / synthesis result fingerprint / company_id / question sha256 / analysis_as_of / normalized payload；不含 outline_id / created_at）。同 fingerprint → replay 同 outline_id；SynthesisResult 变化 → 新 Outline；**无 update API**。
- **0 LLM / 0 Chroma / 0 Retrieval / 0 Writer / 0 Audit**。
- 决策记录：按文档策略本轮不新建 ADR；migration 0032 docstring 记录 schema 与 downgrade 语义。

## 5B：Evidence-bound DraftSection Writer（completed，FINAL）

- **范围**：VerifiedReportOutline + section_id → Section Input Pack（C/E/X/G 确定性 alias，LLM 永不见 UUID / fingerprint）→ DeepSeek V4 Flash（thinking disabled + temperature=0 + structured output）→ 确定性校验（ref format / known / cross-section / unbound / numeric grounding / forbidden）→ DraftSection。**0 tools / 0 web / 0 Chroma**。
- **模型**：单表 `draft_sections`（migration 0033）：writer_input_fingerprint UNIQUE、section_fingerprint、writer_name/version、provider/model_id；downgrade 在存在任意 row 时拒绝（`RuntimeError`）。持久化 payload 只存真实 claim_id / evidence_card_id / index，不存 alias / prompt / raw response / reasoning_content。
- **Fingerprint / replay**：`writer_input_fingerprint` = outline_fingerprint + section 身份 + allowed Claim/Evidence fingerprints + writer 身份 的 SHA-256；同输入 → replay 同一行（0 model calls，ON CONFLICT DO NOTHING，无 Python lock）。
- **测试**：Fake Writer（0 real LLM）E2E + 16 integration + domain 单测 + 回归全绿；受控 smoke 1 次真实 DeepSeek 通过。

## 5C：Deterministic Report Assembly + Check（completed）

- **范围**：`ReportAssemblyDraft(outline_id, draft_section_ids)` 显式精确选择 → 逐 DraftSection verify → 机械拼装 v1 Report payload（coverage/identity 硬边界）→ 10 项确定性 Check（report_fingerprint = canonical JSON SHA-256）。**0 LLM / 0 Chroma / 0 Retrieval**。
- **模型**：`reports` + `report_check_results`（migration 0034，FK RESTRICT；downgrade 在有行时拒绝）；`REPORT_SCHEMA_VERSION=1`、`REPORT_CHECK_SCHEMA_VERSION=1`。Check finding 只含 code/section_id/paragraph_index/related refs；status pass/fail。
- **测试**：真实 PG E2E（Fake Writer 全 draft → Report → Check pass）+ replay/并发/拒绝路径/tamper + migration 0034 downgrade guard；全程 0 真实 DeepSeek。

## 5D：Evidence-bound Agent Audit（completed）

- **范围**：确定性 routing 决定 pass/rewrite/research/human_review；Auditor Pack（只含 alias）+ 结构化输出，模型只输出 issues，不决定路线。**0 tools / 0 web / 0 Chroma / 0 检索**。
- **模型**：`report_audits` + `review_issues`（migration 0035，FK RESTRICT；downgrade 在有行时拒绝）。
- **测试**：真实 PG 最小链 E2E + 确定性 routing 单测 + Check Integrity（tamper 拒绝）+ provenance closure（document_chunk / macro_observation）+ migration 0035 downgrade guard；受控 smoke 1 次真实 DeepSeek 通过。

## 5E：Retry / Human confirmation / Rewrite / Research backflow（FINAL）

- **5E.1 = FINAL（Review Routing + Human Confirmation Foundation，`REVIEW_ACTION_SCHEMA_VERSION=1`、`HUMAN_REVIEW_REQUEST_SCHEMA_VERSION=1`、`HUMAN_REVIEW_DECISION_SCHEMA_VERSION=1`、migration 0036，Gate 验收完成）**：VerifiedReportAudit → 确定性 review routing → ReviewActionPlan →（若 human_review）HumanReviewRequest → HumanReviewDecision 正式持久化；0 LLM / 0 Chroma / 0 Retrieval；三层逐层 verify integrity（不 repair）；downgrade 有行时拒绝。
- **5E.2A = FINAL（Rewrite + Human control loop，`REVISION_SCHEMA_VERSION=1`、migration 0037，Gate 验收完成）**：rewrite 路线确定性写入 DraftSectionRevision；rewrite / research / human 三路线由 LangGraph 统一调度闭环；Stage5 不越过 Stage2/3/4。
- **5E.2B = completed（Research Backflow Contract + Continuation，`RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION=1`、`RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION=1`、migration 0038）**：research request / fulfillment 两张 immutable 表；从 Stage5 final checkpoint 恢复身份 → request/fulfillment fingerprint + 确定性 replay；continuation identity（company/question/cutoff 全等）+ no-progress 政策（新 result 与新 run fingerprint 双不等）；`build_stage5_continuation_request` 让新 Stage5 run 以 pass audit finalize。**Stage5 不执行 Stage2/3/4 research**——只做可验证 handoff 并消费 upstream 新 SynthesisResult。
- **Stage 5 总体 = FINAL**：报告主线 **Source → Evidence → Claim → Report → Audit → ReviewAction → Revision / ResearchBackflow** 全链路闭环。
