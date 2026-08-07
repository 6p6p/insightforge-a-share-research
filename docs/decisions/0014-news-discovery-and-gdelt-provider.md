# ADR-0014：News Discovery 契约、GDELT DOC 2.0 Discovery Provider 与发现持久化（阶段 2D.1）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **2D.1 状态（四维）：implementation completed / automated tests completed / docker rebuild acceptance pending / live external acceptance pending（2026-08-07）**。
   - News Discovery 基础 + GDELT DOC 2.0 Discovery Provider 已实现并冻结；78 项 News non-integration 测试（A. Contracts 35 / B. GDELT Client 16 / C. Parser 18 / D. Fingerprint 9）+ 11 项 MockTransport E2E 集成测试全部通过；全量回归 734 项 non-integration + 114 项 integration（原 656/103）通过，ruff lint/format 通过，alembic = 0011 head。
   - 受控真实 Probe 已执行：对 `api.gdeltproject.org` 的单次受控请求发生 **ConnectTimeout**（与 2C.1 对 `worldbank.org` 相同的域名级出口阻断环境），未重试、未代理、未 DNS 覆盖，live acceptance 记录为 **pending**。该 Probe 是真实进程，**不经过** pytest Network Guard（后者只能由自动化测试单独证明）；Probe 错误路径日志只验证了 failure-path log redaction，**不能宣称成功响应路径已验证**。
   - Docker 重建已完成：`docker compose build backend` 成功生成包含当前工作区代码的新镜像（此前 PyPI ReadTimeout 已恢复）；`docker compose up -d backend` 后容器 healthy，`/api/v1/health/live` = 200、`/api/v1/health/ready` = 200 且五项 checks（configuration/database/chroma/checkpoint/raw_storage）全部 ok，docker rebuild acceptance 记录为 **completed**。
   - 跑通前不开放生产新闻发现、不把 Discovery Candidate 视为 Evidence、不开始 2D.2。
2. **2D 顶层不变量：发现（Discovery）与事实来源（Source）分离**。
   - GDELT、搜索引擎、LLM 搜索都是 Discovery Provider / acquisition mechanism（复用 `AcquisitionMethod.WEB_SEARCH_DISCOVERY`），不是事实发布者、不是 SourceProvider、不是 Evidence source。真正的新闻来源是 `candidate.discovered_url` 指向的原始发布网页；只有 2D.2 完成 original-source verification + 原文归档后，新闻材料才有资格进入 Evidence 管线。
3. **GDELT 不进 Source Registry**。
   - `source_providers` seed 禁止 `gdelt`/`gdelt_doc`/`openai`/`chatgpt`/`search_engine`；GDELT 不伪装成 Tier 3/4 SourceProvider。`news_discovery_candidates` 表不引用 source_providers。
4. **Discovery Engine 枚举最小化**。
   - `NewsDiscoveryEngine=gdelt_doc`、`NewsDiscoveryStatus=available`、`NewsCandidateVerificationStatus=unverified`；不提前增加 verified/rejected/archived/evidence_ready（2D.2 演进）。复用现有 `AcquisitionMethod`，不新增同义枚举。
5. **NewsDiscoveryQuery 契约（8 条校验规则）**。
   - company_id UUID / query_text（trim 后非空、≤300、拒绝 CRLF/NUL）/ start_at、end_at 必须 tz-aware / start ≤ end / 窗口 ≤365 天 / end_at ≤ now(UTC)+5min / max_results 1-100（默认 50，拒绝 bool/非 int）。常量：`_MAX_QUERY_TEXT_LENGTH=300`、`_MAX_RESULTS_LIMIT=100`、`_MAX_WINDOW_DAYS=365`、`_FUTURE_TOLERANCE=5min`。
