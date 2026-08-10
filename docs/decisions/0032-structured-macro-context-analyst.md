# ADR-0032：Structured Macro Context Analyst + analysis_as_of 查询列（阶段 4C.1B + Gate 0）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 背景

ADR-0030 / ADR-0031 建立了 Macro Claim 的传导 provenance 基础（migration 0023 +
0024，schema v5/v2，4C.1A = FINAL）。4C.1B 把该基础**接上 LLM**：Macro Evidence +
Company Evidence + research question → 结构化 Macro Context 分析 → Macro Claim，
镜像 4B.2C.2 Structured Financial Analysis 的"Analyst 只做判断、确定性交给代码"
角色边界，成为 4C 宏观看板的第一个分析链路。

**Gate 0 审计发现**：`analysis_as_of` 目前只存在于 transmission / claim
fingerprint 与 `MacroClaimDraft`（语义输入），**不是查询列**——fingerprint 含日期
但 DB 无法从 claim_id 反推 cutoff；未来 Audit / Writer / API / Claim 检查必须能
从 claim_id 拿到 analysis_as_of。故新增 **migration 0025** 查询列。

## 决策

1. **Gate 0：`analysis_as_of` 查询列（migration 0025）**：
   `macro_transmission_chains.analysis_as_of DATE NULL` + CHECK
   `(transmission_schema_version < 3 OR analysis_as_of IS NOT NULL)` + 普通 INDEX
   `(company_id, analysis_as_of)`。**不做任何 backfill**：历史 v1/v2 链保持
   analysis_as_of=NULL（0024-era 语义），且**绝不从 created_at / published_at /
   reporting_period_end / fingerprint 反推历史 cutoff**。downgrade guard：仅当不存在
   (a) 任何 `transmission_schema_version >= 3` 的链 **且** (b) 任何
   `analysis_domain='macro' AND claim_schema_version >= 6` 的 Claim 时允许回滚，
   否则显式拒绝（不删除数据 / 不修改行 / 不静默丢弃 cutoff provenance），
   `alembic_version` 保持 0025。不修改 0023 / 0024；不改写历史行。

2. **Version boundary（v6/v3 当前，v5/v2/v4/v1 冻结 legacy）**：
   `MACRO_CLAIM_SCHEMA_VERSION=6`、`MACRO_TRANSMISSION_SCHEMA_VERSION=3`；
   `MACRO_CLAIM_SCHEMA_VERSION_V5=5`、`MACRO_TRANSMISSION_SCHEMA_VERSION_V2=2`、
   `MACRO_CLAIM_SCHEMA_VERSION_V4=4`、`MACRO_TRANSMISSION_SCHEMA_VERSION_V1=1`
   冻结不改写。**两个 fingerprint 的 payload 都包含 schema version**——v6/v3 与
   v5/v2、v4/v1 永不误 collision：版本升级 = 新 fingerprint = 新 Claim + 新链，
   历史对象原样保留。**不污染 generic ClaimDraft / FinancialClaimDraft**。
   **Replay 版本感知分叉（four-branch，不 repair）**：v6 → 当前 v3/v6 规则
   （document driver 资格 / availability / time-alignment policy，**额外核验
   chain.analysis_as_of == draft.analysis_as_of**——0025 起的 v3 查询列语义）；
   v5 → v2/v5 历史规则（与 v6 同一套资格 / 可用性政策，但 0025 不 backfill，
   历史链 analysis_as_of=NULL 允许，不反推 cutoff）；v4 → v1/v4 legacy 规则
   （macro_driver 必须 macro_observation；normalized_period_start /
   source_published_at / reporting_period_end 可用时间）；其他值 →
   `MacroClaimIntegrityError`。

