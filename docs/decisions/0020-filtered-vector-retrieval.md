# ADR-0020：Filtered Vector Retrieval（阶段 3B.2）

- 状态：已接受
- 日期：2026-08-08
- 决策人：InsightForge 项目

## 决策

1. **3B.2 状态（四维）：implementation completed / automated tests completed / live acceptance = not required（本阶段不开放检索 HTTP 端点，只有 service 层 read path）。本阶段建立 RetrievalQuery → query embedding → Chroma filtered query → PG hydrate → RetrievalHit 的语义检索 read path。不实现 EvidenceCard / Claim / Report / LLM / LangGraph 集成——EvidenceCard 属于 3C，Claim/Report 属于 Stage 4。**

2. **PG = Source of Truth，Chroma = 可重建 derived index（检索只读）**。Chroma 只返回 candidate `chunk_id + distance`，**绝不作为正文来源**（RetrievalHit.text 从 PG hydrate）；任何 Chroma 不一致 → `RetrievalIndexIntegrityError`，**不 skip / 不自动重建 / 不自动 repair / 不自动 index_chunk_set**。RetrievalService 使用 `get_collection`（**不创建 collection**），缺失 → `RetrievalIndexNotReady`。检索是 **纯 read path**：不写 PG（不新增 manifest、不更新任何行）、不写 Chroma。

3. **RetrievalQuery（`app/rag/retrieval/contracts.py`，构造时校验、frozen）**。
   - `company_id`（UUID）必填；`query_text` trim 后非空、≤1000 字符；`top_k` 默认 10、范围 1..50。
   - 可选 filters：`source_ids / provider_keys / document_types / authority_tiers / critical_claim_eligible_only / published_from/to / reporting_period_from/to`；空 list 归一化为 None；时间必须 timezone-aware，from ≤ to；reporting_period 用 `date`（当日 00:00 UTC epoch）。
   - **不支持任意用户自定义 Chroma where JSON**（RetrievalQuery 无 where 字段）。

4. **Eligible index selection（PostgreSQL 侧，read only，embedding 配置完整匹配）**。JOIN `chunk_vector_indexes ↔ chunk_sets ↔ parsed_sources ↔ source_records`，同时满足：
   - manifest `status='ready'` + **embedding 配置完整匹配**：`embedding_model_id` / `embedding_model_revision` / `embedding_dimension` / `normalize_embeddings` / `collection_name` / `collection_schema_version` 与当前 embedding schema 精确一致（`collection_name` 对齐本服务查询的 collection：生产默认 == `compute_collection_name(spec)`，测试注入自定义名时对齐注入名，保证 manifest 指向检索实际查询的 collection）；
   - **`expected_chunk_count == indexed_chunk_count`**（部分索引的 manifest 不进入 eligible）；
   - `chunker_name / chunker_version` 与当前 chunker（block_window v1）精确一致；
   - parser identity 与当前 `_parser_specs()`（html_dom v2 / pdf_layout v2）任一精确匹配（当前无 parser 注册 → 空结果）；
   - `company_id = query.company_id` + RetrievalQuery filters（source/provider/document/time/authority）。
   - 为空 → `RetrievalIndexNotReady`。**read path 不 index_chunk_set、不 repair/write**。

5. **Query embedding**。`EmbeddingProvider.embed_query(query_text)`（BGE query instruction 由 provider 加）；**禁止 silent truncation**（超长 → `EmbeddingInputTooLong` 传播）。不用 `embed_documents`。

6. **Chroma filtered query**。`build_chroma_where` 以 `{"chunk_set_id": {"$in": eligible}}` 开头（company 隔离白名单双闸之一），其余 filters 追加，单个 clause 时直接返回、多个 clause 组合成单个 `{"$and": [...]}`；时间用 epoch 数值比较。**query 前先校验 collection**：实际 name == 查询 collection，且 `collection.metadata` 冻结键（schema_version / model_id / model_revision / dimension / normalized / distance_metric）与 `build_collection_metadata(current spec)` **完全一致**；任一不一致 → `RetrievalIndexIntegrityError`，**不继续 query、不自动修改 collection**（read path 不 repair/write）。再调用 `collection.query(query_embeddings=[vector], n_results=top_k, where=where, include=["metadatas", "distances"])`；**不传 query_texts**；ids/metadatas/distances 长度不一致 → integrity error。

7. **PG hydrate + integrity（保持 Chroma ranking 顺序）**。按 `str(chunk_id)` 批量 hydrate `DocumentChunk → ChunkSet → ParsedSource → SourceRecord`（4 表 JOIN），逐条校验：
   - chunk 存在（缺失 → `RetrievalIndexIntegrityError`）；
   - `chunk_set_id ∈ eligible`；
   - **chunk_id 无重复**（Chroma 返回重复 id → integrity error）；
   - Chroma metadata 与 PG 逐 key 一致：`chunk_id / chunk_set_id / parsed_source_id / source_id / company_id / provider_key / document_type / text_sha256`；
   - distance 是 finite 数（`NaN / Inf` → integrity error）。
   任何不一致 → 稳定错误码 `retrieval_index_integrity_error`。非 UUID chunk_id → integrity error。

8. **Ranking**。只使用 Chroma cosine distance，升序（相似度降序）。**无 similarity threshold / reranker / MMR / BM25 混合 / LLM relevance judge**。`distance` 字段只作检索诊断，**禁止叫 confidence / probability / score**；top_k 不足时返回实际命中数。

