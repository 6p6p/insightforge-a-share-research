# ADR-0019：BGE Embedding + Chroma 向量索引基座（阶段 3B.1）

- 状态：已接受
- 日期：2026-08-08
- 决策人：InsightForge 项目

## 决策

1. **3B.1 状态（四维）：implementation completed / automated tests completed / docker rebuild acceptance completed / live acceptance = not required（本阶段不开放检索端点、无检索 read path；BGE 不作为 /ready 条件）。real_bge_acceptance = passed（2026-08-08，真实模型单次受控 smoke，见 §10）**。本阶段建立 **DocumentChunk → BGE Embedding → Chroma 向量索引**的确定性索引基座：PG manifest（`chunk_vector_indexes`，migration 0015）+ 固定共享 Chroma collection + 逐 chunk 向量记录。**不实现 RetrievalService / top-k API / threshold / reranker / EvidenceCard / Claim / Report / LLM / LangGraph 集成**——那些属于 3B.2 / 3C 及以后。

2. **Embedding 模型契约（`app/rag/embedding/contracts.py`）**。
   - 冻结 **BAAI/bge-small-zh-v1.5**：`dimension=512`、`normalize_embeddings=true`、`max_input_tokens=512`、query_instruction=`"为这个句子生成表示以用于检索相关文章："`（**仅 query 加，document 不加**）。
   - **immutable revision**：`BGE_MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"`（real BGE smoke 解析出的 commit hash，绝不依赖 moving "main"）。revision 未配置（None）时 provider 拒绝加载（`EmbeddingModelNotConfigured`），自动化测试使用 `FakeEmbeddingProvider`。
   - 向量契约：任何 embedding 必须 dimension=模型维度、分量全部 finite、L2 norm≈1（`EmbeddingContractError`），否则拒绝。
   - **禁止 silent truncation**：tokenized 长度（含 special tokens）> max_input_tokens → `EmbeddingInputTooLong`，不静默截断。

3. **模型依赖 / 加载（`app/rag/embedding/bge.py`）**。
   - sentence-transformers **精确 pin**（backend pyproject）：sentence-transformers 4.1.0 / transformers 4.57.6 / tokenizers 0.22.2 / torch 2.13.0+cpu（real smoke 记录，见 §10）。
   - **lazy load**：SentenceTransformer 首次调用时才 import 并加载，**不阻塞 app import / startup**，也不在启动时联网下载；加载线程安全（lock）。
   - `EmbeddingProvider` Protocol（embed_documents / embed_query / token_count / model_info），`BGEProvider` 与 `FakeEmbeddingProvider` 都实现它。

4. **Chroma 角色（PG = Source of Truth，Chroma = 可重建 derived index）**。
   - PostgreSQL：DocumentChunk + ChunkSet + provenance 全量、权威、不可静默重建。
   - Chroma：只存 `确定性 record id（str(chunk_id)）→ embedding → primitive metadata`，不含 chunk 正文、不含 locator_refs（locator 仍从 PG hydrate）。**允许 partial rows / 整体重建**。
   - 固定共享 collection `insightforge_document_chunks`（不按公司 / ChunkSet 拆），cosine distance（HNSW `space: cosine`，**不配置 embedding_function**——application 自己计算 embedding 显式传入）。
   - collection metadata 冻结键：`schema_version / model_id / model_revision / dimension / normalized / distance_metric`（`CHROMA_COLLECTION_SCHEMA_VERSION=1`）；同名 collection 配置不一致 → `VectorCollectionConflict`，**不覆盖既有 collection**（覆盖会掩盖配置漂移）。

5. **Chroma record metadata（每 chunk 一条，仅 primitive）**。含 `chunk_id / chunk_set_id / parsed_source_id / source_id / company_id / provider_key / document_type / chunk_ordinal / text_sha256 / authority_tier / critical_claim_eligible`；`published_at` 有值才额外存 `published_at_epoch`（int，NULL 不伪造）；**不塞 locator_refs**（nested JSON 不写 Chroma）。

6. **Index fingerprint（`app/rag/index/contracts.py::compute_index_fingerprint`）**。canonical JSON（`sort_keys + separators + ensure_ascii=False` + UTF-8）→ SHA-256，覆盖：`chunk_set_fingerprint`、`embedding_model_id`、`embedding_model_revision`、`embedding_dimension`、`normalize_embeddings`、`collection_name`、`collection_schema_version`、`distance_metric`。**不含 timestamps / DB ID / status / chunk 正文**。同一 ChunkSet + 同模型配置 → 同一指纹 → 重建命中同一 manifest。

7. **Migration 0015（Alembic head = 0015）**。新表 `chunk_vector_indexes`：
   - `vector_index_id` UUID PK、`chunk_set_id` FK **RESTRICT**、`embedding_model_id` / `embedding_model_revision`、`embedding_dimension`、`normalize_embeddings`、`collection_name`、`collection_schema_version`、`expected_chunk_count` / `indexed_chunk_count`、`index_fingerprint` CHAR(64) **UNIQUE**、`status`（building/ready/failed）、`last_error_code`、`created_at` / `ready_at`；
   - CHECK：fingerprint 64 hex、status 枚举、dimension ≥ 1、indexed ≤ expected、last_error_code 非空仅限 failed；
   - 自然身份 **UNIQUE(chunk_set_id, embedding_model_id, embedding_model_revision, collection_schema_version)**；
   - `chunk_sets` 补 **UNIQUE(parsed_source_id, chunker_name, chunker_version)**（源端身份，供自然身份对齐）。
   - **不修改 0014**；0015 的 downgrade 在 `chunk_vector_indexes` 有行时拒绝（数据安全防护）。