3. **Structured Macro Context Analyst（4C.1B）流程（10 步，镜像 Financial）**：
   ① 防御性 request 校验；② 短 DB session：加载全部 Macro Evidence（macro_driver
   池）+ Company Evidence（company 池）并**逐条校验**（任一缺失 → EvidenceNotFound、
   跨公司 → CompanyMismatch、provenance 链缺失 → Corrupted；macro_driver 池逐条
   满足 v2/v3 资格——macro_observation 或 news_article + evidence_type ∈ {event,
   fact, statement}，违反 → OriginViolation；company 池每条 origin_type=
   document_chunk，违反 → OriginViolation；全部 availability 解析（缺失 →
   TemporalInsufficient），任何 future（availability > analysis_as_of）→
   FutureEvidence）；③ 关闭 DB session（**LLM 调用期间不持有 DB transaction /
   connection**）；④ 构造 M/E alias；⑤ 调 `MacroAnalysisModel.analyze` →
   `MacroAnalysisDecision`（provider 失败 → ModelUnavailable；输出无法解析 →
   MalformedOutput）；⑥ 防御性 schema double-check；⑦ relevant=false → 0-claims
   （不写任何 Claim）；⑧ macro numeric-literal guard v1；⑨ M/E ref resolution +
   overclaim policy；⑩ 构造全部 v6 `MacroClaimDraft` + claim_kind policy →
   `MacroClaimService.create_claim_batch`（1..3 drafts，单 transaction）→
   `MacroAnalysisResult`。**不创建 Report / DraftSection / ReviewIssue / Audit**；
   不接 LangGraph 分析节点；不调用 Retrieval / Chroma / RawArtifact / tools / web
   search；不做任何宏观定量计算 / 估值。

4. **请求 / 决策契约**（`app/analysis/macro/contracts.py`）：`MacroAnalysisRequest`
   （macro_driver_evidence_ids 1..20、company_evidence_ids 1..30、两池**不重叠**、
   去重 + canonical 排序、research_question 非空）；`MacroClaimCandidate`
   （claim_kind **只允许 inference/risk**——**schema 层拒绝 fact**：宏观定量事实由
   Macro Evidence 承载，Analyst 只解释并判断风险，`MacroClaimDraft` 更低层契约仍
   支持 fact 但 service `_check_kind_policy` 兜底；每条 ≥1 macro_driver_ref（`M<number>`）
   + ≥1 company_exposure_ref（`E<number>`），**无 reasoning / CoT / UUID /
   fingerprint**）；`MacroAnalysisDecision`（relevant=false → 空 claims + 可选
   reason_code ∈ {not_relevant, insufficient_macro_evidence,
   insufficient_company_evidence}；relevant=true → 1..3 claims + 无 reason_code）；
   `MAX_CLAIMS_PER_DECISION=3`。

5. **M/E alias 设计**：`build_macro_driver_pack` 按 `str(evidence_card_id)` 升序
   编号 **M1..Mn**（确定性），`build_company_evidence_pack` 同法编号 **E1..En**，
   两池 namespace **严格分离**（M 只对应 macro_driver 卡、E 只对应 company 卡）。
   **最小投影**：只发送证据的人类可读摘要与确定性字段（evidence_statement /
   evidence_type / provider_key / authority_tier_snapshot / availability /
   quote_text / indicator 摘要 / value_summary 等）；**不发送** UUID / fingerprint /
   source UUID / observation UUID / locator / raw / Chroma。

6. **Numeric-literal guard v1（独立于 Financial guard）**：statement 禁止
   ASCII/full-width digits / `%％` / 中文数字字符（`零〇二两三四五六七八九十百千万亿兆`）/
   定量短语（`百分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数 / 一成 / 一半 / 一点`）/
   numeric-context（`第?+一+季/月/年/期/日/号`），违反 →
   `MacroAnalysisNumericLiteralForbidden` 整次 0 写；**不自动删数字 / 不改写 /
   不让第二个 LLM 修正**；**`一/点` 本身允许**（"一定/进一步/观点"等非数量词可用）。

7. **Ref resolution + overclaim contract**：M/E → UUID 确定性映射（不 fuzzy
   resolve）；未知 M/E → `MacroAnalysisUnknownRef`、跨 relation →
   `MacroAnalysisRelationConflict`；组内去重 + canonical 排序；**任一 candidate
   无效 → 整次 0 写**。overclaim contract 防线：`observed_impact` 需 ≥1
   `observed_effect_ref`（否则只能 plausible_impact）；`time_alignment=uncertain`
   只允许 `plausible_impact + risk + normal`；`MacroClaimDraft.__post_init__` 与
   service `_check_overclaim_policy` / `_check_kind_policy` 双重兜底。