6. **NewsDiscoveryCandidate 契约（10 条校验规则）与 URL normalization**。
   - rank int ≥1（拒绝 bool）/ title trim 非空 ≤1000 / discovered_url 必须 http(s) 且无 userinfo、hostname 合法 / normalized_url 自动派生或校验与派生一致 / domain 自动派生为 normalized hostname 或校验一致 / seen_at tz-aware / engine 必须是 NewsDiscoveryEngine 实例 / source_language、source_country 空→None（trim 由 Parser 负责，契约层不改语义）。
   - `normalize_discovery_url`：scheme 小写、hostname IDNA 编码 + 小写、删除 scheme 默认端口（http→80、https→443）、保留 path 与 query、删除 fragment、不删 utm、不重排 query、不猜 canonical URL、不 follow redirect；拒绝 http/https 之外 scheme、userinfo、空/非法 hostname。
7. **GDELT DOC 2.0 Client（固定 endpoint、固定参数）**。
   - `GdeltDocClient`：每次 discover 恰好一次请求；`_build_doc_url` 只允许 `{query, mode=artlist, format=json, sort=datedesc, maxrecords, startdatetime, enddatetime}`（UTC `%Y%m%d%H%M%S`）；不接受任意 endpoint / 额外参数 / 用户 Header。
8. **HTTP 安全规则（22 条，统一在 client）**。
   - 仅 https；host 固定 `api.gdeltproject.org`；`trust_env=False`（不读系统代理/环境变量）；不发送 Cookie / Authorization / API Key；`follow_redirects=False`，手动重定向 ≤3 次且 redirect 后 hostname 必须仍为 API_HOST（跨 host 拒绝、缺 location 拒绝、loop 拒绝）；不自动重试；429→"rate limited"、5xx→"upstream error"、其他非 2xx→"http status N" 稳定错误；正文流式读取上限 5 MiB（`aiter_bytes(chunk_size=65536)`）；Content-Type 基础类型必须 `application/json`；JSON 解析 `parse_float=Decimal` + `parse_constant` 显式拒绝 NaN/Infinity；捕获 `(TimeoutException, TransportError)` → 日志脱敏 + 稳定错误 `GdeltRequestFailed("GDELT API request failed")`。
9. **日志脱敏**。
   - Client 只记录 provider_key / hostname / status / duration_ms / error_type；Provider 在解析后补充 result_count / request_count；绝不记录完整 query_text / 完整 URL / 响应正文。httpx logger 在 `configure_logging` 已设为 WARNING。
10. **GDELT JSON Parser（11 条规则）与 malformed/bad candidate 边界（2D.1.1 收口）**。
    - 顶层必须 object（否则 `GdeltMalformedResponse`）；articles 缺失→空结果（**成功返回空**，不是错误）、articles 非 list → `GdeltMalformedResponse`。malformed（结构性损坏，query 级失败）严格区别于 `GdeltInvalidJson`（JSON 解析层失败）与单条 bad candidate（行级问题，只跳过该条、不让整个查询失败）。
    - 单 article 必须 object（非 object 跳过）；缺 url/title 或 trim 空跳过；URL 非 http/https 跳过；seendate 无法解析跳过且**不代用当前时间**；domain 由 normalized URL hostname 派生、不盲信 Provider 字段；同一 normalized_url 去重保留第一条；rank 过滤后从 1 重排；输出顺序稳定。
11. **GDELT Provider（discover 一次请求、request_count=1）**。
    - `GdeltNewsDiscoveryProvider`：client.discover → Parser.parse → `NewsDiscoveryResult`；不写 DB、不访问 candidate URL、不下载正文、不创建 SourceRecord/Evidence。
