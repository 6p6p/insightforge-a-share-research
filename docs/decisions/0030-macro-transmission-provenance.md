# ADR-0030：Macro Transmission Provenance Foundation（阶段 4C.1A）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 决策

1. **4C.1A 状态：implementation completed / automated tests completed / live acceptance not required（不开放 Macro Claim HTTP 端点）**。4C.1A 建立 Macro Claim 的传导 provenance 基础：**Macro Evidence + Company Exposure Evidence → Macro Transmission Chain → Macro Claim**。它不是把 macro 事实直接 Claim 化，而是登记"宏观变量如何传到公司"的**传导分析产物**，使 Audit 可回溯。**0 LLM / 0 DeepSeek call / 0 Chroma query / 0 Retrieval / 0 LangGraph / 0 Valuation / 0 Claim Synthesis / 0 Report / 0 Audit**；真实 LLM 不进入本阶段自动化测试。

2. **架构裁决：不是 Macro Evidence → Claim，而是三跳链**。`MacroClaimDraft` 必须同时提供 **macro_driver**（宏观驱动，origin_type=macro_observation）与 **company_exposure**（公司暴露，origin_type=document_chunk）两类证据，外加可选 **observed_effect**；持久化为 `Claim → MacroTransmissionChain → {macro Evidence, company exposure Evidence, observed effect Evidence}`，再各自由既有 provenance 链回溯到 `MacroObservation / SourceRecord → SourceProvider → RawArtifact`。单条 macro 事实或单条公司事实都不能独立支撑"宏观变化导致公司影响"——它们只作为 **context** 展开进 ClaimEvidenceLinks，传导语义由 MacroTransmissionChain 承载。

3. **核心边界（Transmission 不是 EvidenceCard）**：Macro Evidence = 来源支撑的宏观事实（World Bank 真实 provenance 登记的 EvidenceCard）；Company Exposure Evidence = 来源支撑的公司事实（document_chunk EvidenceCard）；**Macro Transmission = 分析产物**（利率 → financing channel → 公司有息负债 → 融资成本压力），是独立持久化的分析链，**禁止伪装成 EvidenceCard / 来源事实**。`MacroClaimService` 只接受 `MacroClaimDraft`，不接受 Macro Evidence 直接 Claim 化。

4. **Migration 0023（`macro_transmission_chains` + `macro_transmission_evidence_links`）**：
   - **macro_transmission_chains**：`transmission_id` PK；`claim_id` FK claims **CASCADE** + **UNIQUE**（一个 Macro Claim 至多一条链）；`company_id` FK companies **RESTRICT**；`channel_type` CHECK ∈ revenue/cost/financing/demand/supply_chain/trade_policy/operations/other（channel 描述宏观如何传到公司，**不是宏观变量本身**）；`effect_direction` CHECK ∈ tailwind/headwind/mixed/uncertain（**不是 buy/sell**）；`impact_status` CHECK ∈ plausible_impact/observed_impact；`time_alignment` CHECK ∈ aligned/uncertain（**无 misaligned**——证据明确错位时 Service 拒绝而非存 misaligned Claim）；`transmission_schema_version >= 1`；`transmission_fingerprint` CHAR(64) CHECK hex + **UNIQUE**（变更 → 新链，旧链保留）。
   - **macro_transmission_evidence_links**：`transmission_id` FK chains **CASCADE**；`evidence_card_id` FK evidence_cards **RESTRICT**（证据存在期间 link 不静默消失）；`role` CHECK ∈ macro_driver/company_exposure/observed_effect；PK(transmission_id, evidence_card_id, role)；**UNIQUE(transmission_id, evidence_card_id)**——同一证据对同一链只能一种 role；INDEX evidence_card_id。
   - **downgrade guard**：两表任一行存在时拒绝回滚（不静默丢弃传导 provenance）；空数据才允许回到 0022。

5. **v1 枚举（`app/claims/macro_contracts.py`）**：`MacroChannelType`（8 值：revenue/cost/financing/demand/supply_chain/trade_policy/operations/other）；`MacroEffectDirection`（tailwind/headwind/mixed/uncertain，**无 buy/sell**）；`MacroImpactStatus`（plausible_impact/observed_impact）；`MacroTimeAlignment`（aligned/uncertain，**无 misaligned**）；`MacroTransmissionRole`（macro_driver/company_exposure/observed_effect）。`MACRO_CLAIM_SCHEMA_VERSION = 4`、`MACRO_TRANSMISSION_SCHEMA_VERSION = 1`——与 generic v1 / financial v2-v3 分离，指纹含 schema_version 不 collision。

