# 阶段 3 计划概览

> 阶段 3 目标概览：把 ParsedSource 确定性快照变为**可检索的证据单元**（DocumentChunk → Embedding → EvidenceCard）。详细接口在对应子阶段冻结。
> 顶层证据链：**Source → Evidence → Claim → Report → Audit**；Stage 3 是 **Evidence** 的确定性前置，不进入 Claim/Report。

## 3A：Deterministic Document Chunking（当前，completed）

- **状态（2026-08-08）：implementation completed / automated tests completed / docker rebuild acceptance completed / live acceptance not required**。
- ParsedSource + ordered ParsedBlocks → ChunkSet + DocumentChunk 确定性分块快照（`chunk_sets` + `document_chunks` 表，migration 0014）。
- `block_window` chunker v1（`CHUNKER_NAME="block_window"` / `CHUNKER_VERSION=1`）：字符窗口 `target_chars=400` / `max_chars=500` / `overlap=0`，严格按 block.ordinal、尽量合并完整 block、`"\n"` 连接、不超过 max；单 block > 500 按确定性句末标点（。！？!?；;）切分，无标点 hard split；**不删除重复文本、不跨 ParsedSource、chunk text 非空**。
- 每个 Chunk 保存 `locator_refs`：`{"block_ordinal":N,"char_start":S,"char_end":E,"locator":{原 ParsedBlock.locator}}`，char 索引相对原 block.text（Python `[start, end)`），**保证 Chunk → ParsedBlock locator → ParsedSource → SourceRecord → RawArtifact 完整回溯**。
- PDF（`pdf_page` 页面坐标）与 HTML（`html_dom` DOM 定位）使用同一 Chunk 模型，仅 locator type 不同。
- `chunk_set_fingerprint`（canonical JSON + SHA-256，排除 DB ID / created_at）驱动 **replay / 并发单集 / chunker version 敏感**（旧版本保留）；已有 ChunkSet 损坏 → `ChunkSetIntegrityError`，**不自动修复**。
- 本阶段**不创建 Chroma collection、不做 Embedding / Retrieval / EvidenceCard / LLM**。
- 决策记录：[docs/decisions/0018-deterministic-document-chunking.md](decisions/0018-deterministic-document-chunking.md)。
- 测试：38 项单元测试 + 11 项集成测试（E2E 回溯、replay、版本、并发、损坏、零修改上游）。

## 3B：BGE Embedding + Chroma indexing/retrieval

### 3B.1：BGE Embedding + Chroma Index Foundation（completed）

- **状态（2026-08-08）：implementation completed / automated tests completed / live acceptance not required；real_bge_acceptance = passed；latest_image_docker_acceptance = completed**（CPU-only build 成功产出新镜像 `9066b4c9150b`，2.24GB；Docker 使用 CPU-only BGE runtime：torch 2.13.0+cpu 从 PyTorch 官方 CPU index 预装，镜像内无 nvidia-* CUDA 运行时包；Stage 3B 全部达成）。
- 冻结 BGE 契约（`app/rag/embedding/contracts.py`）：**BAAI/bge-small-zh-v1.5**、dimension=512、normalize=true、max_input_tokens=512、query_instruction=`"为这个句子生成表示以用于检索相关文章："`（仅 query 加）；**immutable revision `7999e1d3359715c523056ef9478215996d62a620`**（real smoke 解析，不依赖 "main"）；禁止 silent truncation。
- 模型 lazy load（不阻塞 app startup / 不在启动时联网下载）；sentence-transformers 4.1.0 精确 pin。
- **PG = Source of Truth，Chroma = 可重建 derived index**（collection identity v2，3B.1 closeout）：collection name 由 embedding schema 指纹确定性推导（`compute_collection_name`）→ 共享 collection `insightforge_chunks_v2_<fp[:12]>`（schema_version=2）；cosine、不配置 embedding function；冻结 metadata（schema_version/model_id/model_revision/dimension/normalized/distance_metric），配置不一致 → `VectorCollectionConflict`；**model revision 变化 → 确定性新 collection + 新 manifest，旧 collection/manifest 保留**（无 revision-specific 分支）。
- **Migration 0015**：`chunk_vector_indexes` 表（vector_index_id PK、chunk_set_id FK RESTRICT、模型配置、expected/indexed count、index_fingerprint CHAR(64) UNIQUE、status building/ready/failed、last_error_code、ready_at、collection_name；自然身份 UNIQUE(chunk_set_id, model_id, model_revision, schema_version)）；`chunk_sets` 补 UNIQUE(parsed_source_id, chunker_name, chunker_version)。**0015 downgrade guard**：`chunk_vector_indexes` 有行时降级 0014 被拒（有 isolated 集成测试）。
- **VectorIndexService.index_chunk_set(chunk_set_id)**：短 DB session 读后关闭；Embedding/Chroma 网络操作不持 DB transaction；create-or-get manifest（自然身份 ON CONFLICT）→ 兼容 collection → 分批 upsert（确定性 id=str(chunk_id)）→ 验证 expected chunk IDs + text_sha256；成功 ready、失败 failed+稳定错误码；ready replay 先验证不重嵌入，缺失 → `VectorIndexIntegrityError` 不自动修复；允许 Chroma partial；并发 → PG manifest=1 + 每 chunk record=1（无进程锁）。
- Chroma record metadata 仅 primitive：chunk_id/chunk_set_id/parsed_source_id/source_id/company_id/provider_key/document_type/chunk_ordinal/text_sha256/authority_tier/critical_claim_eligible（published_at / reporting_period_end 有值才存 epoch）；不塞 locator_refs。
- 测试：单元（contracts/bge/fingerprint/collection name）+ 集成（真实 PG + FakeChroma 零网络）+ 真实 Chroma（独立测试 collection，结束删除）+ 0015 downgrade guard（isolated temp DB）；**不下载真实模型**（FakeEmbeddingProvider）。
- 决策记录：[docs/decisions/0019-bge-chroma-index-foundation.md](decisions/0019-bge-chroma-index-foundation.md)。

