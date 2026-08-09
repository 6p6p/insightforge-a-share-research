# ADR-0023：Generic Evidence Origin + Macro Evidence（阶段 3C.3A）

- 状态：已接受
- 日期：2026-08-09
- 决策人：InsightForge 项目

## 决策

1. **3C.3A 状态（四维）：implementation completed / automated tests completed / live acceptance = not required（不开放 Evidence HTTP 端点，宏证据也走确定性服务而非 HTTP）**。目标：在 Stage 4（Claim）之前完成 EvidenceCard 的 **origin 模型泛化**，使 Evidence 原子单元不再被 DocumentChunk 绑定，可承载"已确认与研究问题相关的宏观观测"。
   - **EvidenceCard 双 origin**：`origin_type ∈ (document_chunk, macro_observation)`。
     - `document_chunk`：3C.1/3C.2 的既有语义（chunk quote + 文档 provenance）。
     - `macro_observation`：直接引用 MacroObservation 的宏证据（**不是 Macro → fake DocumentChunk**，不经过 DocumentChunk / ParsedSource / Chroma / quote resolver）。
   - **单表单 namespace**：同一个 `evidence_card_id` 命名空间，**不拆两张表**。两种 origin 共享同一 EvidenceCard 身份、fingerprint / replay / 并发幂等、Repository 与 Reviewer 视角。

2. **Migration 0017（`alembic current` = 0017 head）**。
   - `origin_type VARCHAR(32) NOT NULL server_default 'document_chunk'` + 索引 `ix_evidence_cards_origin_type`；**旧 v1 document 行回填 `document_chunk`，不重算旧 fingerprint**（`evidence_schema_version` 保持 1，旧卡原样可读 / 可 replay）。
   - 新增三列（UUID NULL，FK RESTRICT `macro_observations` / `macro_dataset_snapshots` / `macro_series`，上游存在期间不级联删除）：`macro_observation_id` / `macro_snapshot_id` / `macro_series_id` + 索引 `ix_evidence_cards_macro_observation_id`。
   - 现有 document-specific 列改为允许 NULL：`source_id` / `parsed_source_id` / `chunk_set_id` / `chunk_id` / `quote_start` / `quote_end` / `quote_text` / `quote_sha256`。
   - 3 个新 CHECK：
     - `ck_evidence_cards_origin_type`：origin 枚举。
     - `ck_evidence_cards_origin_consistency`（conditional）：document_chunk → document provenance + quote 全 NOT NULL 且 macro_* 全 NULL；macro_observation → macro_* 全 NOT NULL 且 document provenance + quote 全 NULL。
     - `ck_evidence_cards_locator_refs_nonempty`：`jsonb_array_length(locator_refs) > 0`（两种 origin 的 locator 都非空，**不造 fake 文本**）。
   - `provider_key` / `authority_tier_snapshot` / `critical_claim_eligible_snapshot` 保持 NOT NULL：两种 origin 都从 Source Registry / Macro provenance **确定性获得**（macro 用 MacroDatasetSnapshot 的获取时快照，**不硬编码 World Bank tier**）。
   - **0017 downgrade guard**：存在任何 `origin_type='macro_observation'` 行时拒绝降级（恢复 document NOT NULL 会破坏 macro 行，不静默丢失 origin semantics）；无 macro 行时 document 行满足全部 NOT NULL，可安全降级（isolated 临时 PG 集成测试覆盖两条路径）。