6. **`MacroClaimDraft`（独立 dataclass，不污染 generic ClaimDraft）**：字段含 `claim_kind`（**只允许 inference/risk**——macro 事实由 Macro Evidence 承载，Claim 只做判断）、`confidence`、`importance`、`channel_type`、`effect_direction`、`impact_status`、`time_alignment`、`analysis_as_of`、三类传导证据 id、三类 additional 证据 id、analyst 身份。**构造校验**：macro_driver ≥1、company_exposure ≥1、analysis_as_of 必须 date、statement trim 非空、**同一 Evidence 不能出现在任何两个传导角色**、**同一 Evidence 不能同时出现在传导与 additional**、**additional 的 supports/contradicts/context 之间互斥**、证据 id 去重 + str(uuid) canonical 排序。违反 → `MacroClaimDraftError`。

7. **Evidence 校验（真实 PG，不信任调用方）**：全部引用卡必须存在（缺失 → `MacroClaimEvidenceNotFound`）；全部卡 company == draft.company_id（跨公司 → `MacroClaimEvidenceCompanyMismatch`，additional 也不能绕过）；**按角色校验 origin**：macro_driver 必须 origin_type=macro_observation，company_exposure / observed_effect 必须 document_chunk（违反 → `MacroClaimOriginViolation`）。**不查 Chroma、不接受调用方提供的 provider/authority/provenance**——authority_tier / critical_claim_eligible 一律从卡的真实 provenance 快照复制。

8. **Temporal 边界（真实可用时间，不伪造缺失日期）**：macro 卡可用时间 = `MacroObservation.normalized_period_start`（其 source_published_at / reporting_period_end 恒 NULL）；document 卡 = `source_published_at`（优先）否则 `reporting_period_end`。任一卡已知可用时间晚于 `analysis_as_of` → `MacroClaimFutureEvidence`；每个 macro_driver / company_exposure 至少一个可用时间，无 → `MacroClaimTemporalEvidenceInsufficient`。**程序只验证 not-future / has-time**；lag reasonableness 由分析师判断；`time_alignment` **不自动猜测**（存 aligned/uncertain 原值）。

9. **Transmission role → ClaimEvidence 语义**：macro_driver / company_exposure / observed_effect 自动展开为 ClaimEvidenceLinks，**一律 relation=context**（单条证据不能独立证明因果，因果语义由 Chain 承载）；additional 保持调用方指定的 supports/contradicts/context。

10. **Critical policy**：`importance=CRITICAL` 需 ≥1 **eligible** macro_driver（`critical_claim_eligible_snapshot=true`）**且** ≥1 eligible company_exposure；`observed_impact` 时额外 ≥1 eligible observed_effect；否则 → `MacroClaimCriticalEvidenceInsufficient`。**additional support 不能替代两条传导腿**。

11. **Impact-status rule（overclaim 防御）**：`impact_status=observed_impact` 需 ≥1 observed_effect，否则 → `MacroClaimImpactStatusInsufficient`；`plausible_impact` 只需 macro_driver ≥1 + company_exposure ≥1。

12. **Fingerprint（canonical JSON + SHA-256）**：**transmission fingerprint** 含 transmission_schema_version / company_id / channel_type / effect_direction / impact_status / time_alignment / analysis_as_of / 三类 role-sorted `[{"evidence_card_id","evidence_fingerprint"}]`（证据稳定指纹，不伪造）；**EXCLUDES** transmission_id / created_at / claim_id。**macro claim fingerprint** 含 claim_schema_version=4 / company / research_question / analysis_as_of / statement / claim_kind / confidence / importance / analyst 身份 / transmission_fingerprint / additional 按 relation 的 evidence id 列表；**EXCLUDES** claim_id / created_at。

13. **Persistence（4 步、单短 transaction、0 partial write）**：`MacroClaimService.create_claim` = ①短 DB session 加载并校验全部 Evidence + Observation（随后关闭）→ ②纯函数派生（fingerprints + context expansion，无 DB）→ ③单短 PG transaction：`ClaimRepository.create_or_get`（ON CONFLICT(claim_fingerprint) DO NOTHING）→ 新建时 `MacroTransmissionRepository.create_or_get`（ON CONFLICT(transmission_fingerprint)；claim_id = 实际 claim_id；**新建 Claim 却复用了既有 transmission fingerprint → `MacroClaimIntegrityError`**）→ bulk insert transmission links + claim evidence links → commit。任一 `SQLAlchemyError` → 整条 rollback + `MacroClaimPersistenceFailed`（**无 compensating delete**）；同一 fingerprint 并发 → 最终 1 Claim + 1 Chain + 1 套 links。