### 3B.2：Filtered Vector Retrieval（completed）

- **状态（2026-08-08）：implementation completed / automated tests completed / live acceptance not required**。
- **RetrievalService.retrieve(query)**：`RetrievalQuery → embed_query → Chroma filtered query → PG hydrate → RetrievalHit`。**纯 read path**：不自动 index_chunk_set、不 repair/write、不创建 collection（`get_collection`，缺失 → `RetrievalIndexNotReady`）。
- **RetrievalQuery**（`app/rag/retrieval/contracts.py`）：company_id 必填；query_text trim 后非空、≤1000 字符；top_k 默认 10（1..50）；可选 filters：source_ids / provider_keys / document_types / authority_tiers / critical_claim_eligible_only / published_from/to / reporting_period_from/to（时间 timezone-aware，from ≤ to）；**不支持任意用户自定义 Chroma where JSON**。
- **Eligible index selection（PG 侧，完整匹配）**：ready manifest + **embedding 配置完整匹配**（model_id / revision / dimension / normalize_embeddings / collection_name / collection_schema_version）+ **indexed == expected** + 当前 chunker（block_window v1）+ 当前 parser identity（html_dom v2 / pdf_layout v2）+ company_id + filters；为空 → `RetrievalIndexNotReady`。failed/building manifest、旧 chunker/parser、维度/归一化/collection 名不匹配、indexed<expected 一律排除。
- **Query embedding**：`embed_query(query_text)`（加 BGE query instruction）；禁止 silent truncation（超长抛 `EmbeddingInputTooLong`）。
- **Chroma filtered query**：where 至少含 `chunk_set_id $in eligible`（company 隔离白名单），再组合 filters 成单个 `$and`；n_results=top_k；只取 ids/metadatas/distances（**不用 documents 作为正文来源**）。
- **Collection metadata 校验（query 前）**：`get_collection` 后校验 name == 查询 collection 且冻结 metadata 键（schema_version/model_id/model_revision/dimension/normalized/distance_metric）与 `build_collection_metadata(spec)` 完全一致；不一致 → `RetrievalIndexIntegrityError`，**不继续 query、不自动修改 collection**。
- **PG hydrate + integrity**：按 chunk_id 批量 hydrate（DocumentChunk → ChunkSet → ParsedSource → SourceRecord provenance），保持 Chroma ranking 顺序；任何不一致（chunk 缺失 / metadata 或 text_sha256 不匹配 / chunk_set 不在 eligible / 重复 chunk_id / distance 非 finite / ids-metadatas-distances 长度不一致）→ `RetrievalIndexIntegrityError`，**不 skip / 不自动重建**。
- **Ranking**：只使用 Chroma cosine distance，升序（相似度降序）；**无 threshold / reranker / MMR / BM25 / LLM judge**；`distance` 只作检索诊断，不叫 confidence/probability；top_k 不足时返回实际命中数。
- **RetrievalHit** 是 read model（不落库，Alembic 保持 0015 head）：rank / chunk_id / chunk_set_id / parsed_source_id / source_id / company_id / text / distance / provider_key / document_type / source_title / source_url / published_at / reporting_period_end / authority_tier / critical_claim_eligible / chunk_ordinal / locator_refs。
- 测试：单元 37 项（contracts/validation/where builder/query instruction/token too long/no threshold/collection metadata 校验/重复 chunk_id/非 finite distance）+ 集成 20 项（真实 PG + FakeChroma 零网络：company 隔离 / provider / document_type / source_ids / authority / critical-only / published range / reporting period range / ready-only / failed+building 排除 / 旧 chunker+parser 排除 / 维度、normalize、collection 名不匹配、ready 但 indexed<expected 排除 / 全链路 hydrate / ranking / 篡改 metadata→integrity / Chroma 不可用→稳定错误 / read path 0 manifest）+ 真实 Chroma 1 项（独立 collection，结束删除）。
- 决策记录：[docs/decisions/0020-filtered-vector-retrieval.md](decisions/0020-filtered-vector-retrieval.md)。

