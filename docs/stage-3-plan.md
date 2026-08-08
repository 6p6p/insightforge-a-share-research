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

### 3B.1：BGE Embedding + Chroma Index Foundation（当前，completed）

- **状态（2026-08-08）：implementation completed / automated tests completed / docker rebuild acceptance completed / live acceptance not required；real_bge_acceptance = passed**。
- 冻结 BGE 契约（`app/rag/embedding/contracts.py`）：**BAAI/bge-small-zh-v1.5**、dimension=512、normalize=true、max_input_tokens=512、query_instruction=`"为这个句子生成表示以用于检索相关文章："`（仅 query 加）；**immutable revision `7999e1d3359715c523056ef9478215996d62a620`**（real smoke 解析，不依赖 "main"）；禁止 silent truncation。
- 模型 lazy load（不阻塞 app startup / 不在启动时联网下载）；sentence-transformers 4.1.0 精确 pin。
- **PG = Source of Truth，Chroma = 可重建 derived index**：固定共享 collection `insightforge_document_chunks`、cosine、不配置 embedding function；冻结 metadata（schema_version/model_id/model_revision/dimension/normalized/distance_metric），配置不一致 → `VectorCollectionConflict`。
- **Migration 0015**：`chunk_vector_indexes` 表（vector_index_id PK、chunk_set_id FK RESTRICT、模型配置、expected/indexed count、index_fingerprint CHAR(64) UNIQUE、status building/ready/failed、last_error_code、ready_at；自然身份 UNIQUE(chunk_set_id, model_id, model_revision, schema_version)）；`chunk_sets` 补 UNIQUE(parsed_source_id, chunker_name, chunker_version)。
- **VectorIndexService.index_chunk_set(chunk_set_id)**：短 DB session 读后关闭；Embedding/Chroma 网络操作不持 DB transaction；create-or-get manifest（自然身份 ON CONFLICT）→ 兼容 collection → 分批 upsert（确定性 id=str(chunk_id)）→ 验证 expected chunk IDs + text_sha256；成功 ready、失败 failed+稳定错误码；ready replay 先验证不重嵌入，缺失 → `VectorIndexIntegrityError` 不自动修复；允许 Chroma partial；并发 → PG manifest=1 + 每 chunk record=1（无进程锁）。
- Chroma record metadata 仅 primitive：chunk_id/chunk_set_id/parsed_source_id/source_id/company_id/provider_key/document_type/chunk_ordinal/text_sha256/authority_tier/critical_claim_eligible（published_at 有值才存 epoch）；不塞 locator_refs。
- 测试：单元（contracts/bge/fingerprint/collection）+ 集成（真实 PG + FakeChroma 零网络 9 项）+ 真实 Chroma 2 项（独立测试 collection，结束删除）；**不下载真实模型**（FakeEmbeddingProvider）。
- 决策记录：[docs/decisions/0019-bge-chroma-index-foundation.md](decisions/0019-bge-chroma-index-foundation.md)。

### 3B.2：Retrieval（next，尚未开始）

- 对 3B.1 的向量索引实现语义检索：给定查询 → 返回候选 Chunk 及其证据链定位。
- 前置：3B.1 完成（已达成）。
- 不在本阶段实现的边界：EvidenceCard / Claim / Report / LLM。

## 3C：EvidenceCard（later，尚未开始）

- 把 Retrieval 命中的 Chunk + 原文定位封装为 EvidenceCard（可独立核对的证据单元）。
- 前置：3B 完成。
- 后续：Claim（主张抽取）、Report（研报生成）、Audit（事实审核）属于 Stage 4 及以后。
