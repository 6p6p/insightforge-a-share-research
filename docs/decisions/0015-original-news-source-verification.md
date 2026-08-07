# ADR-0015：Original Publisher Verification、Safe HTML RawArtifact 归档与 SourceRecord 登记（阶段 2D.2A）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **2D.2A 状态（四维）：implementation completed / automated tests completed / docker rebuild acceptance completed / live external acceptance completed（2026-08-07）**。
   - 本阶段实现确定性链路：`NewsDiscoveryCandidate → Original Publisher → Safe HTML fetch → RawArtifact(text/html) → SourceRecord → NewsSourceVerification`。自动化测试全链路使用 MockTransport + FakeHostResolver，无真实外网请求。
   - 受控真实 Probe（§二十八）只对 `xinhuanet.com` 最多 1 次请求、不重试、不代理、不 DNS 覆盖，已执行且成功（真实 DNS + 真实 HTTPS，status=200 / text/html / redirects=0 / 178,458 字节），**live external acceptance = completed**；本环境网络阻断时记录为 pending，不阻塞提交。
2. **顶层不变量 A：Discovery Provider ≠ Source Provider**。
   - GDELT、搜索引擎、LLM 搜索都是 Discovery Provider / acquisition mechanism，不是事实发布者、不是 SourceProvider、不是 Evidence source。`candidate.discovered_url` 指向的**原始发布网页**才是潜在事实来源。本阶段正是把"候选线索"提升为"可追溯原始来源记录"的第一步，但提升不等于证明内容为真。
3. **顶层不变量 B：Candidate URL 不直接请求——必须走 Resolver → Registry → SafeHtmlFetcher**。
   - 任何代码不得直接对 `candidate.discovered_url` 发起网络请求。获取原文的唯一通路是：`OriginalPublisherResolver`（把 normalized_url 解析为 Source Registry 中登记的 original publisher）→ `SafeHtmlFetcher`（DNS/SSRF 校验 + 域名归属校验 + 手动重定向重校验）。Discovery Candidate 只是线索，其 URL 必须先在 Source Registry 的 allowlist 语义下被认定为原创发布者域名，才能被获取。
4. **顶层不变量 C：只有 enabled + news_article + public_html 的 Provider 才有资格成为 original publisher**。
   - Resolver 只从满足 `enabled=True ∧ capabilities 含 news_article ∧ acquisition_methods 含 public_html` 的 Provider 中解析；无匹配 → `NewsPublisherUnsupported`；多个匹配 → `NewsPublisherAmbiguous`（不自动按优先级挑选）。
5. **顶层不变量 D：publisher verification 只证明"页面已公开可访问并被归档"，不证明新闻为真**。
   - `NewsSourceVerification` 的语义严格限定为：原始发布网页属于登记的原创媒体、公开 HTML 被安全获取、raw HTML 已不可变归档、Candidate → SourceRecord 溯源已建立。它**不代表**：内容真实、已交叉验证、支持关键声明、是 Evidence。候选新闻真实性判定属于后续 Evidence 管线。
6. **顶层不变量 E：Media tier 3，critical_claim_eligible = false**。
   - 第一批 Original Publishers（新华网/中国证券网/中证网）全部 `provider_type=media`、`authority_tier=3`、`critical_claim_eligible=false`。它们可以成为"事实来源"登记，但新闻内容不能单独支撑关键声明（critical claim）。
7. **顶层不变量 F：seen_at 永不是 published_at；published_at 未知时为 NULL**。
   - `candidate.seen_at` 是 GDELT 观测到该链接的时间，不是新闻发布时间。`source_records.published_at` 必须为 NULL，除非未来有确定性途径获得真实发布时间；本阶段一律 NULL。
8. **顶层不变量 G：SourceRecord.provider_key 必须是真实原创发布者（xinhuanet / cnstock / cs_com_cn），绝无 gdelt**。
   - 登记进 `source_records` 的 `provider_key` 是 Resolver 解析出的原创媒体 Provider key，绝不可能是 `gdelt` / `gdelt_doc`。GDELT 永远只出现在 Discovery Run / Candidate 溯源，不进入 SourceRecord。
