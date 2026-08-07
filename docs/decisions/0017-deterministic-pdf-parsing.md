# ADR-0017：确定性 PDF 解析——pdf_layout v1 + pdf_page 页面定位（阶段 2E.2）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **2E.2 状态（四维）：implementation completed / automated tests completed / docker rebuild acceptance completed / live acceptance not required（2026-08-07）**。本阶段把已归档的 application/pdf SourceRecord 确定性解析为可定位结构化文本（ParsedSource + ParsedSourceBlock），**复用 2E.1 的 ParsedDocument / ParsedBlock / parse_fingerprint / ParsedSourceRepository / replay / integrity / concurrency 全链路**。**不做 OCR / Chunk / Embedding / Chroma / Evidence / Claim / Report / LLM / LangGraph**——那些属于 Stage 3。
2. **PDF parser（`app/parsing/pdf_parser.py`，`pdf_layout` VERSION=1，pdfplumber 0.11.x 是本阶段唯一新增依赖）**。
   - 输入是已归档的 application/pdf 原始字节，输出确定性 ParsedDocument（复用 `ParsedBlock` 有序序列 + extracted_title + extracted_published_at + raw_content_sha256），**不创建第二套 persistence service**。
   - **仅支持 machine-generated PDF**；整个 PDF 无任何可提取文本 → `PdfTextUnavailable`（OCR 留到未来阶段，不做扫描件识别）。单页无文字**不失败**（空页合法，被跳过）。
   - **不联网、不修改 PDF、不写临时外部文件**：内部用 `BytesIO(raw)` 包装，全程内存操作。
3. **安全边界（§3）**。
   - PDF magic 必须有效：`%PDF-` 前缀（允许前导 ASCII 空白），否则稳定 `PdfParseError`。
   - encrypted / password-protected → 稳定 `PdfEncryptedError`（pdfplumber 抛 `PdfminerException`，其 args[0] 为 `PDFEncryptionError`/`PDFPasswordIncorrect`，稳定映射到该错误，不暴露内部异常链细节）。
   - 资源限制：`page_count` 必须 ∈ 1..1000（超出 → `PdfResourceLimitError`）；提取字符总量 ≤ 5,000,000（超出 → `PdfResourceLimitError`）。单页异常超限同样落入该稳定错误。
   - 非加密但结构损坏 → 稳定 `PdfParseError`。所有 PDF 错误消息不含 PDF 正文 / 完整 raw content / 绝对路径。
4. **页面文本提取（确定性）**。
   - 使用 pdfplumber 稳定的 word/char API，**不用 experimental `extract_text_lines`**。
   - 每页先 `page.dedupe_chars()`（去掉重叠重复字符，返回新 FilteredPage 需重新赋值），再 `extract_words(use_text_flow=False, keep_blank_chars=False, expand_ligatures=True, x_tolerance=2.0, y_tolerance=3.0)`。
   - **固定排序**：page_number ASC → top ASC → x0 ASC；**固定 y tolerance（3.0）聚合 words 为行**，行内 words 按 x0 ASC；每行一个 block。
   - **不推断 heading / 语义**：普通 PDF 文本一律 `block_type=paragraph`（不做章节层级猜测）；空文本跳过。
   - 跨页相邻相同 (block_type, text) 行去重（保留首个），与 HTML parser 一致，满足 ParsedDocument 契约不变量。
5. **PDF locator 契约**：每个 block 携带 `{"type":"pdf_page","page_number":N,"line_index":M,"bbox":[x0,top,x1,bottom],"page_width":...,"page_height":...}`。
   - `page_number` / `line_index` 均 1-based；`line_index` 每页从 1 重新计数。
   - bbox 用 pdfplumber top-left 语义（top 从页面顶部测量）；全部 float `round(...,3)`；bbox 必须落在 page bounds 内。
   - 同一 PDF bytes + 同一 parser version → locator 完全稳定（float 精确相等，确定性可复算）。
