# ADR-0021：EvidenceCard Provenance（阶段 3C.1）

- 状态：已接受
- 日期：2026-08-09
- 决策人：InsightForge 项目

## 决策

1. **3C.1 状态（四维）：implementation completed / automated tests completed / live acceptance = not required（不开放 Evidence HTTP 端点）。本阶段把"已确认与研究问题相关的 DocumentChunk 片段"确定性登记为可追溯 EvidenceCard。无 LLM、无 Evidence Extractor Agent、无 Claim 创建。**
   - **证据边界**：RetrievalHit = 候选资料；EvidenceCard = 已确认与研究问题相关、有明确原文片段和 provenance 的原子证据；Claim = Stage 4。EvidenceCard **不含** `supports_claim` / `contradicts_claim`；语义字段命名为 `evidence_statement`（不用 `claim_text`）。

2. **Migration 0016：`evidence_cards` 表**。
   - `evidence_card_id` UUID PK；5 个 FK 全部 RESTRICT：`company_id→companies`、`source_id→source_records`、`parsed_source_id→parsed_sources`、`chunk_set_id→chunk_sets`、`chunk_id→document_chunks`；`provider_key` FK RESTRICT `source_providers`。
   - 16 个 CHECK：`quote_start>=0`、`quote_end>quote_start`、`evidence_type IN (fact/metric/event/statement/context)`、`extractor_confidence IN (low/medium/high)`、`extractor_version>=1`、`evidence_schema_version>=1`、所有 SHA（research_question_sha256 / quote_sha256 / evidence_fingerprint）匹配 `^[0-9a-f]{64}$`、`locator_refs jsonb_typeof='array'`、`btrim(...) <> ''`（research_question / evidence_statement / quote_text / provider_key / extractor_name）。
   - 6 个索引：`company_id` / `source_id` / `chunk_id` / `research_question_sha256` / `evidence_type` / `created_at`；`evidence_fingerprint` CHAR(64) UNIQUE。
   - **0016 downgrade guard**：`evidence_cards` 有行时降级 0015 被显式 `RuntimeError` 拒绝（数据安全防护）；空表允许降级删除表（isolated 临时 PG 集成测试覆盖两种路径）。

3. **领域契约（`app/evidence/contracts.py` + `app/evidence/errors.py`）**。
   - `EVIDENCE_SCHEMA_VERSION = 1`；`EvidenceType`（fact / metric / event / statement / context）；`EvidenceConfidence`（low / medium / high）。
   - `EvidenceCardDraft`（frozen dataclass）**只允许调用方提供语义输入**：research_question、evidence_statement、evidence_type、chunk_id、quote_start、quote_end、extractor_name、extractor_version、extractor_model_id（optional）、extractor_confidence。调用方**不得提供**：company_id / source_id / parsed_source_id / chunk_set_id / authority_tier / provider / source_published_at / reporting_period_end / locator_refs / quote_text / quote_sha256 / evidence_fingerprint——这些必须从 PG provenance + chunk 确定性推导。

4. **Exact quote 契约**：`quote_text = chunk.text[quote_start:quote_end]`，由程序生成，**不信任 caller / LLM**；`quote_text.strip()` 非空；越界 / 非法区间 → `EvidenceQuoteRangeError`。**绝不 normalize / 改写 / 摘要 / 自动纠错**（原文逐字节切片，含内部空白与标点）。`quote_sha256 = SHA-256(quote_text UTF-8)`。

5. **Evidence locator projection**：`project_evidence_locator_refs(chunk_text, locator_refs, quote_start, quote_end)`。
   - chunk text 由每个 ref 对应原 block slice 以 `"\n"` 连接；**必须验证 `sum(ref 段长) + separators == len(chunk_text)`**，不一致 → `EvidenceLocatorIntegrityError`（**不自动修复**）。
   - 按 `char_end - char_start` 重建每个 ref 在 chunk 内的 local span，再与 quote `[start, end)` 求交；只保存 quote 实际覆盖到的 refs，`char_start/char_end` 缩窄到原 ParsedBlock 对应字符范围；`locator` 原样保留（HTML：xpath/element_id；PDF：page_number/bbox）。

6. **Provenance load**：`EvidenceCardService.create_card(draft)` 从 `chunk_id` 真实加载 `DocumentChunk → ChunkSet → ParsedSource → SourceRecord → Company`，派生 company_id / source_id / parsed_source_id / chunk_set_id / provider_key / source_published_at / reporting_period_end / authority_tier_snapshot / critical_claim_eligible_snapshot；任一环节缺失 → `EvidenceProvenanceIntegrityError`。`company_id` 取自 SourceRecord.company_id（FK RESTRICT 保证公司存在，不单独加载 Company 行）。**不读取 Chroma、不重新 Retrieval**。

7. **Research question**：不新建表、不伪造 question UUID。`research_question` trim 后保留原文本；`research_question_sha256 = SHA-256(trim 后 UTF-8)`。

8. **Evidence semantics**：`evidence_statement` 是对 quote 的原子语义表达，**不是 Claim**；`evidence_type` 限定五类，**不加 prediction / recommendation / buy / sell / counter_evidence**。

