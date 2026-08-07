# ADR-0016：确定性 HTML 解析——ParsedSource / ParsedBlock 快照（阶段 2E.1）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **2E.1 状态（四维）：implementation completed / automated tests completed / docker rebuild acceptance completed / live acceptance not required（2026-08-07）**。本阶段只把已归档的 text/html SourceRecord 确定性解析为可定位结构化文本（ParsedSource + ParsedSourceBlock）。**不做 Chunk / Embedding / Chroma / Evidence / Claim / Report / LLM / LangGraph**——那些属于 Stage 3。
2. **顶层分工：确定性解析（2E）≠ Evidence 管线（Stage 3）**。
   - 2E 只产出"已归档原文的可定位结构化文本快照"，是 Evidence 管线的**确定性前置**，不是 Evidence 本身。ParsedSource 不是 Chunk、不是 EvidenceCard；Chunk/Embedding/Chroma/EvidenceCard 全部属于 Stage 3，本阶段一张相关表都不建。
   - 因此 2D.2B 的"真实新闻正文进入 Evidence 管线"表述被移除：新闻正文的确定性解析与结构化抽取由 2E（2E.1 HTML → 2E.2 PDF）承接；Evidence 管线整体移到 Stage 3。
3. **ParsedSource 是 SourceRecord 的确定性解析快照**（migration 0013，revises 0012，已应用）。
   - 新表 `parsed_sources`：parsed_source_id UUID PK / source_id FK source_records RESTRICT / artifact_id FK raw_artifacts RESTRICT / parser_name VARCHAR(64) 非空 / parser_version BIGINT≥1 / raw_content_sha256 CHAR(64) sha256 regex / parse_fingerprint CHAR(64) sha256 regex UNIQUE / extracted_title TEXT 可空 / extracted_published_at TIMESTAMPTZ 可空 / block_count BIGINT≥0 / parsed_at TIMESTAMPTZ 非空 / created_at。索引 source_id / artifact_id。
   - 新表 `parsed_source_blocks`：block_id UUID PK / parsed_source_id FK parsed_sources **CASCADE** / ordinal BIGINT≥1 / block_type IN ('heading','paragraph','list_item','blockquote','table_text') / text TEXT 非空（btrim 非空 CHECK）/ text_sha256 CHAR(64) sha256 regex / locator JSONB（jsonb_typeof='object' CHECK）/ created_at。UNIQUE(parsed_source_id, ordinal)。
   - FK 语义：**上游 SourceRecord / RawArtifact 用 RESTRICT**（解析快照在证据链上游存在期间不可被级联删除），**Blocks 随 ParsedSource 级联删除**（快照完整性由 ParsedSource 控制）。
4. **HTML parser（`app/parsing/html_parser.py`，`html_dom` VERSION=2，lxml 5.4.0 是唯一新增依赖）**。
   - 输入是已归档的 text/html 原始字节，输出确定性 ParsedDocument（ParsedBlock 有序序列 + extracted_title + extracted_published_at + raw_content_sha256）。
   - **不联网**（`no_network=True`）、不执行 JS、**不修改 RawArtifact**；`parse_html_bytes` 只读 bytes，返回结构化结果。
   - **编码检测确定性（只从真实 meta 声明识别）**：`<meta charset="...">`（HTML5 属性形式）或 `<meta http-equiv="Content-Type" content="...; charset=...">`（http-equiv 大小写不敏感）；**绝不全局扫描任意 `charset=` 文本**（body/script 内出现 `charset=...` 不影响编码判定，`<meta name="description" content="report charset=gbk">` 之类非声明文本也绝不产生编码声明）。流程是先 `_detect_encoding`（纯本地字节扫描，`raw[:8192]` 以 latin-1 无损解码后交 **stdlib `html.parser.HTMLParser` 确定性 attribute 扫描**——只读取 `<meta>` 的 `charset` 属性或 `http-equiv` Content-Type 的 `content` 值内 `charset` 参数，不依赖正则匹配任意 `charset=` 文本）把 bytes 解码为 str，再交 lxml（str 输入不再猜编码）。BOM → 声明 → UTF-8 默认（不依赖 libxml2 的 latin-1 猜测）。声明编码不可用（LookupError / UnicodeDecodeError）回退 UTF-8；无声明且非合法 UTF-8 → `HtmlParseError`（宁可失败，不可产出 latin-1 乱码文本）。**绝不显式给 lxml 写死 encoding**（显式 encoding 会覆盖 meta charset 声明，导致 GBK 页面被按 UTF-8 解错）。
   - 内容根优先 `article → main → body`（无则 document 根）；删除 `script/style/noscript/template/svg`（含 svg 内 `<title>` 不污染标题）。
   - DOM 顺序抽取 `h1-h6 / p / li / blockquote / table`；whitespace normalize；空文本跳过；相邻 (block_type, text) 完全相同去重（保留首个）；嵌套去重（祖先链已有被选中容器块 li/blockquote/table 时跳过，其文本并入外层 `text_content()`）。
   - title 优先 `og:title → <title> → h1 → None`；全部 normalize 后非空才采用。
   - published_at **只接受明确 publication 元数据**：`<meta property/name="article:published_time" content="...">`，或 `[itemprop="datePublished"]`（可读取 `<meta content>` 或 `<time datetime>`）；**普通 `<time datetime>` 无 itemprop=datePublished 忽略**，`updated_time` / `dateModified` 不冒充 published_at。`datetime.fromisoformat` 解析后必须 timezone-aware；naive → None（无法可靠确定绝对时刻），无效值 → None。**绝不使用 `Candidate.seen_at` / `parsed_at` / 当前时间伪造**（不变量 F 的延续）。
   - 空字节 / 纯空白输入 → `HtmlParseError`；非空但内容为空白的页面 → 0 blocks 的合法 ParsedDocument。