12. **Migration 0011：news_discovery_runs + news_discovery_candidates（已应用）**。
    - `news_discovery_runs`：discovery_run_id UUID PK / company_id FK companies RESTRICT / engine VARCHAR(32) / query_text VARCHAR(300) / query_start_at、query_end_at TIMESTAMPTZ / max_results SMALLINT / raw_artifact_id FK raw_artifacts RESTRICT / raw_content_sha256 CHAR(64) / result_count INTEGER / request_count SMALLINT / response_status SMALLINT / final_hostname VARCHAR(253) / content_type VARCHAR(100) / query_fingerprint CHAR(64) UNIQUE / status default available / fetched_at / created_at。CHECK：engine='gdelt_doc'、query_text trim 非空、start≤end、max_results 1-100、两个 sha256 均 `^[0-9a-f]{64}$`、result_count≥0、request_count≥1、response_status 200-299、final_hostname trim 非空、status='available'。
    - `news_discovery_candidates`：candidate_id UUID PK / discovery_run_id FK runs CASCADE / rank SMALLINT / title VARCHAR(1000) / discovered_url TEXT / normalized_url TEXT / url_sha256 CHAR(64) / domain VARCHAR(253) / seen_at TIMESTAMPTZ / source_language、source_country VARCHAR(100) NULL / verification_status default unverified / created_at。CHECK：rank≥1、title/normalized_url/domain trim 非空、url_sha256 64 hex、verification_status='unverified'。UNIQUE `(run, rank)` 与 `(run, normalized_url)`；索引 run_id、domain、seen_at DESC、url_sha256。
13. **Run 不直接成为 Source（明确 provenance）**。
    - `news_discovery_runs` 不是 SourceRecord、不是 Evidence/Claim：它只是"搜索-发现过程"的审计记录（查询、请求次数、原始响应归档引用 + 冗余响应元数据），候选是待核验线索。E2E 测试断言持久化后 source_records 表为空。
14. **Discovery Persistence Service（discover_and_persist A-H）**。
    - A. provider.discover(query)（网络 I/O 期间绝不持有 AsyncSession）；B. 原始响应先 `put_json_bytes` 内容寻址落盘（文件 I/O 在 DB transaction 之前；孤儿文件保留等待后续 GC）；C. 短 DB transaction：RawArtifact get_or_create 后做**四元一致性校验**（media_type 必须 application/json、content_sha256 / byte_size / storage_key 与本次落盘描述完全一致），任一不一致抛 `NewsDiscoveryArtifactConflict()`（无参 raise，调用方拿到稳定默认 message），冲突时不创建任何 Run/Candidate；D. build_query_fingerprint（engine + company_id + query_text + UTC ISO 时间窗 + max_results + raw_content_sha256，不含 fetched_at/request_count/ID/storage_key）；E. run create-or-get by fingerprint（ON CONFLICT DO NOTHING + RETURNING，仅赢家 created=True）；F. replay 完整性检查：candidate_count == result_count，不符抛 `NewsDiscoveryIntegrityError`，不自动修复；G. 仅新 run 时 bulk insert Candidates（含 url_sha256、verification_status=unverified）；H. commit；DB 层异常 rollback 并抛 `NewsDiscoveryPersistenceFailed`。
15. **query fingerprint 语义：重复完全相同的 discovery response → 相同 run_id，replayed=true**。
    - fingerprint 由"归档后的 raw content SHA-256"构造（与磁盘归档强一致、可由持久化行重算，E2E 验证重算一致）；时区归一化为 UTC ISO。
16. **Repository 不 commit、不推导业务**。
    - `NewsDiscoveryRunRepository`（get_by_id / get_by_fingerprint / create_or_get_by_fingerprint / list_for_company 稳定排序 fetched_at DESC, created_at DESC, id ASC / count_for_company）与 `NewsDiscoveryCandidateRepository`（bulk_create / list_for_run rank ASC, id ASC / count_for_run）只做数据访问，事务由 Service 协调。
17. **错误类型稳定 5 类，消息不含敏感信息，关键类带稳定默认 message（2D.1.1 收口）**。
    - `NewsDiscoveryInvalidQuery`（news_discovery_invalid_query）、`Gdelt*` 系列（request_failed / response_too_large / invalid_content_type / invalid_json / malformed_response）、`NewsDiscoveryArtifactConflict`（news_discovery_artifact_conflict）、`NewsDiscoveryIntegrityError`（news_discovery_integrity_error）、`NewsDiscoveryPersistenceFailed`（news_discovery_persistence_failed）。错误消息不得含 query_text / raw response / DB URL / 完整 URL / path。
    - 基类 `NewsDiscoveryError` / `GdeltDiscoveryError` 提供 `code` + 类级稳定默认 `message` + `__init__`；`NewsDiscoveryArtifactConflict` 无参 raise 时 `str()` = `"news discovery raw artifact metadata conflict"`，`GdeltMalformedResponse` = `"GDELT response structure is invalid"`。无 SHA 完整值 / storage absolute path / raw JSON / DB URL / query_text。
