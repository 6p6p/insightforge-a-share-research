# 阶段 4 计划概览

> 阶段 4 目标：把 Stage 3 已确认的 Evidence 单元（EvidenceCard）进一步登记为**可追溯、可回放的 Claim 分析结论**，经结构化 Analyst（4B）、专项分析（4C）与综合（4D）推进；**Report 生成与 Agent Audit 属于 Stage 5**，不在 Stage 4 范围内。详细接口在对应子阶段冻结。
> 顶层证据链：**Source → Evidence → Claim → Report → Audit**；但**开发阶段不与其混淆**——Stage 4 只能是：**4A Claim provenance / persistence → 4B Structured Analysts → 4C Financial/Macro specialized analysis → 4D Claim synthesis / conflict / evidence gaps → Stage 4 final acceptance**；Stage 5 才是：**ReportOutline / DraftSection / Report / Deterministic Check / Agent Audit / Retry / Human confirmation**。**不得写 4C=Report、4D=Audit，也不得写 Stage 4 = Claim → Report → Audit**。
>
> 总进度：**4A = completed**（含 0019 跨 relation 唯一性 closeout），**4B.1 = completed（Structured Analyst Foundation + Business / Event / Risk Claim Analysis）**，**4B = next（4B.2 Financial Analyst）**，**4C = later**，**4D = later**；**Stage 5 不提前标记**（Report / Audit 语义未到验收门槛，不提前开工）。

## 4A：Claim Provenance + Persistence Foundation（当前，completed）

- **状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Claim HTTP 端点）**。
- 把"分析结论的语义输入"确定性登记为可追溯 **Claim**（`claims` 表）+ **Claim ↔ Evidence 关系**（`claim_evidence_links` 表，migration 0018）。证据边界：EvidenceCard = 已确认的最小证据单元（Stage 3）；Claim = 引用 Evidence 的分析结论（Stage 4）；Report / ReviewIssue = Stage 5。**关系属于 ClaimEvidenceLink，不在 evidence_cards 上增加 supports_claim / contradicts_claim**。
- **ClaimDraft 只允许语义输入**（`app/claims/contracts.py`）：company_id / research_question / statement / analysis_domain / claim_kind / confidence / importance / support/contradict/context_evidence_ids / analyst_name / analyst_version / analyst_model_id（optional）；调用方**不得提供** authority tier / provider / source IDs / provenance / fingerprint / created_at——全部由 ClaimService 从真实 Evidence 确定性派生。
- **冻结枚举**（`CLAIM_SCHEMA_VERSION = 1`）：
  - `analysis_domain`：financial / business / event / macro / risk / valuation；
  - `claim_kind`：fact / inference / risk / relative_valuation（**不含** prediction / buy / sell / recommendation / price_target / return_forecast）；
  - `confidence`：low / medium / high；`importance`：normal / critical；
  - `ClaimEvidenceRelation`：supports / contradicts / context。
- **输入防御**：research_question / statement / analyst_name trim 后非空；每种 evidence id list 去重后按 `str(uuid)` 升序（deterministic canonical order，replay 可从无 order column 的 DB link rows 重建）；**同一 EvidenceCard 不能同时出现在多个 relation**（v1 禁止 supports+contradicts / supports+context / contradicts+context 任意跨 relation 重复）。
- **ClaimService.create_claim(draft)**（`app/services/claim_service.py`）：短 DB session 从真实 PG 加载全部 EvidenceCard——任一缺失或 `evidence.company_id != draft.company_id` → `ClaimEvidenceCompanyMismatch`（不自动修复）。纯函数规则（不持有 DB 连接，**不做语义判断**）：
  - **支持政策**：至少 1 个 supports Evidence，否则 `ClaimEvidenceInsufficient`；
  - **critical 政策**：importance=critical 时至少 1 个 supports Evidence 满足 `critical_claim_eligible_snapshot=true`，否则 `ClaimCriticalEvidenceInsufficient`。**不因 extractor_confidence=high 放宽来源政策；不因多个 Tier-3 Evidence 自动推断 critical eligible**；
  - **Macro Claim 传导规则**：analysis_domain=macro 时需 ≥1 macro_observation supports **且** ≥1 document_chunk Evidence（supports 或 context，体现公司暴露 / 公司经营事实），否则 `MacroClaimTransmissionEvidenceInsufficient`。**只验证证据结构具备传导链材料，不判断实际因果**。