5. **Locator 契约**：每个 ParsedBlock 携带 `{"type":"html_dom","ordinal":N,"tag":...,"xpath":...,"element_id":null|"..."}`。xpath 为绝对路径（1-based 同 tag 兄弟下标，仅当同 tag 兄弟多于一个时加 `[N]`），同一 raw bytes + parser version 下完全稳定；`element_id` 取元素 id 属性。Locator 不存绝对路径 / 不存浏览器坐标，只做 DOM 级定位，供后续 Evidence 原文核对。
6. **parse_fingerprint 是确定性 SHA-256**（`app/parsing/contracts.py::compute_parse_fingerprint`）。
   - 至少覆盖：source_id、raw artifact sha256、parser_name / parser_version、extracted metadata、ordered blocks（text + locator）。序列化用 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`；**禁止 repr() / hash()**；**排除 parsed_at / created_at / DB ID**。
   - **RawArtifact 永久不可变、SourceRecord 固定引用其 artifact**：同一 source + 相同 RawArtifact + 相同 parser version → 同一 fingerprint → replay 原快照；**原始内容变化必须由新 RawArtifact + 新 SourceRecord 表达**（各自独立 ParsedSource，旧记录零 UPDATE）；同 source + 同 RawArtifact + parser version 变化 → 新 fingerprint → 新快照，旧快照保留（可追溯）。
7. **SourceParsingService.parse_source(source_id)（`app/services/source_parsing_service.py`）**。
   - ① 短 DB session 读 SourceRecord + RawArtifact metadata → **关闭 session**；仅允许 `artifact.media_type == text/html`，否则 `UnsupportedParseMediaType`。
   - ② 文件 I/O（`LocalRawArtifactStore.open(storage_key).read()`）**不持 DB transaction**。
   - ③ 解析 + 内容寻址一致性（`document.raw_content_sha256 == artifact.content_sha256`，不一致 → `ParsedSourceIntegrityError`）+ 计算 fingerprint。
   - ④ 短 DB transaction：`INSERT ... ON CONFLICT(parse_fingerprint) DO NOTHING RETURNING` → 创建则 bulk insert Blocks → commit；并发相同 parse 最终只 1 个 ParsedSource + 一套 Blocks（PostgreSQL 唯一索引序列化并发，无进程锁）。
   - **replay**：fingerprint 命中已有快照 → 校验 source/artifact 一致、raw sha 一致、parser 身份一致、block_count 一致、逐 block 比较 ordinal/block_type/text_sha256/locator 完整 → 通过则返回 replayed=true；任一项不一致抛 `ParsedSourceIntegrityError`，**不自动修复**（证据链快照不可静默重建）。
   - **不更新 SourceRecord.title / published_at**：解析出的 metadata 只写入 ParsedSource，SourceRecord 保持原始 provenance 不可变（2D.2A 不变量 F 的延续）。
   - 稳定错误：`HtmlParseError` / `UnsupportedParseMediaType` / `ParsedSourceIntegrityError` / `ParsedSourcePersistenceFailed` / `ParsingContractViolation`；消息不含 HTML 正文 / 完整 raw content / DB URL / 绝对存储路径。
8. **安全边界（§7）**。
   - HTML content API 对 text/html 保持 HTTP 415（存储型 XSS 防线，既有测试冻结）；**不新增 raw HTML endpoint**。
   - 解析只通过 `LocalRawArtifactStore` 读取已归档字节，不启动浏览器、不联网、不执行脚本。
   - **本阶段不创建 DocumentChunk / EvidenceCard / Chroma / Embedding / Claim**；解析产物只是 ParsedSource + ParsedSourceBlock，无任何下游向量化/证据化写入。
9. **明确不做（边界）**。
   - 不做 PDF 解析（2E.2，确定性 PDF Parsing + page location）；不做分块（Chunking）、不做 Embedding、不进 Chroma（Stage 3）；不做 EvidenceCard / Claim / Report / Audit；不用 LLM / LangChain / LangGraph / CrewAI；不批量解析历史归档；不开放任何新的 HTTP 端点。
   - 2E.2（next）= 确定性 PDF 解析 + page location；2E.3 = Stage-2 source pipeline E2E acceptance；Stage 3 才开始 DocumentChunk / Embedding / Chroma / EvidenceCard。
10. **2E.1.1 语义收口（2026-08-07）**：冻结 RawArtifact 生命周期、published_at、charset detection 三项语义。
    - **RawArtifact 生命周期**：RawArtifact 永远不可变、SourceRecord 固定引用其 artifact。原始内容变化只能由新 RawArtifact + 新 SourceRecord 表达（内容寻址登记），不得通过修改既有文件 / UPDATE `content_sha256` / `byte_size` / `storage_key` 模拟。存储文件与登记 SHA 不一致一律视为存储层损坏/篡改 → `ParsedSourceIntegrityError`（完整性防线，不是原文更新）。replay 只发生在"同 source + 同 RawArtifact + 同 parser version"；"同 source 内容变化 → 新快照"的旧表述作废。
    - **published_at 严格识别**：只认可 `article:published_time` meta 或 `[itemprop="datePublished"]`（meta content / time datetime）；普通 `<time datetime>` 无 itemprop 忽略；`updated_time` / `dateModified` 不冒充 published_at；仍只接受 timezone-aware（naive / invalid → None）；不使用 seen_at / now / parsed_at。
    - **charset 严格识别**：只从真实 meta 声明识别编码（`<meta charset>` 或 http-equiv Content-Type charset），绝不从 body/script 文本或任意 `charset=` 文本推断编码；BOM → 声明 → UTF-8 默认；未知声明回退 UTF-8；无声明非 UTF-8 → `HtmlParseError`。
11. **版本化收口（Gate 0，2026-08-07）：`html_dom` VERSION 1 → 2，charset 检测改用 stdlib HTMLParser**。
    - 2E.1.1 已改变 published_at 严格识别与 charset 确定性检测语义，因此 **`HTML_PARSER_VERSION` 从 1 升为 2**（指纹随 parser_version 变化 → 同一原文在新版本下产生新快照；**旧 v1 快照不修改不删除，保留可追溯**；集成测试先 monkeypatch 成 1 建 v1 快照再恢复真实 2 验证 v1/v2 并存、v2 不 replay v1）。
    - charset 检测**从正则扫描 `<meta ...>` 标签内任意 `charset=` 改为 stdlib `html.parser.HTMLParser` 确定性 attribute 解析**：只认可 `<meta charset="...">` 属性形式或 `<meta http-equiv="Content-Type" content="...; charset=...">`（http-equiv 大小写不敏感，charset 只取 Content-Type content 值内参数）；`<meta name="description" content="report charset=gbk">` 这类非声明文本绝不产生编码声明（杜绝 UTF-8 内容被误按 GBK 解成乱码）。

## 后果

- 已归档的 text/html SourceRecord（含 2D.2A 登记的新闻原文）第一次获得确定性的、可定位、可校验的结构化文本快照：同一原文 + 同一 parser version → 同一 fingerprint → 同一 ParsedSource，replay 不再重复解析落库；**原始内容变化必须由新 RawArtifact + 新 SourceRecord 表达（RawArtifact 不可变，旧记录零 UPDATE）**，或 parser 版本变化 → 新快照、旧快照保留。
- Migration 0013 已应用（`alembic current` = 0013 head），两个新表全部约束已在真实 PostgreSQL 中验证。
- 中文编码缺陷已修复并冻结：lxml 无声明时默认 latin-1 曾导致 UTF-8 中文乱码；现在的编码只从真实 meta 声明（`<meta charset>` / http-equiv Content-Type）识别、BOM → 声明 → UTF-8 默认，既修复 UTF-8 无声明中文，又尊重 GBK 等 meta charset 声明（GBK 单测通过），且绝不从 body/script 文本推断编码、无声明非 UTF-8 内容宁可失败也不产乱码。
- 自动化测试：**36 项 HTML parser 单元测试 + 25 项 contracts/fingerprint 单元测试 + 12 项解析 Service 集成测试**（真实 PostgreSQL + 临时 LocalRawArtifactStore，零网络）全部通过，覆盖 first parse / replay / fingerprint 确定性 / **不同 RawArtifact → 独立快照（RawArtifact 不可变，旧记录零 UPDATE）** / parser version 变化 → 新快照（旧快照保留）/ 完整性损坏（block sha 篡改、block_count 不一致、存储 SHA 与登记不一致）→ integrity error 不自动修复 / 并发单快照 / ordinal 稳定 / 非 HTML 拒绝 / Source 不存在 / SourceRecord 元数据不被回写 / **published_at 只认 publication 元数据（meta datePublished、time datePublished、普通 time 忽略、updated/modified 不冒充、naive/invalid → None）** / **charset 只认真实 meta 声明（meta charset、http-equiv Content-Type、body/script 文本 charset= 不影响 UTF-8、未知声明回退 UTF-8、UTF-8 BOM）**。
- 安全边界保持：HTML 内容端点仍 415；无新增 HTTP 路由；解析只经 storage 读归档字节；不创建任何 Chunk / Embedding / Chroma / Evidence。
- 遗留边界：PDF 正文解析（2E.2）尚未开始；分块 / Embedding / Chroma / EvidenceCard（Stage 3）尚未开始。