9. **Confidence / reliability 分离**：`authority_tier_snapshot`（来源可靠性，来自 SourceRecord）≠ `extractor_confidence`（语义提取置信度）。`critical_claim_eligible_snapshot` 直接复制 SourceRecord；**不因 extractor_confidence=high 自动提升 critical_claim_eligible**。

10. **Fingerprint / replay**：`evidence_fingerprint` = canonical JSON（sort_keys、`separators=(",",":")`、ensure_ascii=False、UTF-8）→ SHA-256 hex。至少包含：evidence_schema_version + 5 ids + research_question / evidence_statement / evidence_type + quote_start / quote_end / quote_sha256 / locator_refs + provider_key / authority_tier_snapshot / critical_claim_eligible_snapshot / source_published_at / reporting_period_end + extractor_name / extractor_version / extractor_model_id / extractor_confidence；**排除 evidence_id / created_at**。同一完全相同 Evidence → replay 原 evidence_id；并发最终只 1 卡；语义 / quote / extractor version 任一变化 → 新卡、旧卡保留。

11. **Replay integrity**：已有 fingerprint replay 时重新加载真实 provenance，并核实 chunk text slice == quote_text、quote_sha256、locator projection、chunk/source/parsed IDs、provider、authority tier、critical eligibility、published / reporting period、evidence fingerprint；任一损坏 → `EvidenceCardIntegrityError`，**不自动 repair**。

12. **Repository**：`EvidenceCardRepository`（get_by_id / get_by_fingerprint / list_by_company / create_or_get）。`create_or_get` 用 PostgreSQL `ON CONFLICT(evidence_fingerprint) DO NOTHING RETURNING`，冲突回退 get_by_fingerprint；**无 Python process lock；不允许 update API**（修订 = 新卡）。

13. **测试**。
    - 单元（`tests/evidence/`，57 项）：contracts（draft 输入防御、**draft 无 provenance 字段**、question trim+sha256、枚举、fingerprint 确定性 / 语义 / quote / extractor / provenance 敏感性）、quote（精确切片、中文 code point 索引、越界 / 区间非法 / 空白拒绝、sha256）、locator（单 / 多 block、跨 `"\n"`、partial block slice、单 / 多页 PDF、确定性、长度 / 结构 integrity）。
    - 集成（`tests/integration/test_evidence_card_service.py`，19 项，真实 PG + 真实 SourceParsingService/ChunkingService，零 Chroma / LLM / embedding）：首建、replay、并发→1、statement / quote range / extractor version 变化→新卡、provenance snapshots、high confidence 不提升 critical eligibility、损坏 replay→integrity 不修复、**Repository 无 update API**、E2E HTML（DOM locator + card→chunk→chunk_set→parsed→source→company/artifact 完整回溯）、E2E HTML 跨 `"\n"` 双 locator、E2E PDF（page/bbox 跨页保留 + 回溯到 ParsedSourceBlock）、provenance 链断裂→integrity。
    - migration 0016 downgrade guard（isolated 临时 PG 库 `insightforge_gate_*`，2 项）：有卡拒绝降级且数据保留、空表允许降级删除表。

14. **Boundary**：不创建 Claim / Report / ReviewIssue；不调用 LLM / LangGraph / CrewAI / BGE / Chroma query；EvidenceCard **不是 RetrievalHit 的自动升级**——Service 构造函数只持有 sessionmaker，只显式接受 `create_card(EvidenceCardDraft)`；Alembic head = 0016。

## 后续演进（3C.3A）

本 ADR 冻结的 document origin（`EVIDENCE_SCHEMA_VERSION=1`、`alembic head=0016`）已被 [ADR-0023（Generic Evidence Origin + Macro Evidence，阶段 3C.3A）](0023-generic-evidence-origin-macro-evidence.md) 泛化：`evidence_cards` 增加 `origin_type ∈ (document_chunk, macro_observation)`（migration 0017，`EVIDENCE_SCHEMA_VERSION=2`），document-specific 列允许 NULL。**本文档的 document 语义不变**：既有 `EvidenceCardService.create_card` 仍只处理 document_chunk origin，draft 输入防御、exact quote、locator projection、provenance load、replay 完整性、并发幂等、无 update API 全部原样保留；新 document 卡使用 schema v2（fingerprint 加入 origin_type），既有 v1 卡不重算。

## 后果

- Evidence 原子单元落库，具备完整 provenance + 原文定位（DOM / 页面坐标），可**在不重跑 LLM 的情况下确定性 replay / 核对**。
- 语义 / quote / extractor version 变化 → 新卡与旧卡并存（可追溯演进，不静默覆盖）。
- fingerprint + PG `ON CONFLICT` 保证并发幂等（无进程锁）；损坏 replay 只抛错不修复，修复只能走显式重建。
- 为 3C.2 Evidence Extractor（把 RetrievalHit + 语义抽取接入 create_card）与 Stage 4 Claim 提供确定性的"已确认证据单元 + 原文定位"输入形态。

## 明确不做（边界）

不创建 Claim / Report / ReviewIssue；不调用 LLM / LangGraph / CrewAI / BGE / Chroma query；不做 RetrievalHit→Evidence 自动升级；不做自动修复 / 自动重建；EvidenceCardRepository 无 update API；不开放 Evidence HTTP 端点；不新建 research_question 表 / 不伪造 question UUID；不新增 prediction / recommendation / buy / sell / counter_evidence 类型。