3. **领域契约（`app/evidence/contracts.py`）**。
   - `EVIDENCE_SCHEMA_VERSION = 2`：v2 = 泛化 origin 模型。`compute_evidence_fingerprint` 的 canonical payload **加入 `origin_type`**（document fingerprint 敏感于 origin_type）。旧 v1 document 卡**不重算**；新 document 卡用 v2。
   - `EvidenceOrigin` StrEnum：`DOCUMENT_CHUNK` / `MACRO_OBSERVATION`。
   - `MacroEvidenceDraft`（frozen dataclass，与 EvidenceCardDraft 同哲学）：**只允许调用方提供语义输入**——company_id / research_question / macro_observation_id / evidence_statement / extractor_name / extractor_version / extractor_model_id（optional）/ extractor_confidence。**`evidence_type` 不是 draft 字段**（固定 `metric`）。调用方**不得提供**：value_numeric / is_missing / period / provider_key / snapshot_id / series_id / locator_refs / authority_tier_snapshot / critical_claim_eligible_snapshot / source_published_at / reporting_period_end / quote_* / evidence_fingerprint——全部由 Service 从真实 Macro provenance 确定性推导。
   - `build_macro_observation_locator(...)` → 单元素数组，entry `{"type":"macro_observation", provider_key, series_id, snapshot_id, observation_id, source_id, external_indicator_id, geography_code, frequency, period, normalized_period_start}`，确定性结构化 locator，**不造 fake 文本**。
   - `compute_macro_evidence_fingerprint(...)`：canonical JSON（sort_keys、紧分隔、UTF-8）+ SHA-256，payload 含 schema_version + origin_type + company_id / research_question / evidence_statement / evidence_type + macro_observation_id / macro_snapshot_id / macro_series_id + period / normalized_period_start / value_numeric（str 或 None）/ is_missing + provider_key / authority_tier_snapshot / critical_claim_eligible_snapshot + locator_refs + extractor 三件套；排除 evidence_id / created_at。相同 → replay；语义 / extractor version / 上游 snapshot 任一变化 → 新 fingerprint → 新卡、旧卡保留。

4. **Macro provenance 加载（`MacroEvidenceService.create_macro_card(draft)`）**。
   - 短 DB session 读真实链：**Company（由调用方当前研究上下文提供，Service 必须验证存在）** → MacroObservation → MacroDatasetSnapshot → MacroSeries → SourceProvider（Source Registry）→ SnapshotArtifact links → RawArtifact；链任一断裂 / 快照无 artifact 链接 → `EvidenceProvenanceIntegrityError`（**不自动修复**）。
   - `company_id` 由调用方上下文显式提供（Macro 不经过 SourceRecord，不沿用 document 的 "company_id 取自 SourceRecord" 规则），但 Service 显式校验 Company 存在。
   - **不读取 Chroma、不重新 Retrieval、无 LLM re-interpretation、无 quote resolver**：数值 / 周期 / provider 等全部来自真实 Macro 模型，Service 绝不重新解释数值。

5. **确定性派生（纯函数，不持有 DB 连接）**。
   - `provider_key` = MacroSeries.provider_key（FK 指向 SourceProvider，Source Registry 一致性）。
   - `authority_tier_snapshot` / `critical_claim_eligible_snapshot` = **直接复制 MacroDatasetSnapshot 的获取时快照**（不是硬编码 World Bank tier；测试 UPDATE snapshot tier=2 → 卡复制 2，证明取自 provenance）。
   - `locator_refs` = `build_macro_observation_locator`；`research_question_sha256` / `evidence_fingerprint`（macro variant，schema v2）。
   - `evidence_type` 固定 `metric`；quote / `source_published_at` / `reporting_period_end` 固定 NULL（macro 无 source record 发布语义）；document-specific 全 NULL。
   - 任何数值语义（value / period / is_missing）不进 draft，只作为 fingerprint 输入来自 observation 行——观测值变化 → 新 fingerprint → 新卡（同一 observation 修订语义）。

6. **Replay / 并发 / 修订**。
   - `create_or_get`（PG `ON CONFLICT(evidence_fingerprint) DO NOTHING RETURNING`，无进程锁），并发 → 1 卡。
   - **Replay integrity**：已有 fingerprint 时重新加载真实 provenance 并逐字段核实（origin_type / company_id / macro ids / research_question + sha256 / evidence_statement / evidence_type / locator_refs / provider_key / authority_tier_snapshot / critical_claim_eligible_snapshot / extractor 三件套 / schema_version / fingerprint）；任一损坏 → `EvidenceCardIntegrityError`，**不自动 repair**（修订 = 新 EvidenceCard）。
   - statement / extractor version 变化 → 新 fingerprint → 新卡、旧卡保留。

