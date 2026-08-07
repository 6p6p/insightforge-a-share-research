# ADR-0018：确定性文档分块——block_window v1 + ChunkSet/DocumentChunk（阶段 3A）

- 状态：已接受
- 日期：2026-08-08
- 决策人：InsightForge 项目

## 决策

1. **3A 状态（四维）：implementation completed / automated tests completed / docker rebuild acceptance completed / live acceptance not required（2026-08-08）**。本阶段把 **ParsedSource + ordered ParsedBlocks 确定性解析快照**切分为 **ChunkSet + DocumentChunk 快照**（新表 `chunk_sets` + `document_chunks`，migration 0014）。**不做 Embedding / Chroma indexing / Retrieval / EvidenceCard / Claim / Report / LLM / LangGraph**——Stage 3B（BGE + Chroma）与 3C（EvidenceCard）尚未开始；**本阶段不创建任何 Chroma collection**（实现侧有源码级 guard 测试，见后果）。

2. **ChunkingService（`app/services/chunking_service.py`）复用 SourceParsingService 的「短 DB session → 纯函数 → 短 DB transaction」模式**。
   - 短 session 只读 ParsedSource + ordered ParsedBlocks（`get_by_id` + `list_for_parsed_source`），随后关闭 session；**不重新读取 RawArtifact、不重新解析**。
   - 纯函数 chunking（`block_window` v1）+ 计算 `chunk_set_fingerprint`；短 transaction 里 create-or-get ChunkSet →（created 时）bulk insert chunks → commit。
   - 输入损坏（block_type / text hash / locator 与契约不符）统一抛 `ChunkSetIntegrityError`；ParsedSource 不存在 → `ParsedSourceNotFound`；持久化事务失败 → `ChunkSetPersistenceFailed`（回滚，不部分写入）。

3. **block_window chunker v1（`app/chunking/chunker.py`，纯函数；字符窗口，不绑定 BGE tokenizer）**。
   - `target_chars=400` / `max_chars=500` / `overlap=0`（字符，非 token）。
   - 严格按 `block.ordinal` 顺序、尽量合并完整 block、block 之间以 `"\n"` 连接、合并后不得超过 `max_chars`；达到 target 后不再与下一 block 合并（自然结算该 chunk）。
   - 单 block > 500 字符：先按**确定性句末标点集合（。！？!?；;）**切分，段尽量接近 max；找不到合适标点则 **hard split**。英文句点 `.` **不在** v1 标点集合，对英文句点文本走 hard split（v1 冻结语义）。
   - **不删除重复文本**（原文重复必须保留）、**不跨 ParsedSource**、chunk text 非空。
   - `CHUNKER_NAME="block_window"` / `CHUNKER_VERSION=1`。

4. **Chunk 模型与 locator_refs（`app/chunking/contracts.py`）**。
   - `Chunk`：ordinal（连续 1..n）、text、text_sha256、char_count（= len(text)，Python str 语义，中文按字符计数）、locator_refs（非空）。
   - `locator_refs`：`[{"block_ordinal":N,"char_start":S,"char_end":E,"locator":{原 ParsedBlock.locator}}]`；`char_start/char_end` 相对**原 ParsedBlock.text** 的 Python 字符索引，**半开区间 [start, end)**。
   - **保证 Chunk → ParsedBlock locator → ParsedSource → SourceRecord → RawArtifact 可完整回溯**：任一 chunk 可按 refs 取回原 block 文本片段（`block.text[start:end]`），片段按 block 顺序以 `"\n"` 连接即等于 chunk.text（集成测试逐 chunk 验证）。

5. **PDF / HTML 使用同一 Chunk 模型**：只是 `locator_refs[].locator` 的 type 不同（`pdf_page` 页面坐标 vs `html_dom` DOM 级定位），locator 原样保留、逐字段校验（集成测试验证 page_number / line_index / bbox / page_width / page_height 与 DOM xpath / element_id 不被改写）。

6. **fingerprint / replay / 并发 / 版本 / 损坏（确定性、可追溯、不自动修复）**。
   - `chunk_set_fingerprint`：canonical JSON（`sort_keys + separators + ensure_ascii=False` + UTF-8）→ SHA-256，至少覆盖 `parsed_source_id`、`source_parse_fingerprint`、`chunker_name/version`、ordered chunks（text + locator_refs）；**排除 DB ID / created_at**。
   - 同 ParsedSource + 同 chunker version → 同指纹 → **replay 原 ChunkSet**（不重复插 chunks，`replayed=True`）；chunker version 变化 → 新指纹 → **新 ChunkSet，旧版本保留**（可追溯）。
   - 并发相同 chunking：`INSERT ... ON CONFLICT(chunk_set_fingerprint) DO NOTHING RETURNING`，输家回查复用既有 ChunkSet，**最终只 1 个 ChunkSet + 一套 chunks**。
   - 已有 ChunkSet replay 时校验 parsed_source 一致、source_parse_fingerprint 一致、chunker 身份一致、chunk_count 一致、每个 chunk 的 ordinal/text/text_sha256/char_count/locator_refs 完整；任何不一致抛 `ChunkSetIntegrityError`，**不自动修复**（ChunkSet 是证据链的一部分，静默重建会掩盖不一致）。