9. **顶层不变量 H：source_url 使用 final URL；discovery URL 保留在 Candidate + Verification 溯源**。
   - `source_records.source_url` 必须是 SafeHtmlFetcher 返回的最终 URL（可能经过重定向）。发现时的原始 URL 保存在 `news_discovery_candidates.discovered_url` 与 `news_source_verifications.requested_url`，构成完整溯源。
10. **Migration 0012（revision 0012，revises 0011）与 Schema 演进（§五-六）**。
    - 通过 drop 旧 CHECK + create 新 CHECK 演进 5 个约束：`source_providers.provider_type` +media；`raw_artifacts.media_type` +text/html（保留 pdf/json）；`source_records.document_type` +news_article；`source_records.acquisition_method` +public_html；`news_discovery_candidates.verification_status` 允许 unverified + verified（仍禁止 fact_verified/evidence/trusted）。
    - 新表 `news_source_verifications`：verification_id UUID PK / candidate_id FK candidates RESTRICT UNIQUE / source_id FK source_records RESTRICT / publisher_provider_key FK source_providers RESTRICT / requested_url TEXT / final_url TEXT / final_hostname VARCHAR(255) / http_status SMALLINT(200-299) / content_type VARCHAR(255) / redirect_count SMALLINT(0-5) / title_origin VARCHAR(32) IN ('discovery_candidate') / verified_at TIMESTAMPTZ / created_at。CHECK：requested/final URL 与 hostname btrim 非空；索引 source_id / publisher_provider_key / verified_at。
11. **OriginalPublisherResolver（§七）**。
    - 纯函数输入 normalized URL，输出单个 `SourceProviderModel`；不发起网络请求。规则：可解析 URL；仅 https；无 userinfo；无非默认端口；hostname IDNA 规范化；hostname 必须等于 allowed_domain 或真实子域（`is_url_allowed` 语义，非 substring）；只匹配 enabled + news_article + public_html Provider。
12. **Candidate 完整性校验（§八）**。
    - 从 `candidate.normalized_url` 重算 hostname，必须等于 `candidate.domain`，否则 `NewsOriginalSourceIntegrityError`。确认 `candidate.run_id → Run.company_id` 存在；SourceRecord.company_id 一律来自 `NewsDiscoveryRun.company_id`，绝不来自 Candidate 或任何外部参数。
13. **DNS/SSRF 防护（§九）**。
    - `HostResolver` Protocol + `SystemHostResolver`（IPv4+IPv6，`asyncio.to_thread` 包裹 `socket.getaddrinfo`）。拒绝 loopback / private / link-local / multicast / reserved / unspecified / shared；拒绝 IP 字面量作为 hostname。DNS 预检是纵深防御，**不在 ADR 中宣称传输层 DNS pinning**。
14. **SafeHtmlFetcher（§十）**。
    - 输入 url / provider_key / allowed_domains；可选注入 transport 与 resolver。`trust_env=False`；无 Cookie / Authorization / API Key / 浏览器 Headers / JS / 自动重试。仅 https；hostname 必须属于同一 Publisher 的 allowed_domains；DNS/IP 校验先于请求；手动重定向 ≤5 次（urljoin 后每跳完整重校验），跨 publisher 重定向拒绝；仅接受 2xx；Content-Type 基础媒体类型必须 `text/html`（不接受 xhtml+xml）；5 MiB 最大响应体（Content-Length 提前拒绝 + 实际流式超限拒绝）；保留原始字节（不强制 UTF-8、不重编码）；返回 `FetchedHtmlPage`（requested_url / final_url / final_hostname / status_code / content_type / redirect_count / fetched_at tz-aware UTC / raw_bytes）。
15. **HTML RawArtifact 归档（§十一）**。
    - `LocalRawArtifactStore.put_html_bytes`：media_type `text/html`；storage key `sha256/ab/cd/<sha256>.html`；内容寻址、原子写、不可变、replay 不覆盖（与 PDF/JSON 同语义）；不解码不解析；空字节语义沿用既有策略。
16. **Artifact 冲突防线（§十二）**。
    - 先 `get_or_create` 再强一致校验：既有一行 media_type==text/html 且 content_sha256 / byte_size / storage_key 与本次落盘描述完全一致，否则抛 `NewsOriginalArtifactConflict`（稳定默认消息 "news original source raw artifact metadata conflict"），不创建任何 SourceRecord / Verification。