6. **PDF metadata**：`extracted_title` = PDF metadata `Title` normalize 后非空否则 None；`extracted_published_at` **恒为 None**——**禁止用 CreationDate / ModDate / SourceRecord.published_at**（不变量 F 的延续：不伪造发布时间）。
7. **SourceParsingService 泛化为 dispatcher**（`app/services/source_parsing_service.py`）。
   - `_PARSERS_BY_MEDIA_TYPE`：`text/html → parse_html_bytes`（html_dom v2）、`application/pdf → parse_pdf_bytes`（pdf_layout v1）；其他 media type → 稳定 `UnsupportedParseMediaType`。
   - 复用现有 ParsedSource / ParsedSourceBlock / parse_fingerprint / Repository / replay / integrity / concurrency 逻辑，**不新增表、不新增 migration（Alembic 保持 0013 head）**；PDF 用 `block_type=paragraph` + locator JSONB。
   - **不改 schema**：现有 `parsed_sources.block_type` 5 类枚举已含 paragraph，locator JSONB object CHECK 对 pdf_page 结构同样成立。
   - `compute_parse_fingerprint` 使用当前 parser_name/version；HTML 全部回归继续通过。
8. **版本化**：`PDF_PARSER_VERSION = 1`。PDF 无确定性页面语义变更时不 bump；变更提取/排序/聚合语义时递增（同 source + 同 raw + 新 version → 新 fingerprint → 新快照，旧快照保留可追溯，与 ADR-0016 的版本化语义一致）。
9. **明确不做（边界）**。
   - 不做 OCR / 扫描件 / 图像文字识别；不做分块（Chunking）、不做 Embedding、不进 Chroma（Stage 3）；不做 EvidenceCard / Claim / Report / Audit；不用 LLM；不批量解析历史归档；不开放任何新的 HTTP 端点。
   - 2E.3（next）= Stage-2 source pipeline E2E acceptance（PDF + HTML 全链路确定性解析的端到端验收）；Stage 3 才开始 DocumentChunk / Embedding / Chroma / EvidenceCard。

## 后果

- 已归档的 application/pdf SourceRecord（如公告、研报 PDF）第一次获得确定性的、可页面定位、可校验的结构化文本快照：同一 PDF bytes + 同一 parser version → 同一 fingerprint → 同一 ParsedSource，replay 不再重复解析落库；**原始内容变化必须由新 RawArtifact + 新 SourceRecord 表达（RawArtifact 不可变，旧记录零 UPDATE）**，或 parser 版本变化 → 新快照、旧快照保留。
- `pdf_page` locator 携带页面坐标（bbox + page_width/height），供 Stage 3 Evidence 原文核对（页面级原文摘录）。
- 安全边界保持：不联网、不写临时文件、加密/损坏/超限均为稳定错误；不创建任何 Chunk / Embedding / Chroma / Evidence。
- 自动化测试：**27 项 PDF parser 单元测试 + 新增 PDF contracts/fingerprint 单元测试 + 6 项解析 Service 集成测试**（真实 PostgreSQL + 临时 LocalRawArtifactStore，零网络；PDF bytes 由纯 stdlib 手写 fixture 确定性构造，不引入 PDF 生成运行时依赖），覆盖 first parse / replay / HTML vs PDF 独立快照 / page_number/line_index 1-based 与每页重置 / bbox bounds 与 float rounding / 重复字符 dedupe / 中文提取 / 空页允许 / 整篇无文本 → PdfTextUnavailable / magic 与 malformed → PdfParseError / encrypted → PdfEncryptedError / 页数与字符超限 → PdfResourceLimitError / 跨页相邻重复行去重 / metadata Title normalize / published_at 恒 None / 确定性多遍一致 / SourceRecord 元数据不被回写 / 并发单快照 / 非受支持 media type 拒绝。
- 完整回归：**895 项非集成测试 + 全量集成测试**通过；ruff check 零告警、ruff format 全部格式化；`pip check` 通过。
- 遗留边界：OCR / 扫描件识别（未来）；分块 / Embedding / Chroma / EvidenceCard（Stage 3）尚未开始。