7. **不修改上游**：ChunkingService 对 SourceRecord / ParsedSource **零写操作**（集成测试验证 title / published_at / artifact_id / block_count / parse_fingerprint 全部不变）；分块只是 ParsedSource 快照的下游只读消费。

8. **schema（migration 0014，Alembic head = 0014）**。
   - `chunk_sets`：chunk_set_id UUID PK、parsed_source_id FK **RESTRICT**、chunker_name、chunker_version、source_parse_fingerprint、chunk_count、chunk_set_fingerprint **UNIQUE**、created_at；CHECK：fingerprint 64 hex、chunker_name 非空、chunker_version ≥ 1、chunk_count ≥ 0。
   - `document_chunks`：chunk_id UUID PK、chunk_set_id FK **CASCADE**、ordinal、text、text_sha256、char_count、locator_refs JSONB、created_at；CHECK：ordinal ≥ 1、fingerprint 64 hex、char_count ≥ 1、text 非空、`jsonb_typeof(locator_refs)='array'`；UNIQUE(chunk_set_id, ordinal)。
   - migration 0014 downgrade 在 chunk_sets 有行时拒绝（数据安全防护）。

9. **明确不做（边界）**：不做 Embedding / 向量 / Chroma collection / Retrieval（Stage 3B）；不做 EvidenceCard / Claim / Report / Audit（Stage 3C 及之后）；不用 LLM / LangGraph；不批量分块历史归档；不开放新的 HTTP 端点；不改动 SourceRecord / RawArtifact / ParsedSource 语义与 schema。

## 后果

- 已归档 HTML / PDF 的 ParsedSource 第一次获得确定性的、可定位、可校验的分块快照：同一 ParsedSource + 同一 chunker version → 同一 fingerprint → 同一 ChunkSet，replay 不再重复分块落库；**解析层内容变化由新 ParsedSource（新 parse_fingerprint）表达**，chunker version 变化 → 新 ChunkSet、旧版本保留。
- 每个 Chunk 携带 locator_refs（block_ordinal + char 索引 + 原 locator），Stage 3B 索引 / 3C EvidenceCard 可精确回溯到 ParsedBlock → ParsedSource → SourceRecord → RawArtifact 的原始文本片段与页面/DOM 位置。
- 字符窗口（overlap=0）不绑定任何 tokenizer，保证分块本身是确定性代码产物；重复文本保留、中文按 Python str 计数，不依赖 LLM/BGE 的 token 语义。
- 自动化测试：**38 项 chunking 单元测试**（`tests/chunking/`：multi-block merge、target/max 边界、oversized 句末标点切分 + hard split、英文句点 fallback、重复文本保留、中文 char 计数、HTML DOM / PDF page locator 保留、locator_refs/char_start/char_end、chunk ordinal 连续、blocks 非空/连续校验、契约校验、fingerprint 确定性/敏感性/排除 DB ID、版本敏感契约、**源码级无 Chroma guard**）+ **11 项 ChunkingService 集成测试**（真实 PostgreSQL + 临时 LocalRawArtifactStore + 真实 SourceParsingService + 真实 ChunkingService，零网络）：HTML/PDF Source → ParsedSource → ChunkSet → DocumentChunks 首建、**逐 chunk 回溯到 SourceRecord + RawArtifact + 原 locator**、多 block 多 chunk 回溯、replay、chunker version 变化新旧并存、并发单 ChunkSet、chunk text / chunk_count / locator_refs 篡改 → `ChunkSetIntegrityError` 不修复、ParsedSourceNotFound、SourceRecord/ParsedSource 零修改。
- 完整回归：**全部非集成测试 + 全量集成测试**通过；ruff check 零告警、ruff format 全部格式化；`pip check` 通过。
- 遗留边界：分块后的 Embedding / Chroma 向量索引 / 语义检索（Stage 3B）；EvidenceCard（Stage 3C）；overlap>0 的滑动窗口 / 基于语义或 tokenizer 的分块（未来版本，需 bump CHUNKER_VERSION）。