## 3C：EvidenceCard（当前：3C.3A 完成）

### 3C.1：EvidenceCard Provenance（completed）

- **状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required**（不开放 Evidence HTTP 端点）。
- 把**已确认与研究问题相关的 DocumentChunk 片段**确定性登记为可追溯 EvidenceCard（`evidence_cards` 表，migration 0016）。证据边界：RetrievalHit = 候选资料；EvidenceCard = 已确认、有明确原文片段和 provenance 的原子证据；Claim = Stage 4。EvidenceCard 不含 supports/contradicts_claim，语义字段命名 `evidence_statement`。
- **EvidenceCardDraft 只允许语义输入**（research_question/evidence_statement/evidence_type/chunk_id/quote_start/quote_end/extractor_name/extractor_version/extractor_model_id?/extractor_confidence）；company_id/source_id/authority tier/provider/published time/locator_refs/quote_text/evidence_fingerprint 全部由 Service 从 PG provenance + chunk **确定性推导**。
- **Exact quote**：`quote_text = chunk.text[quote_start:quote_end]` 程序切片，不信任 caller/LLM，strip 后非空、越界 → `EvidenceQuoteRangeError`；绝不 normalize/改写/摘要/自动纠错。
- **Locator projection**：chunk text = 各 ref block slice 以 `"\n"` 连接；`sum(段长)+separators == len(chunk_text)` 破坏 → `EvidenceLocatorIntegrityError` 不修复；与 quote 求交只留实际覆盖的 refs，char 范围缩窄到原 ParsedBlock，locator 原样保留（HTML xpath/element_id；PDF page_number/bbox）。
- **Provenance load**：`create_card(draft)` 从 chunk_id 真实加载 DocumentChunk→ChunkSet→ParsedSource→SourceRecord→Company 派生全部快照；链损坏 → `EvidenceProvenanceIntegrityError`；不读取 Chroma、不重新 Retrieval。
- **Confidence/reliability 分离**：`authority_tier_snapshot` ≠ `extractor_confidence`；`critical_claim_eligible_snapshot` 直接复制 SourceRecord，不因 high confidence 自动提升。
- **Fingerprint / replay / 并发**：`evidence_fingerprint` = canonical JSON + SHA-256（含 schema_version + 5 ids + 语义 + quote + locator_refs + provenance 快照 + extractor 三件套，排除 evidence_id/created_at）；相同 → replay 原卡；并发 → 1 卡（PG `ON CONFLICT`，无进程锁）；语义/quote/extractor version 任一变化 → 新卡、旧卡保留。
- **Replay integrity**：replay 时重新加载真实 provenance 核实 quote 切片/quote_sha256/locator projection/各级 IDs/provider/快照/fingerprint；任一损坏 → `EvidenceCardIntegrityError`，不自动 repair；Repository 无 update API。
- **测试**：57 单元 + 19 集成（真实 PG + 真实 Parsing/Chunking，零 Chroma/LLM/embedding）+ 2 项 migration 0016 downgrade guard（isolated 临时 PG）。
- 决策记录：[docs/decisions/0021-evidence-card-provenance.md](decisions/0021-evidence-card-provenance.md)。

### 3C.2：Structured Evidence Extractor（completed）