18. **重要现实限制：GDELT 非中文全文搜索可靠替代**。
    - 已实现的是**第一种 discovery-only 新闻候选 Provider**，只产生待核验的候选 URL 线索；README 明确"系统现在可以完整搜索 A 股新闻"是不允许的表述（实际表述：第一种 discovery-only 新闻候选 Provider）。GDELT 主要覆盖英文及机器翻译内容，不承诺对 A 股中文媒体完整 recall；中文完整覆盖属于 2D.3（Model Web Search fallback）讨论范围。
19. **2D.1 不做的边界**。
    - 不下载新闻正文、不解析 HTML、不把 Candidate 当 Source、不把 GDELT 当 SourceProvider、不创建 Evidence/Claim、不做摘要/情感/聚类、不用 LLM、不接 LangGraph、不实现 Model Web Search、不实现 FRED/NBS、不创建 Macro API；2D.2（original-source verification + HTML RawArtifact archival）与 2D.3（Model Web Search fallback + Discovery Router）尚未开始。
20. **真实验收 pending 与数据门槛**。
    - 全部测试使用 MockTransport，原始归档只写测试临时目录，不连 Chroma；conftest autouse Network Guard 继续拦截任何非回环真实网络。受控 Probe 仅对 `api.gdeltproject.org` 一次请求、不重试（本环境 ConnectTimeout → live acceptance pending）；该 Probe 是真实进程、**不经过** pytest Network Guard，其错误路径日志只验证 failure-path log redaction，成功响应路径未验证。没有任何持久化的真实 GDELT 数据。生产新闻发现在真实验收跑通前不开放。

## 后果

- 建立"发现契约 → 安全 HTTP Client → 宽容 Parser → 内容寻址归档 → query fingerprint → 事务化持久化 → 并发幂等 → replay 完整性检查"的完整 2D.1 链路；Discovery 与 Source 分离成为 2D 顶层不变量并写入 stage-2-plan。
- News 相关测试共 89 项：**78 项 non-integration**（test_contracts.py 35：query 8 条校验 + URL normalization + candidate 规则；test_client.py 16：固定 endpoint/参数、单次请求、无 Cookie/Auth、trust_env=false、redirect 安全、429/5xx/超时稳定错误、Content-Type、5 MiB、非法 JSON/NaN、日志字段白名单；test_parser.py 18：11 条规则 + 2D.1.1 收口 malformed/bad candidate 3 项；test_fingerprint.py 9：golden vector + 敏感性 + 时区归一化）+ **11 项 MockTransport E2E 集成测试**（全链路持久化、replay 幂等、跨次字节稳定 replay、并发单 run、篡改候选 IntegrityError、DB 异常包装 PersistenceFailed、run 不产生 source_records、repository 查询辅助 + 2D.1.1 收口 artifact conflict 3 项（non-JSON 占用同 SHA / storage_key mismatch / byte_size mismatch）+ SourceRecord count 不变化 1 项）。
- Migration 0011 已应用（`alembic current` = 0011 head）；downgrade 先删 candidates 再删 runs。
- 全量回归通过：734 项 non-integration + 114 项 integration（含本任务新增 3 项 Parser + 4 项集成测试）；既有 PDF/Company/Task/Workflow/Macro 测试保持通过；ruff lint/format 通过。
- 遗留边界：2D.1 live external acceptance pending（网络阻断，非代码失败）；docker rebuild acceptance completed（新镜像 healthy、live/ready 200）；无任何持久化的真实 GDELT 数据；2D.2 / 2D.3 未开始。