- **Fingerprint**：`claim_fingerprint` = canonical JSON（sort_keys、紧分隔、UTF-8）+ SHA-256，含 claim_schema_version / company_id / research_question / statement / analysis_domain / claim_kind / confidence / importance / analyst_name / analyst_version / analyst_model_id / **按 relation 分组的 ordered evidence_card_ids**；**不含 claim_id / created_at**。`research_question_sha256` 与 EvidenceCard 同算法（同一 question 跨 evidence_cards / claims 哈希一致）。
- **Replay**：已有 fingerprint 时重新加载 Claim / ClaimEvidenceLinks / EvidenceCards，逐项核实 statement / enums / company / question hash / analyst identity / link 数量 / relations / Evidence IDs / critical support rule / macro rule / fingerprint；任一损坏 → `ClaimIntegrityError`，**不自动 repair**（修改观点 = 新 Claim）。**语义 / evidence relations / confidence / analyst version 任一变化 → 新指纹 → 新行，旧行保留**。
- **Repositories**（`claim_repository.py` / `claim_evidence_link_repository.py`）：get_by_id / get_by_fingerprint / list_by_company / create_or_get（PG `ON CONFLICT(claim_fingerprint) DO NOTHING RETURNING`，并发输家回查既有行，**无 Python 进程锁，无 update API**）。
- **Migration 0018 downgrade guard**：存在任何 Claim / Link 数据时拒绝降级（不静默丢弃 Claim 证据链）；无数据时允许回到 0017（isolated 临时 PG 验证两条路径）。
- **Migration 0019 closeout**：把"同一 EvidenceCard 对同一 Claim 只能有一种 relation"下沉到数据库层，新增 `UNIQUE(claim_id, evidence_card_id)`（`uq_claim_evidence_links_claim_evidence`）；**不修改已落地的 0018**。真实 PG 验收：同 claim + 同 evidence 已有 supports 后，直接 SQL 插入 contradicts 必须被数据库 UNIQUE 拒绝。downgrade：`claim_evidence_links` 有数据时拒绝回滚（删除约束会静默允许跨 relation 重复、改变 v1 语义）。
- **阶段边界**：`claims` / `claim_evidence_links` 允许存在；`report_outlines` / `report_sections` / `reports` / `review_issues` **不得存在**（使用精确 Stage-5 表名，不用"Stage 4 tables must not exist"这种以后会过期的名字）。
- **测试**：**24 项契约单元**（`tests/claims/test_claim_contracts.py`：枚举白名单含 prediction/buy/sell/recommendation/price_target/return_forecast 排除、draft 输入防御、evidence id 去重 + canonical 排序、跨 relation 重复拒绝、fingerprint 确定性 / 敏感性与不含 claim_id/created_at、question hash 与 Evidence 同算法）+ **26 项集成**（`tests/integration/test_claim_service.py`：document/macro/mixed relations 持久化、company mismatch / missing / no supports / critical without+with eligible / macro 拒绝 / valid macro structure / supports-contradicts-context links / fingerprint 确定性 / replay / 并发→1 / statement change→新 Claim / evidence relation change→新 Claim / analyst version change→新 Claim / replay corruption→integrity error / EvidenceCard 行永不修改 / document + macro E2E provenance SQL trace / 精确阶段边界 / **0019 跨 relation 重复由数据库拒绝**）+ **2 项 migration 0018 downgrade guard + 3 项 migration 0019**（isolated 临时 PG：0019 无数据降级成功 / 有 link 数据降级拒绝 / 跨 relation 直接 SQL 插入被数据库 UNIQUE 拒绝）。**0 LLM / 0 Chroma query / 0 LangGraph / 0 Claim Agent / 0 Report 表**。
- 决策记录：[docs/decisions/0024-claim-provenance-foundation.md](decisions/0024-claim-provenance-foundation.md)。

