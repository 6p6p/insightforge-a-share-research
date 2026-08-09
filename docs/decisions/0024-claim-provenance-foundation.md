# ADR-0024：Claim Provenance + Persistence Foundation（阶段 4A）

- 状态：已接受
- 日期：2026-08-09
- 决策人：InsightForge 项目

## 决策

1. **4A 状态（四维）：implementation completed / automated tests completed / live acceptance = not required（不开放 Claim HTTP 端点，Claim 只经确定性 Service 登记）**。目标：在接入 LLM Analyst 之前，先建立 **Claim 的最小原子单元**——把"分析结论的语义输入"确定性登记为可追溯、可回放的 Claim，并把 Claim ↔ EvidenceCard 的关系显式持久化。Stage 3 的 EvidenceCard = 已确认的来源事实（"2025年海外收入同比增长31.4%"）；Stage 4A 的 Claim = 引用 Evidence 的分析结论（"海外业务是公司2025年收入增长的重要驱动因素"）。

2. **Migration 0018（`alembic current` = 0018 head）**。
   - **`claims` 表**：claim_id UUID PK；company_id UUID FK RESTRICT companies；research_question TEXT；research_question_sha256 CHAR(64)；statement TEXT；analysis_domain VARCHAR(32)；claim_kind VARCHAR(32)；confidence VARCHAR(16)；importance VARCHAR(16)；analyst_name VARCHAR(64)；analyst_version INTEGER；analyst_model_id VARCHAR(200) NULL；claim_schema_version INTEGER；claim_fingerprint CHAR(64) **UNIQUE**；created_at TIMESTAMPTZ。
   - **`claim_evidence_links` 表**：PK (claim_id, evidence_card_id, relation)；claim_id FK **CASCADE** claims（删 Claim 删 links）；evidence_card_id FK **RESTRICT** evidence_cards（证据存在期间 link 不静默消失）；relation VARCHAR(16) CHECK（supports / contradicts / context）；created_at TIMESTAMPTZ。
   - 关系属于 ClaimEvidenceLink，**不在 evidence_cards 上增加 supports_claim / contradicts_claim**。
   - CHECK：analysis_domain / claim_kind / confidence / importance / relation 枚举白名单、claim_schema_version ≥ 1、analyst_version ≥ 1、research_question / statement / analyst_name `btrim <> ''`、research_question_sha256 / claim_fingerprint `^[0-9a-f]{64}$`。
   - 索引：claims.company_id / created_at / research_question_sha256；claim_evidence_links.evidence_card_id / relation。
   - **0018 downgrade guard**：存在任何 Claim / Link 数据时拒绝降级（不静默丢弃 Claim 证据链）；无数据时允许回到 0017（isolated 临时 PG 集成测试覆盖两条路径，错误消息 `"claims/claim_evidence_links rows present"`）。
   - **Migration 0019 closeout（阶段 4A 收尾）**：把"同一 EvidenceCard 对同一 Claim 只能有一种 relation（supports / contradicts / context 中恰好一种）"下沉到数据库层强制——新增 `UNIQUE(claim_id, evidence_card_id)`（`uq_claim_evidence_links_claim_evidence`）；**不修改已落地的 0018**。此前该不变量只在 ClaimDraft 构造时约束，DB 层允许直接 SQL 写入互相矛盾的 relation；0019 之后由数据库直接拒绝。**0019 downgrade guard**：`claim_evidence_links` 有行时拒绝降级（删除约束会静默允许跨 relation 重复、改变 v1 语义，错误消息 `"claim_evidence_links rows present"`）；无数据时才允许回到 0018。