- **状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Evidence HTTP 端点）；real_evidence_extractor_smoke = completed（2026-08-09，真实 DeepSeek V4 Flash smoke 走生产路径通过：provider=deepseek、request_model=deepseek-v4-flash、thinking 显式 disabled、relevant=true、1 item、quote 精确解析成功；一次性受控 smoke，见 ADR-0022 §13）**。
- 把 `RetrievalHit + research question` 经 LLM 结构化语义抽取接入 `EvidenceCardService.create_card(draft)`。角色边界：**Extractor 只做语义**（相关性、原子 evidence_statement、evidence_type、low/medium/high confidence、逐字 quote_text）；确定性代码负责 quote_start/end、locator、provenance IDs、authority tier、critical eligibility、fingerprint、Claim、投资建议；Extractor 不调用 RetrievalService / 不重新检索 / 不读 Chroma。
- **契约**（`app/evidence/extractor/`）：`EVIDENCE_EXTRACTOR_NAME="structured_llm"`、`EVIDENCE_EXTRACTOR_VERSION=1`、`MAX_EXTRACTION_ITEMS_PER_HIT=3`；`EvidenceExtractionItem` + `EvidenceExtractionDecision`（relevant=false→items 空；true→1..3 且 reason_code=None；无完全重复 item；无 reasoning/CoT 字段）。
- **LLM 抽象**：最小 `EvidenceExtractionModel` Protocol（model_id + async extract）；自动测试用 `FakeEvidenceExtractionModel`；可选 `LangChainStructuredOutputAdapter`（lazy import，langchain 非必需依赖；model_id 不伪造 revision）；temperature=0，禁止 tools/web search。
- **Prompt 边界**：system/data 分离（source 只在 user payload 的 `<<<SOURCE_TEXT_START/END>>>` 内）；system 冻结声明 source 不可信 DATA、忽略注入、无 tools/CoT、quote 逐字、statement 由 quote 支持、不补充 source 外事实、不投资建议、不输出 Claim、无直接证据→relevant=false。
- **Exact quote resolver**：`resolve_exact_quote` 精确子串，0 次→NotFound、>1 次（含重叠）→Ambiguous；不做 fuzzy/normalize/自动纠错；LLM 不返回 offsets。
- **ExtractionService**：短 DB read + stale guard（hash + 5 ids，LLM 前拒绝）→ strict schema → relevant=false 0 写 → 全部 items 先完成 quote+decode 校验再逐 draft `create_card`（单 hit ≤3 卡；replay/并发由 3C.1 fingerprint 保证）；quote 以 fresh PG text 为准；日志仅白名单字段。
- **错误分类**：Unavailable / MalformedOutput / QuoteNotFound / QuoteAmbiguous / InputStale / InputError。
- **测试**：122 单元（contracts/quote/prompt 注入边界/service；零 LLM）+ 11 集成（真实 PG：E2E HTML DOM locator + PDF page/bbox、rerun replay、stale 0 写、relevant=false/not-found/ambiguous/malformed 0 写、high confidence 不提升 critical、单 hit 3 卡、0 manifest 无 Stage 4 表；零 Chroma/BGE/LLM/network）。
- 决策记录：[docs/decisions/0022-structured-evidence-extraction.md](decisions/0022-structured-evidence-extraction.md)。

### 3C.3A：Generic Evidence Origin + Macro Evidence（completed）