17. **NewsOriginalSourceService（§十三-十八）**。
    - `verify_candidate(candidate_id)`：从 Candidate + Run + Resolver 派生 company_id / provider_key / URL；成功 SourceRecord 字段：company_id=candidate.run.company_id、provider_key=解析 publisher、document_type=news_article、title=candidate.title（截断到 500 上限，display 元数据边界）、source_url=final_url、artifact_id、published_at=NULL、reporting_period_end=NULL、external_document_id=NULL、authority_tier_snapshot=publisher.authority_tier、critical_claim_eligible_snapshot、provider_capabilities_snapshot=sorted(...)、acquisition_method=public_html、status=available。同一 DB 事务内：RawArtifact → SourceRecord → Verification → candidate verified → commit。
    - replay：短 DB session 检查既有 Verification，完整性通过则返回 replayed=true，无网络请求。SourceRecord 去重键 (provider_key, final_url, artifact_id)：多个 Candidate 可指向同一 SourceRecord，但各自独立 Verification（UNIQUE per candidate）。并发用 `ON CONFLICT DO NOTHING + RETURNING`，无进程锁。
18. **失败语义（§十九）**。
    - 失败不创建任何 Verification 行，candidate 保持 unverified。稳定错误：`NewsCandidateNotFound` / `NewsPublisherUnsupported` / `NewsPublisherAmbiguous` / `NewsOriginalFetchFailed` / `NewsOriginalContentRejected` / `NewsOriginalArtifactConflict` / `NewsOriginalSourceIntegrityError` / `NewsOriginalPersistenceFailed`。消息禁止完整响应正文 / HTML / DB URL / 绝对存储路径 / Cookie / Auth / query_text；日志只记录 provider_key / hostname / status / duration / error_type。
19. **Upload/Import 边界（§二十）**。
    - `news_article` 不改变 POST source upload / import-url 的既有语义：上传与 URL 导入仍只接受其原文档类型与能力集；`news_article` 只经 `NewsOriginalSourceService` 产生；回归测试证明 PDF 上传/导入无法绕过。
20. **HTTP Content 安全（§二十一）**。
    - `GET /source-records/{id}/content` 不得内联返回第三方 HTML（存储型 XSS）。HTML 形态的 SourceRecord 内容端点 → 415 `SourceContentUnsupportedMediaType`；测试断言内容端点不内联返回 HTML body。
21. **明确不做的边界（§二十七）**。
    - 本阶段不创建 EvidenceCard / Claim / DocumentChunk / Chroma；不用 LLM / LangChain / LangGraph / CrewAI；不做新闻正文解析、内容清洗、摘要、情感、聚类；不做批量历史新闻同步；不开放一般 Web 爬虫；不抓取任意 GDELT Candidate。
22. **title 截断边界**。
    - `news_discovery_candidates.title` VARCHAR(1000) 而 `source_records.title` VARCHAR(500)；Service 持久化时把 title 截断到 500 字符（display 元数据边界，不影响证据链完整性）。

## 后果

- 建立"Candidate → Original Publisher → 安全获取 → 内容寻址归档 → SourceRecord → Verification"确定性链路，新闻原始发布网页第一次获得可追溯的、不可变归档的来源记录；`verified` 语义被严格限定为"发布者归属 + 页面可访问 + 原文已归档 + 溯源已建立"，不是内容真实性判定。
- 第一批 Original Publishers 进入 Source Registry（共 11 个 Provider）：新华网 / 中国证券网 / 中证网为 media / tier 3 / news_article / public_html，`critical_claim_eligible=false`；GDELT 等 discovery-only 机制仍不进 Source Registry。
- Migration 0012 已应用（`alembic current` = 0012 head）；downgrade 0011 / 再 upgrade 往返验证通过。
- 自动化测试全部 MockTransport + FakeHostResolver（Network Guard 继续拦截真实网络）；受控 Probe 仅对 xinhuanet.com 至多 1 次请求且已成功（live external acceptance = completed）。Docker 重建成功（新镜像 healthy、`/api/v1/health/live` 200、`/api/v1/health/ready` 200、五项 checks ok），docker rebuild acceptance = completed。
- 遗留边界：`news_article` 原文进入 Evidence 管线属于 2D.2B 后续/Evidence 阶段；2D.2B（verification status 演进：rejected / archived / evidence_ready）与 2D.3 未开始。