3. **领域契约（`app/claims/contracts.py`）**。
   - `CLAIM_SCHEMA_VERSION = 1`（冻结）。
   - 枚举：`ClaimAnalysisDomain`（financial / business / event / macro / risk / valuation）；`ClaimKind`（fact / inference / risk / relative_valuation——**不含** prediction / buy / sell / recommendation / price_target / return_forecast）；`ClaimConfidence`（low / medium / high）；`ClaimImportance`（normal / critical）；`ClaimEvidenceRelation`（supports / contradicts / context）。
   - `ClaimDraft`（frozen dataclass，与 EvidenceCardDraft / MacroEvidenceDraft 同哲学）：**只允许调用方提供语义输入**——company_id / research_question / statement / analysis_domain / claim_kind / confidence / importance / support_evidence_ids / contradict_evidence_ids / context_evidence_ids / analyst_name / analyst_version / analyst_model_id（optional）。调用方**不得提供**：authority tier / provider / source IDs / Evidence provenance / fingerprint / created_at——全部由 ClaimService 从真实 Evidence 确定性派生。
   - 输入防御：research_question / statement / analyst_name trim 后非空；analyst_version ≥ 1（必须是 int）；analyst_model_id 提供时 trim、空串 → None；evidence id list 必须全 UUID。
   - **Evidence id 归一化**：每种 list 去重后按 `str(uuid)` 升序排序（deterministic canonical order，与调用方提交顺序无关）——fingerprint 与 replay 全确定性，replay 可从无 order column 的 DB link rows 重建顺序。**同一 EvidenceCard 不能同时出现在多个 relation**（v1 禁止 supports+contradicts / supports+context / contradicts+context 任意跨 relation 重复 → `ClaimDraftError`）。
   - `compute_claim_fingerprint`：canonical JSON（sort_keys、紧分隔、UTF-8）+ SHA-256，payload 含 claim_schema_version / company_id / research_question / statement / analysis_domain / claim_kind / confidence / importance / analyst_name / analyst_version / analyst_model_id / **按 relation 分组的 ordered evidence_card_ids**（supports / contradicts / context）；**不含 claim_id / created_at**。同一完全相同 Claim → 同一指纹 → replay 同一行；statement / evidence relations / confidence / analyst version 任一变化 → 新指纹 → 新行，旧行保留（修改观点 = 新 Claim）。
   - `compute_research_question_sha256` 与 EvidenceCard 同算法（`SHA-256(trim 后 UTF-8)`）——同一 question 在 evidence_cards 与 claims 中哈希一致，便于追溯。

4. **ClaimService.create_claim(draft)**（`app/services/claim_service.py`）。
   - 短 DB session 从真实 PG 加载全部 EvidenceCard（supports ∪ contradicts ∪ context）：任一缺失或 `evidence.company_id != draft.company_id` → `ClaimEvidenceCompanyMismatch`（不自动修复）。
   - **纯函数规则（不持有 DB 连接，ClaimService 不做语义判断）**：
     - 支持政策：至少 1 个 supports Evidence，否则 `ClaimEvidenceInsufficient`；
     - critical 政策：importance=critical 时至少 1 个 supports Evidence 满足 `critical_claim_eligible_snapshot=true`，否则 `ClaimCriticalEvidenceInsufficient`。**不因 extractor_confidence=high 放宽来源政策；不因多个 Tier-3 Evidence 自动推断 critical eligible**；
     - Macro Claim 传导规则：analysis_domain=macro 时需 ≥1 macro_observation supports **且** ≥1 document_chunk Evidence（supports 或 context，体现公司暴露 / 公司经营事实），否则 `MacroClaimTransmissionEvidenceInsufficient`。**只验证证据结构具备传导链材料，不判断实际因果**。
   - 短 DB transaction：create_or_get（`INSERT ... ON CONFLICT(claim_fingerprint) DO NOTHING RETURNING`，无进程锁）→ 首次 created=True 时 bulk insert ClaimEvidenceLinks → commit；已有 fingerprint → replay 时重新加载 Claim / ClaimEvidenceLinks / EvidenceCards 并逐项核实（statement / enums / company / question hash / analyst identity / link 数量 / relations / Evidence IDs / critical rule / macro rule / fingerprint），任一损坏 → `ClaimIntegrityError`，**不自动 repair**。
   - **replay 期间来源政策失败包装**：replay 重新加载真实证据后重跑 critical / macro 政策规则；政策不再满足视为既有 Claim 数据损坏 → `ClaimIntegrityError`（不自动 repair）。初始创建时的政策错误仍抛各自专属错误。

5. **Repositories**（`app/repositories/claim_repository.py` / `claim_evidence_link_repository.py`）。
   - `ClaimRepository`：get_by_id / get_by_fingerprint / list_by_company（created_at ASC, claim_id ASC）/ create_or_get（ON CONFLICT(claim_fingerprint) DO NOTHING RETURNING，并发输家回查既有行 created=False）。
   - `ClaimEvidenceLinkRepository`：list_by_claim / bulk_insert / count_for_claim。
   - **无 update API**（修改观点 = 新 Claim = 新指纹 = 新行）；repository 不 commit（事务由 caller 协调）。