8. **VectorIndexService（`app/rag/index/service.py::index_chunk_set(chunk_set_id)`）**。
   - **短 DB session 读后关闭**：读 ChunkSet + provenance + ordered chunks，session 关闭后校验 `chunk_count==len(chunks)` 且 ordinal 连续（损坏 → `ChunkSetIntegrityError`，不自动修复）。
   - Embedding / Chroma 网络操作**不持有 DB transaction**；manifest 只做 create-or-get（自然身份 ON CONFLICT）。
   - **build / retry 分支**：兼容 collection（create-or-get + 冻结 metadata 逐 key 比对）→ 分批 upsert（确定性 id，`CHROMA_UPSERT_BATCH_SIZE=100`）→ 验证 expected chunk IDs + text_sha256；成功 → `ready` + `ready_at`；失败 → `failed` + 稳定 `last_error_code`（`stable_error_code` 映射，不泄露细节），然后 re-raise。
   - **ready replay 分支**：不重新 embedding、不重写 Chroma；先 `_verify_records` 验证 expected 记录，缺失/错误 → `VectorIndexIntegrityError`（`index_integrity_error`），**不在 retrieval read path 自动修复**（derived index partial 可接受，由显式重建收敛）。
   - **允许 Chroma partial rows**（`indexed_chunk_count` 如实记录）；`failed/building` 可重试（同自然身份 → 重置 building → 重建）。

9. **并发**：两进程并发 index 同一 ChunkSet → PG manifest=1（自然身份 ON CONFLICT 只 1 行）、Chroma 每 chunk=1（确定性 id upsert 幂等）、status=ready；**无 Python 进程锁**（集成测试并发 gather 验证）。

10. **real BGE smoke（Part 10，一次性真实模型测试）**。sentence-transformers 4.1.0 / transformers 4.57.6 / tokenizers 0.22.2 / torch 2.13.0+cpu；模型 **revision = `7999e1d3359715c523056ef9478215996d62a620`**（immutable commit hash，已回填 contracts）；max_seq_length=512；dim=512；document / query（含 instruction）L2 norm 均 = 1.000000；相同输入两次 encode 逐位一致（float32 确定性，stability=True）；query（加 instruction）≠ document（不加）；中文样本可 embed 且非零。**real_bge_acceptance = passed**。

11. **测试（自动化测试不下载真实模型，统一用 FakeEmbeddingProvider）**。
    - **单元测试**：embedding contracts（冻结 spec / query instruction / 向量契约 dimension-finite-norm / token 上限禁止静默截断 / Provider 协议）、BGEProvider（revision guard / lazy load / immutable revision 传递 / document 不加 instruction、query 加 / 超长拒绝）、vector index contracts（fingerprint 确定性 / 敏感性 / 排除 DB ID / collection 冻结 metadata / chunk metadata 字段）、Chroma manager fakes。
    - **集成测试**（真实 PostgreSQL，零网络）：VectorIndexService E2E（FakeEmbeddingProvider + FakeChromaManager）：happy path（manifest ready / indexed=expected / fingerprint 64 hex / metadata 证据链字段齐全）、metadata where company_id 过滤、ready replay 不重新 embed、embedding 失败 → manifest failed + 稳定错误码 → retry ready、Chroma record 被删 → `VectorIndexIntegrityError` 且 manifest 仍 ready（不自动修复）、并发单 manifest + 每 chunk 单 record、ChunkSetNotFound、chunk 被删 → `ChunkSetIntegrityError`、collection 配置冲突 → `VectorCollectionConflict` + manifest failed。
    - **真实 Chroma 集成测试**（真实 Chroma 127.0.0.1:8002）：独立测试 collection（uuid 后缀，结束删除）：roundtrip + 冻结 metadata 往返一致 + where company_id 过滤生效、replay 验证既有 records（replayed=True、同一 vector_index_id）。
    - 完整回归：**全部 non-integration + 全部 integration** 通过；ruff check 零告警、ruff format 全部格式化；`pip check` 通过；`alembic current` = 0015 head。

## 后果

- 3A 的 DocumentChunk 第一次获得可检索的确定性向量表示：同一 ChunkSet + 同 BGE 配置 → 同一 index_fingerprint → 同一 manifest（ready replay 不重嵌入、不重写 Chroma）。
- PG 仍是证据链 Source of Truth；Chroma 是可整体重建的 derived index（允许 partial），任何 Chroma 不一致不会静默污染证据链——read path 暴露 `VectorIndexIntegrityError`，修复走显式重建（本阶段无检索端点，read path 尚未建立）。
- `BGE_MODEL_REVISION` 已由真实 smoke 冻结为 immutable commit hash：生产 BGEProvider 可以实际加载模型（此前 revision=None 只允许 Fake 测试），且不依赖 moving "main"。
- **RetrievalService / top-k API / threshold / reranker 属于 3B.2**；**EvidenceCard 属于 3C**；本阶段不写死 similarity>0.5 之类规则。

## 明确不做（边界）

不实现 RetrievalService / top-k / threshold / reranker / EvidenceCard / Claim / Report / LLM / LangGraph 集成；不做批量历史索引回填（本阶段只提供 `index_chunk_set(chunk_set_id)` 服务能力，无调度入口）；不新增 HTTP 端点；BGE 不作为 `/ready` 检查条件（与 `/ready` 五项检查无关）；不下载/缓存真实模型到测试环境（自动化测试零模型下载）。