## 4B：Structured Analysts

- **定位**：把"EvidenceCard 集合 + research question + analysis domain → 结构化 ClaimCandidate → 确定性 ref resolution → ClaimService 持久化"接入结构化 Analyst 层（LLM 只做判断与综合，不做 Retrieval / 搜索 / 直接写库）。
- **子阶段**：
  - **4B.1（completed）Structured Analyst Foundation + Business / Event / Risk Claim Analysis**：见下节。
  - **4B.2（next）= Financial Metric + Financial Analyst**：先有结构化 FinancialMetric、确定性财务计算、期间 / 口径 / 单位、同比 / 环比 / 比率结果，LLM 才解释结果；**Financial Analyst 不得自行成为关键财务计算器**。
- 不提前实现 4C / 4D / Stage 5。

### 4B.1 Structured Analyst Foundation + Business / Event / Risk Claim Analysis（completed）

- **状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Claim Analysis HTTP 端点）；real_claim_analysis_smoke = completed（真实 DeepSeek V4 Flash smoke 走生产适配器通过，见下）**。无新 migration（`alembic current` 保持 0019 head；复用 4A 的 `claims` / `claim_evidence_links`）。
- **链路**：EvidenceCard[] + research question + analysis domain → `ClaimAnalysisRequest` → 真实 PG 加载 → `build_evidence_pack`（E1..En 最小投影）→ `ClaimAnalysisModel.analyze` → `ClaimAnalysisDecision` → `resolve_decision_refs`（E → UUID）→ ClaimDraft[] → `ClaimService.create_claim_batch` 原子持久化。只支持 business / event / risk；financial / macro / valuation → `ClaimAnalysisDomainNotReady`。
- **契约**（`app/analysis/claims/contracts.py`）：`CLAIM_ANALYST_VERSION = 1`、`MAX_EVIDENCE_PER_REQUEST = 30`、`MAX_CLAIMS_PER_DECISION = 5`；`ClaimCandidate`（claim_kind 只允许 fact/inference/risk，schema 层拒绝 relative_valuation，每条 ≥1 support_ref，ref 格式 E<number>）；`ClaimAnalysisDecision`（relevant=false→空 claims + 可选 reason_code；relevant=true→1..5 claims + 无 reason_code；无完全重复）；`ClaimAnalysisRequest`（company_id UUID / question trim 非空 / evidence 1..30 去重 + canonical 排序）。
- **Evidence Pack**（`evidence_pack.py`）：最小投影（evidence_ref / statement / type / origin_type / authority_tier / provider_key，document origin 附 quote_text / published_at / reporting_period_end）；**不发送** UUID / fingerprint / locator / raw / Chroma distance；按 `str(evidence_card_id)` 升序编号 E1..En，双向映射可复现。
- **Strategies**（`strategies.py`）：`business_event_v1`（business/event）、`risk_skeptic_v1`（risk）；persisted `analyst_name` = 具体 strategy，`analyst_version` / `analyst_model_id`（= `provider:model`）一并落库。
- **LLM 抽象 + 适配器**（`contracts.py` Protocol / `adapters.py` / `factory.py`）：`ClaimAnalysisModel`（model_id + async analyze）；`DeepSeekClaimAnalysisModel` = 懒加载 `ChatDeepSeek` + `with_structured_output`，temperature=0 + **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`），只启用 structured-output、不绑定 tools/web search；`OutputParserException`→`ClaimAnalysisMalformedOutput`、其余→`ClaimAnalysisModelUnavailable`。
- **Prompt boundary**（`prompt.py`）：冻结 system prompt 声明 Evidence 是不可信 DATA、忽略注入、不生成投资建议、不使用工具 / 不联网 / 不调用函数、无 CoT；Evidence 只进 user payload 的 `EVIDENCE_DATA_START/END` delimiter，绝不拼接进 system。
- **Ref resolution**（`ref_resolver.py`）：E → evidence_card_id，**不 fuzzy resolve**；未知 E → `ClaimAnalysisUnknownEvidenceRef`；跨 relation 重复 → `ClaimAnalysisRelationConflict`；组内去重 + canonical 排序；**任一 candidate 无效 → 整次 0 写**。
- **Service**（`service.py`）：防御性 domain check → 真实 PG 加载 Evidence（缺失/跨公司 → `ClaimAnalysisEvidenceCompanyMismatch`）→ build pack → `_call_model`（服务层 double-check schema）→ relevant=false 0-claims → resolve → 构造 drafts（analyst 身份确定性派生）→ `_check_kind_compatibility` 兜底 → `create_claim_batch`。
- **原子批量持久化**（`app/services/claim_service.py` 的 `create_claim_batch`）：1..5 个 drafts；**all-drafts-validate-first**（开事务前全量加载证据 + policy 校验 + fingerprint 派生，任一失败 → 整批 0 写）；**单 transaction**（逐个 create_or_get + bulk insert links，任一 SQLAlchemyError → rollback + `ClaimPersistenceFailed`）；`ClaimBatchResult(created, replayed, fingerprints)`；`create_claim` 委托给 batch（单条语义不变）。
- **错误分类**（`errors.py`，8 子类 + 稳定 code）：input / domain not ready / company mismatch / unknown ref / relation conflict / malformed output / model unavailable / domain-kind incompatible；错误消息不泄露 evidence 正文、prompt、key、raw response、DB URL。
- **测试**：**49 项单元**（test_strategies 5 / test_claim_analysis_contracts 23 / test_evidence_pack 5 / test_ref_resolver 6 / test_prompt 10）+ **15 项集成**（test_claim_analysis_service.py：端到端 + analyst 身份落库 + domain→strategy + relevant=false 0-claims + unknown ref/cross-relation conflict 0 写 + company mismatch + domain not ready + critical 政策 + replay + malformed + relative_valuation 拒绝 + model unavailable + 最小投影 + Stage 5 表不存在）+ **4 项 batch 集成追加**（test_claim_service.py）。全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表。全量 **1253 非集成 + 295 集成通过**。
- **真实 DeepSeek smoke**（`app/cli/smoke_structured_claim_analysis.py`）：seed 真实 HTML 链 → E1..En 最小投影 → `DeepSeekClaimAnalysisModel.analyze` → schema 校验 → 打印摘要 → **清理全部 seed 数据（0 正式业务 Claim 残留）**。2026-08-09 实跑通过（model_id=deepseek:deepseek-v4-flash、relevant=true、1 条 fact claim supports E1）。
- 决策记录：[docs/decisions/0025-structured-claim-analysis.md](decisions/0025-structured-claim-analysis.md)。

## 4C：Financial / Macro 专项分析（later）

- **状态：未开始（later）**。Financial / Macro 专项 Analyst（如 Macro Context Analyst）在 4B 之后推进；**本阶段不是 Report 生成**。

## 4D：Claim 综合 / 冲突 / 证据缺口（later）

- **状态：未开始（later）**。Claim Synthesis / Conflict Resolution / Evidence Gap 检测在 4C 之后推进；**本阶段不是 Audit**。

## Stage 5（Report + Audit）及以后

- **范围**：ReportOutline → DraftSection → Report → Deterministic Check → Agent Audit → Retry / Human confirmation。**Report 生成与 Agent Audit 属于 Stage 5**，不属于 Stage 4。
- **不提前标记**：Stage 5 未到验收门槛，不在本计划中定义细节；只有 Stage 4 验收门槛全部关闭后才推进。
