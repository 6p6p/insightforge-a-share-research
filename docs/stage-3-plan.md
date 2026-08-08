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

## 3C：EvidenceCard（next，尚未开始）

- 把 Retrieval 命中的 Chunk + 原文定位封装为 EvidenceCard（可独立核对的证据单元）。
- 前置：3B 完成（已达成）。
- 后续：Claim（主张抽取）、Report（研报生成）、Audit（事实审核）属于 Stage 4 及以后。