- **状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required**（不开放 Evidence HTTP 端点，宏证据走确定性服务）。
- 把 EvidenceCard 泛化为**双 origin**（`origin_type ∈ document_chunk / macro_observation`），在 Stage 4（Claim）前完成 origin 模型泛化。**不是 Macro → fake DocumentChunk**：macro Evidence 不经过 DocumentChunk / ParsedSource / Chroma / quote resolver。单表单 namespace（同一 evidence_card_id），不拆两张表。
- **Migration 0017**（`alembic current` = 0017 head）：`origin_type` NOT NULL server_default 'document_chunk' + 索引（旧 v1 document 行回填 document_chunk，**不重算旧 fingerprint**）；macro_* 三列 UUID NULL（FK RESTRICT macro_observations / macro_dataset_snapshots / macro_series）+ 索引；document-specific 列改可 NULL；3 个新 CHECK（origin 枚举、conditional origin_consistency、`locator_refs` 非空 array）；`provider_key` / `authority_tier_snapshot` / `critical_claim_eligible_snapshot` 保持 NOT NULL。**0017 downgrade guard**：有 macro_observation 行拒绝降级，无 macro 行可安全降级。
- **MacroEvidenceDraft**（`app/evidence/contracts.py`）：只允许语义输入（company_id/research_question/macro_observation_id/evidence_statement/extractor_name/extractor_version/extractor_model_id?/extractor_confidence）；evidence_type 固定 metric（不是 draft 字段）；调用方**不得提供** value/period/provider/snapshot/series/locator/authority tier/quote/fingerprint。
- **MacroEvidenceService.create_macro_card(draft)**（`app/services/macro_evidence_service.py`）：真实 provenance load（Company → Observation → Snapshot → Series → Provider → Artifact links → RawArtifact，链断裂 → `EvidenceProvenanceIntegrityError` 不修复）→ 纯函数派生（provider_key 来自 MacroSeries；authority_tier_snapshot / critical_claim_eligible_snapshot **直接复制 MacroDatasetSnapshot 获取时快照，不硬编码 World Bank tier**；deterministic structured macro locator；evidence_type=metric；quote/published/reporting period 固定 NULL）→ create_or_get（PG `ON CONFLICT(evidence_fingerprint)` 并发幂等）→ replay 逐字段校验（损坏 → `EvidenceCardIntegrityError` 不 repair）。**无 LLM、无 Chroma、无 DocumentChunk、无 quote resolver**。
- **Fingerprint schema v2**：`EVIDENCE_SCHEMA_VERSION = 2`；document + macro fingerprint payload 都加入 `origin_type`；旧 v1 document 卡不重算。`compute_macro_evidence_fingerprint` 含宏身份 / period / value / is_missing / provider 快照 / locator。
- **Document 回归**：既有 `EvidenceCardService.create_card` 继续只处理 document_chunk origin；3C.1/3C.2 全部语义原样保留（回归由集成测试证明）。
- **测试**：30 项宏契约单元 + 12 项宏证据集成（真实 PG + MockTransport WorldBank，零 Chroma/LLM）+ 2 项 migration 0017 downgrade guard（isolated 临时 PG）。
- 决策记录：[docs/decisions/0023-generic-evidence-origin-macro-evidence.md](decisions/0023-generic-evidence-origin-macro-evidence.md)。

### 3C.3：Evidence 服务收口 + Stage 3 Final Acceptance（completed）

- **状态（2026-08-09）：Stage 3 Final Acceptance completed**。本轮收口包含：DeepSeek V4 Flash 运行时迁移（Gate A/B）、真实 LLM smoke（Gate D，completed）、HTML/PDF/Macro 三链 E2E（Gate E）、acceptance 不变量验证（Gate F）、全量验证（Gate G）。
- 迁移：DeepSeek 官方已停止 legacy model names（deepseek-chat / deepseek-reasoner）；当前统一 `LLM_PROVIDER=deepseek`、`LLM_MODEL=deepseek-v4-flash`（model_id = `deepseek:deepseek-v4-flash`，不伪造 revision）。生产 adapter 显式 `extra_body={"thinking": {"type": "disabled"}}` 关闭 thinking（V4 Flash 默认 thinking；`temperature=0` 不等于关闭；`thinking` 非标准 OpenAI 参数按 langchain-deepseek==1.1.0 公开接口经 `extra_body` 传递）。
- 真实 LLM smoke（恰好 1 次，生产路径）：provider=deepseek、request_model=deepseek-v4-flash、thinking disabled、relevant=true、item_count=1、`resolve_exact_quote` 成功；**不记录** API key / 完整 prompt / reasoning_content / provider raw response；认证失败立即停止不重试、临时故障最多额外重试 1 次。
- 全量验证：pip check ✓；ruff check / format --check ✓；1180 非集成 + 242 集成测试全部通过；Alembic current = 0017 (head)；`docker compose up -d --force-recreate backend` 后容器 healthy，`/health/live` 200、`/health/ready` 200（configuration/database/chroma/checkpoint/raw_storage 全 ok）；**LLM 不加入 /ready**。
- Stage 3 完成单元：3A / 3B.1 / 3B.2 / 3C.1 / 3C.2 / 3C.2.1 / 3C.3A / Stage 3 Final Acceptance 全部 completed。**不开始 Stage 4（Claim）**。

### 3C 之后

- Stage 4 已开始：**4A = Claim Provenance + Persistence Foundation（current/completed）**；4B = next；4C = later；4D = later；Stage 5 不提前标记。详见 [docs/stage-4-plan.md](stage-4-plan.md)。