7. **Document 回归（零行为破坏）**：既有 `EvidenceCardService.create_card` **继续只处理 document_chunk origin**；3C.1/3C.2 全部语义（draft 输入防御、exact quote、locator projection、provenance load、replay 完整性、并发幂等、无 update API）原样保留。新 document 卡使用 schema v2（fingerprint 含 origin_type），既有 v1 卡不重算。**两种 origin 卡由不同 Service 独占创建**：document 只经 EvidenceCardService，macro 只经 MacroEvidenceService。

8. **测试**。
    - 单元（`tests/evidence/`，57 项 contracts 系列新增，其中 `test_macro_evidence_contracts.py` 30 项）：MacroEvidenceDraft 输入防御（trim、blank 拒绝、UUID/版本/枚举校验、model_id 归一化、**无 provenance/value 字段、无 evidence_type 字段**）、`build_macro_observation_locator` 确定性 / 敏感性、`compute_macro_evidence_fingerprint` 确定性 / 64-hex / 语义 / 宏身份 / value / provider 快照 / locator / origin_type / schema version 敏感性。
    - 集成（`tests/integration/test_macro_evidence_service.py`，12 项，真实 PG + MockTransport WorldBank 链路，零 Chroma / LLM）：创建 document-free 卡（origin_type=macro_observation、macro ids 正确、document provenance + quote 全 NULL、evidence_type=metric、structured macro locator、schema v2）；locator 回溯到 Observation/Snapshot/Series/Provider；**authority tier / critical eligibility 来自真实 provenance（UPDATE snapshot → 卡复制新值，证明不硬编码）**；要求 Company 已存在；replay / 并发 → 1；statement / extractor version 变化 → 新卡；corrupted provenance → `EvidenceProvenanceIntegrityError`；corrupted replay → `EvidenceCardIntegrityError`；**不创建 DocumentChunk / ChunkSet / ParsedSource / SourceRecord**；missing observation（is_missing=true）仍可登记。
    - migration 0017 guard（`tests/integration/test_migration_0017_downgrade_guard.py`，2 项，isolated 临时 PG）：A 升级 0016 → 种子 v1 document 链 → 升级 0017 → origin_type 回填 document_chunk、字段完整、schema v1、fingerprint 原样 → 无 macro 行可降级 0016、doc 行完整；B 升级 0017 → 种子真实 macro 链 + macro 卡 → 降级被 RuntimeError 拒绝、版本保持 0017、卡完整。

9. **Boundary**：不创建 Claim / ClaimEvidenceLink / Report / Audit；不接 LangGraph 顶层编排 / CrewAI；不自动 Retrieval / reranker / fact cross-check / second LLM judge；不开放 HTTP API；**不引入 LLM**（MacroEvidenceDraft 是显式服务输入，不自动调用 LLM 重新解释数值）；MacroObservation 本身不是 Evidence（只有经 `create_macro_card` 登记成 EvidenceCard 才算）。Alembic head = 0017。

## 后果

- EvidenceCard 从 document-bound 泛化为双 origin：**同一证据单元模型同时承载文档引证与宏观观测**，Stage 4 Claim 可跨 origin 引用，且都带确定性 fingerprint / replay / 并发幂等。
- Macro Evidence **不伪造文档路径**：无 fake DocumentChunk、无 fake quote、无 Chroma 记录；structured macro locator 让 Reviewer 直接回溯到 Observation / Snapshot / Series / Provider / RawArtifact。
- provider / authority / critical 快照全部来自真实 Macro provenance（Source Registry + 获取时快照），不硬编码 World Bank tier，审计口径与 document origin 一致。
- 旧 v1 document 卡不重算、不回写，schema 演进无损；document 路径行为与 3C.1/3C.2 完全一致（回归由集成测试证明）。
- 0017 downgrade guard 防止"恢复 document NOT NULL 破坏 macro 行"的静默数据丢失。

## 明确不做（边界）

不创建 Claim / ClaimEvidenceLink / Report / Audit；不接 LangGraph 顶层编排 / CrewAI；不做自动 Retrieval / reranker / fact cross-check / second LLM judge；不开放 HTTP API；不引入 LLM 自动解释宏数值（MacroEvidenceDraft 是显式语义输入）；不做 Macro → fake DocumentChunk；不拆 EvidenceCard 为两张表；不重算既有 v1 fingerprint。