14. **Replay integrity（不自动 repair）**：已有 fingerprint 时重新加载 Claim / Chain / 两类 links / Evidence / Observations，重新执行 origin / temporal / impact-status / critical 策略与派生，逐项核实 Claim 字段、claim evidence links 按 relation、Chain 字段、transmission links 按 role 与 transmission fingerprint；任一损坏 → `MacroClaimIntegrityError`，**不改动 historical generic v1 / financial v2-v3 Claims**。

15. **错误分类（`app/claims/macro_errors.py`，稳定 code）**：`MacroClaimError` 基类 + MacroClaimDraftError / MacroClaimEvidenceNotFound / MacroClaimEvidenceCompanyMismatch / MacroClaimOriginViolation / MacroClaimRelationConflict / MacroClaimFutureEvidence / MacroClaimTemporalEvidenceInsufficient / MacroClaimCriticalEvidenceInsufficient / MacroClaimImpactStatusInsufficient / MacroClaimIntegrityError / MacroClaimPersistenceFailed；错误消息不泄露 Evidence 正文 / provenance / key。

16. **测试（Contract / Origin / Temporal / Critical / Persistence / Semantics / E2E / Boundary）**：**16 项单元**（`tests/claims/test_macro_contracts.py`：draft 构造校验 / 角色与 additional 互斥 / 枚举白名单冻结（effect_direction 无 buy/sell、time_alignment 无 misaligned）/ transmission + claim fingerprint 确定性 + 语义敏感）+ **24 项集成**（`tests/integration/test_macro_claim_service.py`，真实 PG + 真实 WorldBankProvider(MockTransport) + 真实 HTML 服务链：创建（claim schema=4 / domain=macro / chain / transmission links role / context expansion）/ origin 按角色 / temporal（future → `MacroClaimFutureEvidence`、无时间 → `MacroClaimTemporalEvidenceInsufficient`）/ critical（缺 eligible 任一脚、additional 不能替代、observed_impact 缺 eligible effect）/ impact-status overclaim / replay 同 fingerprint 复用 + 并发 → 1 / 篡改 Claim / Chain → `MacroClaimIntegrityError` 不 repair / 公司隔离 / 缺失证据 0 写 / additional relation 原样 / time_alignment 不自动猜测 / E2E provenance（Chain → {macro 卡, doc 卡} → Observation/Source → Artifact）/ 边界（macro_transmission_* 存在、Stage 5 report 表不得存在、Service 只持有 sessionmaker））。全量 **1505 非集成 + 438 集成通过**。全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表。

17. **文档边界**：stage-4-plan.md / README 统一为 **4B.2 = FINAL（4B.2A/4B.2B/4B.2C.1/4B.2C.2 全部 completed）**、**4C.1A completed（Macro Transmission Provenance）**、**4C.1B next（Structured Macro Context Analyst）**、**4C.2 later（Valuation）**、**4D later（Claim Synthesis / Conflict / Evidence Gap）**、Stage 5 = Report + Audit（不提前标记）。Alembic head = 0023。

## 后果

- **Macro Claim 可回溯到来源**：MacroClaim → MacroTransmissionChain → {Macro Evidence, Company Exposure Evidence, Observed Effect} → (MacroObservation|SourceRecord) → SourceProvider → RawArtifact，传导分析产物独立持久化、不污染 Evidence。
- **边界清晰**：宏观事实与公司事实都是 Evidence；传导是分析产物；Claim 只做判断（inference/risk）。effect_direction 不用 buy/sell，time_alignment 不存 misaligned——避免把研究判断伪装成确定性。
- **0 partial write / 无 compensating delete**：任一校验失败 → 整次 0 写；同 fingerprint 并发 → 1 Claim + 1 Chain。
- **Replay 强一致**：篡改 → `MacroClaimIntegrityError`，不自动修复；不影响 historical generic v1 / financial v2-v3 Claims。

## 明确不做（边界）

不实现 4C.1B Structured Macro Context Analyst（LLM 解释宏观传导）；不实现 Valuation（4C.2）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不接 LangGraph 分析节点；不生成 Report / DraftSection / ReviewIssue / Audit（Stage 5）；不开放 Macro Claim HTTP API；不实现自动交易 / 技术分析 / 短期预测 / 买卖建议；**不开始 4C.1B**。