9. **RetrievalHit（read model，不落库）**。字段：`rank / chunk_id / chunk_set_id / parsed_source_id / source_id / company_id / text / distance / provider_key / document_type / source_title / source_url / published_at / reporting_period_end / authority_tier / critical_claim_eligible / chunk_ordinal / locator_refs`。`locator_refs`（nested JSON）只从 PG hydrate，不进 Chroma。

10. **稳定错误码（`app/rag/retrieval/errors.py::stable_error_code`）**。RetrievalError 映射自身 code（`invalid_retrieval_query / retrieval_index_not_ready / retrieval_index_integrity_error / retrieval_operation_failed`）；EmbeddingError 映射自身 code；`chromadb` 错误 → `chroma_operation_failed`；其他 → `retrieval_operation_failed`。不泄露内部细节。

11. **测试（自动化全部 FakeEmbeddingProvider，零真实模型下载）**。
    - 单元：RetrievalQuery 校验（company_id / trim / 长度 / top_k / 空 list / timezone-aware / from≤to）、build_chroma_where（$in 恒在 / 单 clause 无 $and / 组合 $and / epoch 时间 / 无自定义 where）、RetrievalHit 字段（distance 非 confidence/score）、稳定错误码、RetrievalService（query instruction 走 embed_query / token too long 传播不截断 / no threshold / company isolation where / collection 缺失→NotReady / eligible 空→NotReady / Chroma 不可用→稳定错误 / hydrate integrity 三类）。
    - 集成（真实 PG + FakeChroma 零网络）：company 隔离 / provider / document_type / source_ids / authority_tier / critical-only / published range / reporting period range / ready manifest only（failed+building 排除）/ 旧 chunker+parser 排除 / query→Chroma→PG hydrate 全链路 / ranking 顺序 / locator_refs 从 PG hydrate / chunk 缺失→integrity / Chroma metadata 篡改→integrity / Chroma 不可用→稳定错误 / **read path 0 manifest、0 Chroma 写**。
    - 真实 Chroma（127.0.0.1:8002）：独立测试 collection（uuid 后缀，结束删除）：company filter / chunk_set_id $in / distance 排序 / PG hydrate；结束后删除 collection。

12. **无 persistence migration**。RetrievalHit 是 read model；不新增 DB 表；Alembic 保持 **0015 head**。

13. **3B.2.1 检索 integrity 收口（2026-08-08）**。
    - **collection metadata 校验（任何 Chroma query 前）**：`_get_collection` 成功后校验实际 name == 查询 collection，且 `collection.metadata` 6 冻结键（schema_version / model_id / model_revision / dimension / normalized / distance_metric）与 `build_collection_metadata(current spec)` 完全一致；任一不一致 → `RetrievalIndexIntegrityError`，**不继续 query、不自动修改 collection**。单元测试：correct metadata → query 放行；revision / dimension / normalized / distance_metric / schema_version 任一篡改 → 拒绝且 spy `collection.query` 调用次数 = 0；name 不匹配 → 拒绝。集成测试：篡改 collection.metadata → integrity error（spy query 次数 = 0）。
    - **eligible 完整匹配**：除既有 `status='ready'` / model_id / revision / schema_version / chunker / parser / company / filters 外，新增 `embedding_dimension` / `normalize_embeddings` / `collection_name`（对齐查询的 collection）/ `expected_chunk_count == indexed_chunk_count`；不满足者不进 eligible。集成测试：错误 dimension / normalize / collection_name、ready 但 indexed<expected → 排除（`RetrievalIndexNotReady`）。
    - **query result integrity 补充**：Chroma 返回重复 chunk_id → integrity error；distance 非 finite（NaN / Inf）→ integrity error。

14. **Stage 3B 收口（2026-08-08）**。3B.1 的 `latest_image_docker_acceptance` 已由 CPU-only build 达成：Docker 预装 torch 2.13.0+cpu（PyTorch 官方 CPU index），`pip install .` 复用、不拉 nvidia-* CUDA 运行时包；镜像 `9066b4c9150b`（2.24GB）内 `torch.version.cuda is None`、`torch.cuda.is_available() is False`，`pip list` 无任何 nvidia-* / triton 包，BGE 保持 lazy load、不入 /ready。**Stage 3B（3B.1 + 3B.2 + 3B.2.1）= completed**。

## 后果

- 语义检索 read path 建立，与索引 write path（3B.1）共享 collection identity v2：同一 embedding schema 下 company 隔离由 `company_id`（PG eligible）+ `chunk_set_id $in`（Chroma where）双闸保证。
- 检索绝不触发自动重建：索引不一致在 read path 暴露稳定错误码，修复只能走显式 `index_chunk_set`（3B.1 服务能力）。
- `distance` 的语义边界明确：只作为排序依据与诊断，不是置信度；为后续 EvidenceCard / Claim 阶段预留 "候选证据单元 + 原文定位" 的输入形态。
- 3B.2 之后可直接进入 **3C EvidenceCard**（把 RetrievalHit 封装为可独立核对的证据单元）。

## 明确不做（边界）

不实现 EvidenceCard / Claim / Report / LLM / LangGraph 集成；不新增检索 HTTP 端点（本阶段只有 service 层 read path）；不做 reranker / similarity threshold / MMR / BM25 混合 / LLM relevance judge；不支持任意用户自定义 Chroma where JSON；检索不自动 index_chunk_set / 不 repair / 不重建 / 不创建 collection；不落库 RetrievalHit。