6. **测试**。
   - 单元（`tests/claims/test_claim_contracts.py`，24 项）：枚举白名单（claim_kind **不含** prediction/buy/sell/recommendation/price_target/return_forecast）、draft 输入防御（blank question/statement/analyst_name、analyst_version 0/负数/非 int、str 非 StrEnum、model_id 空白→None）、evidence id 去重 + canonical 排序 + 顺序无关、跨 relation 重复拒绝、supports 契约层允许空（"至少 1 个 supports"由 ClaimService 强制）、fingerprint 确定性 / statement/relation/confidence/analyst version/company 敏感性 / 不含 claim_id/created_at / 64-hex、question hash 与 Evidence 同算法。
   - 集成（`tests/integration/test_claim_service.py`，26 项，真实 PG + 真实 SourceParsingService/ChunkingService/MacroEvidenceService，零 Chroma/LLM/embedding）：document / macro / mixed relations 持久化；company mismatch / missing / no supports / critical without eligible / critical with eligible / macro 拒绝 / valid macro structure；supports-contradicts-context links；fingerprint 确定性 / replay / 并发→1 / statement change→新 Claim / evidence relation change→新 Claim / analyst version change→新 Claim；replay corruption（link 篡改、claim row 篡改）→ `ClaimIntegrityError`；无 update API；**EvidenceCard 行永不修改**；document + macro E2E provenance SQL trace；精确阶段边界（claims 表允许存在、Stage-5 report 表不存在）；**0019 跨 relation 重复由数据库拒绝**（同 claim + 同 evidence 已有 supports 后直接 SQL 插入 contradicts → IntegrityError）。
   - migration 0018 guard（`tests/integration/test_migration_0018_downgrade_guard.py`，2 项，isolated 临时 PG）：A 升级 0018 → 无数据降级 0017 成功、claims 表删除；B 升级 0018 → 真实服务链 seed Claim（SourceRecord → Parsing → Chunking → EvidenceCardService → ClaimService）→ 降级被 RuntimeError 拒绝、版本保持 0018、Claim + link 完整保留。
   - migration 0019 guard（`tests/integration/test_migration_0019_downgrade_guard.py`，3 项，isolated 临时 PG）：A 升级 0019 → 约束存在 → 无数据降级 0018 成功、约束移除；B 升级 0019 → seed Claim+link → 降级被 RuntimeError 拒绝、版本保持 0019；C **Gate 0A 核心**：同 claim + 同 evidence 已存在 supports 行后，直接 SQL 插入 contradicts 必须被数据库 UNIQUE 拒绝、不残留 contradicts 行、原 supports 行保留。
   - 全程 0 LLM / 0 Chroma query / 0 LangGraph / 0 Claim Agent / 0 Report 表。

7. **阶段边界（精确命名）**：Stage 4A 允许 `claims` / `claim_evidence_links` 存在；`report_outlines` / `report_sections` / `reports` / `review_issues` **不得存在**。既有 Stage-3 边界测试已改用精确的 Stage-5 表名（`test_zero_chroma_and_no_stage5_report_tables` / `test_no_stage5_report_tables`），避免"Stage 4 tables must not exist"这类随进度过期的名字。

## 后果

- **Claim 成为可追溯、可回放的分析结论单元**：任何 Claim 都能逐 link 回溯到 EvidenceCard → DocumentChunk / MacroObservation → SourceRecord / Snapshot → RawArtifact，审计口径与 Stage 3 Evidence 一致。
- **Claim 与 Evidence 的关系显式持久化**：supports / contradicts / context 三个 relation 由 `claim_evidence_links` 承载，不在 evidence_cards 上增加语义列；EvidenceCard 保持"来源事实"单一职责。
- **修改观点 = 新 Claim**：fingerprint 只由语义输入决定，statement / evidence relations / confidence / analyst version 任一变化 → 新指纹 → 新行，旧行保留；无 update API，历史观点不可变。
- **来源政策在持久化层强制执行**：critical 只认 `critical_claim_eligible_snapshot` 的真实快照，macro Claim 必须同时具备宏观支持与公司文档传导链证据；replay 时政策不满足 = 数据损坏，不静默放过。
- **为 4B（Structured Analysts）提供稳定输入形态**：Analyst 只需把"判断"作为语义输入交给 create_claim，provenance / 政策 / fingerprint / persistence 全部由确定性代码负责；4B.1 在此基础上以 `create_claim_batch` 原子登记 1..5 个 Claim + links。

## 明确不做（边界）

不调用 LLM、不接 Analyst Agent、不创建 Report / DraftSection / ReviewIssue、不接 LangGraph 分析节点；不做语义判断（statement 是否真的被 Evidence 支持由 LLM Analyst / later Auditor 判断）；不自动 repair 损坏的 Claim；不做 prediction / buy / sell / recommendation / price_target / return_forecast 类 claim_kind；不开放 HTTP API；不提前实现 4B / 4C / 4D；不提前标记 Stage 5。