8. **共享纯函数（禁止重复实现）**：`resolve_availability` /
   `driver_evidence_eligible` 位于 `app/claims/macro_policy.py`，由
   `MacroClaimService` 与 `MacroAnalysisService` **共用**同一 no-lookahead /
   driver 资格策略；availability 语义沿用 ADR-0031：document 用
   `SourceRecord.published_at` 否则 `acquired_at`（**绝不用 reporting_period_end**）；
   macro 用 `MacroDatasetSnapshot.fetched_at`（**绝不用 normalized_period_start**）。

9. **LLM 抽象 + 生产适配器**：`MacroAnalysisModel` Protocol；`FakeMacroAnalysisModel`
   （自动化测试，0 真实 LLM）；`DeepSeekMacroAnalysisModel` = 懒加载 `ChatDeepSeek`
   + `with_structured_output(MacroAnalysisDecision)`，temperature=0.0 + **显式关闭
   thinking**（`extra_body={"thinking": {"type": "disabled"}}`——V4 Flash 默认
   thinking，temperature=0 不等于关闭；无 `reasoning_content`），只启用
   structured-output、不绑定 tools / web search；`model_id = provider:model`；
   `OutputParserException → MalformedOutput`、其余 → `ModelUnavailable`。冻结身份：
   `MACRO_ANALYST_NAME="structured_macro_context_analyst"`、
   `MACRO_ANALYST_VERSION=1`、analysis_domain=macro；persisted
   `analyst_model_id`（=`deepseek:deepseek-v4-flash`）一并落库。`factory.py`
   `create_macro_analysis_model` 按 `llm_provider` 分发（未知 → `UnsupportedLLMProviderError`）。

10. **Migration 0025 downgrade guard（数据安全）**：仅当不存在 (a) 任何
    `transmission_schema_version >= 3` 的链 **且** (b) 任何
    `analysis_domain='macro' AND claim_schema_version >= 6` 的 Claim 时，才允许
    DROP 列 / CHECK / INDEX 回到 0024；否则显式拒绝（**不删除数据 / 不修改行 /
    不静默丢弃 cutoff provenance**），`alembic_version` 保持 0025。isolated 临时 PG
    验证空库降级成功 / v3 链拒绝 / v6 macro Claim 拒绝 / safe legacy v2/v5 降级
    成功且对象保留 / CHECK 拒绝 v3 链缺 cutoff。

## 后果

- **Gate 0 关闭**：`analysis_as_of` 成为查询列，Audit / Writer / API / Claim 检查
  可从 claim_id 直接拿到 cutoff；历史链保持 NULL（0024-era 语义），不反推。
- **版本边界收口**：v6/v3 是当前 schema；v5/v2/v4/v1 冻结；版本升级 = 新
  fingerprint = 新对象，历史数据不批量改写、replay 不误判损坏。
- **分析链路完整**：4C.1B 把 4C.1A 的 provenance 接上 LLM；Analyst 只做判断，
  确定性（alias / guard / ref resolution / 原子持久化）交给代码。
- **no-lookahead 延续**：LLM 分析沿用同一 availability 策略，任何 future evidence
  在调用 LLM 前即拒绝。
- **Alembic head = 0025**（Stage 4C 当前最新）。

## 明确不做（边界）

不实现 4C.2 Valuation（**不开始 4C.2**）；不实现 Claim Synthesis / Conflict /
Evidence Gap（4D）；不接 LangGraph 分析节点（4C.1B 提供 Service 层，未来由
LangGraph 顶层编排调用）；不生成 Report / DraftSection / ReviewIssue / Audit
（Stage 5）；不开放 Macro Analysis HTTP API；不改动 generic Claim schema；不批量
update 历史 rows；不自动删数字 / 不改写模型 statement / 不让第二个 LLM 修正；
不引入新的迁移反推规则；不记录 API key / 完整 prompt / reasoning_content / raw
provider response；不实现自动交易 / 技术分析 / 短期预测 / 买卖建议。
