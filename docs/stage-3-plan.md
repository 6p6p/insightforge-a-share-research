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

## 3B：BGE Embedding + Chroma indexing/retrieval（next，尚未开始）

- 对 3A 的 DocumentChunk 生成确定性 Embedding（BGE），写入 Chroma collection（向量索引）。
- 语义检索：给定查询 → 返回候选 Chunk 及其证据链定位。
- 前置：3A 完成（已达成）；需引入 BGE 相关依赖并冻结模型版本。
- 不在本阶段实现的边界：EvidenceCard / Claim / Report / LLM。

## 3C：EvidenceCard（later，尚未开始）

- 把 Retrieval 命中的 Chunk + 原文定位封装为 EvidenceCard（可独立核对的证据单元）。
- 前置：3B 完成。
- 后续：Claim（主张抽取）、Report（研报生成）、Audit（事实审核）属于 Stage 4 及以后。
