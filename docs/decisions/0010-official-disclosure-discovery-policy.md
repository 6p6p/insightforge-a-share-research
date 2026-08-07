# ADR-0010：官方披露来源可行性探测与 Discovery 契约（阶段 2B.2A）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **官方披露获取走"发现 → 登记"两步，Discovery 契约只做发现**。
   - `DisclosureSearchRequest` / `DisclosureCandidate` / `DisclosureDiscoveryProvider`（Protocol）构成发现层契约：一次查询返回候选（provider_key、title、source_url、discovery_url、published_at、document_type、acquisition_method 等），不下载全文、不落库、不解析 PDF。
   - Candidate 不是 SourceRecord：它只代表"官方页面上可能存在这份披露"，采纳与否由调用方决定，采纳后再走 `SourceIngestionService.ingest_url`（沿用 ADR-0009 的受限 fetcher 与域名校验）。
2. **探测目标与次数边界**。
   - 开发期只探测 sse 与 cninfo 两个 Provider；单个 Provider 最多 6 个请求、整次探测最多 12 个；
   - 不批量分页、不扫描路径、不猜 API endpoint、不调用内部数据服务接口（如 `webapi.cninfo.com.cn`）、不读 JS bundle、不使用开发者工具；
   - **SSE 从公告查询页开始；CNINFO 只从官方首页 `https://www.cninfo.com.cn/` 开始**，不直接进入 `/new/disclosure`，只跟随页面真实链接。
3. **受控 ProbeClient**。
   - 仅 https；URL 必须通过 Source Registry `is_url_allowed` allowlist（含已登记子域）；同域重定向仍重新执行域名校验、跨域重定向拒绝；
   - `trust_env=False` 不读代理环境变量；无 Cookie、无 Authorization、无自定义 Header、不自动重试；
   - HTML 响应流式读取上限 2 MiB；PDF 探测使用**流式 GET**，只读取前 8192 字节文件头验证，不下载正文、不保留文件；
   - **PDF 验证不变量**：只允许 2xx；Content-Type 去除参数后必须为 `application/pdf`；Content-Length 声明超过 10 MiB（`PDF_MAX_BYTES`）在读取正文前拒绝；跳过开头空白后必须以 `%PDF-` 开头；验证完成立即关闭流，正文不进入 `ProbeResponse`；
   - 旧实现使用 `client.get()`（默认 `stream=False`，返回前会读取/缓冲完整正文），导致 PDF 探测实际下载完整 PDF；该实现作废，统一改用 `client.stream()` + `aiter_bytes(chunk_size=8192)` 只读文件头；
   - 链接提取使用 `html.parser.HTMLParser` 轻量实现，只处理 `<a href>`，不执行 JS、不解析 onclick；单个 Provider 请求计数上限，超限即终止本次探测。
4. **探测结果只描述"公开通路形态"**。
   - `DisclosureProbeResult` 记录 listing 页面可达性、HTTP 状态码、最终 hostname（不含路径/query）、响应类型、是否匹配到公司候选、是否直接验证官方 PDF、是否发现文档化 API、是否要求认证、`search_request_applied`、请求数与稳定 note 代码；
   - 不保存 HTML 正文、完整 query、URL 或响应正文；`final_hostname` 只保存 hostname；
   - **日期范围语义固定为闭区间**：inclusive_days = `(end_date - start_date).days + 1`，必须落在 1—366 内（`2026-01-01` 至 `2027-01-01` 合法，`2027-01-02` 非法）。
5. **候选识别基于页面真实链接（不采用通用关键词）**。
   - 删除基于"公告/披露/董事会"等通用关键词的结果识别；目标候选必须同时满足 5 个条件：页面中出现目标 security_code、非空标题、发布时间/日期文本、可解析 `<a href>` 链接、urljoin 后重新执行 https + allowlist 校验；
   - 无法识别时 `matching_candidate_count=0`，不伪造 Candidate；
   - 查询框、栏目、"公告"文字本身不是结果。
6. **接入形态决策规则（严格不变量，优先级 1—7）**。
   - ① `documented_api_found=True` 且明确出现 API Key / App ID / access token / 合同 / 订阅 / 申请权限 → `requires_auth_or_contract`（认证判定只允许在 `documented_api_found=True` 时触发）；
   - ② `documented_api_found=True` 且无需认证 → `documented_api`；
   - ③ 页面可达 + `search_request_applied=True` + 按公司/日期匹配到候选 + 候选直接验证为官方 PDF → `public_server_rendered_html`（不变量：`search_request_applied=True` 且 `matching_candidate_count >= 1` 且 `direct_pdf_verified=True`）；
   - ④ 能确认官方 PDF 可公开下载（`direct_pdf_verified=True`）→ `public_direct_pdf_only`（不变量：`direct_pdf_verified` 必须为 True；即使 `search_request_applied=False` 也只判此形态，不判 server-rendered）；
   - ⑤ 页面可达 + `search_request_applied=False` → `discovery_not_confirmed`（本次受控探测未确认能按目标公司和日期自动发现；不代表 Provider 不可用、不一定需要 JS/内部接口、不排除其他合规公开入口）；
   - ⑥ 页面可达 + `search_request_applied=True` + `matching_candidate_count == 0` → `discovery_not_confirmed`；
   - ⑦ 页面不可达且无任何通路证据 → `unavailable`。
   - **绝对禁止**：`direct_pdf_verified=False` 返回 `public_direct_pdf_only`；`search_request_applied=False` 返回 `public_server_rendered_html`；`matching_candidate_count=0` 返回 `public_server_rendered_html`；仅凭页面出现"登录/注册"就返回 `requires_auth_or_contract`。
   - **当前阶段绝不自动返回 `requires_javascript_or_internal_endpoint`**：该形态只允许在明确证据（人工确认需要执行 JS 或仅有未公开接口）下判定，不得由探测自动推断；SSE/CNINFO 探测结果为 `discovery_not_confirmed` 时表述为"尚未确认自动发现通路"，不表述为"不可用"或"需要 JS/内部接口"。
   - API/Auth 信号：`authentication_required` 只能在已确认官方 API 文档入口（标记出现在锚点/href 中）+ 明确出现凭据/合同/订阅/申请权限类字样 + 同一页/同一结构化区域时置真；未确认时 `documented_api_found=false`、`authentication_required=false`，notes 可记录 `official_data_service_link_found`、`api_access_terms_not_verified`（例如 CNINFO 首页发现 `webapi.cninfo.com.cn` 链接，只记录、不调用）。
7. **日期参数显式记录**。
   - CLI 使用 `DisclosureProbeContext`（security_code / start_date / end_date），不伪造 company_id；
   - 输出 JSON 必须包含 `request` 字段（security_code、start_date、end_date）；
   - 日期/公司筛选未真正送入合规查询入口时 `search_request_applied=false`，不宣称已筛选。
8. **自动发现仅限两类接入形态**。
   - `DOCUMENTED_API` 与 `PUBLIC_SERVER_RENDERED_HTML` 可自动发现；其余形态不实现生产 Adapter；
   - 保留用户上传 + URL 导入 + 后续网络搜索发现作为兜底，不因为缺少自动通路而改用隐藏接口。
9. **不做通用爬虫、不绕过访问控制**。
   - 不绕验证码、Cookie、Referer、登录或频控；不通过开发者工具逆向内部 API；不调用未公开接口；
   - 遇 403 / captcha / rate-limit 即停止；不批量抓取公告、不同步全部 A 股公司。
10. **探测 CLI 是开发期诊断工具**。
    - 从 Source Registry 读取 enabled Provider 与 allowed_domains，输出 JSON 报告到 stdout，不写数据库、不下载/保留响应正文；
    - 探测结果只代表探测当时的可达性，**不承诺第三方 API 免费或稳定**，也不代表自动公告采集已实现；未确认合规通路时只表述"尚未确认"，不宣称 SSE / CNINFO 不可用。
11. **测试级真实网络隔离为共享机制**。
    - 顶层 conftest 的 autouse fixture 同时替换 `httpx.AsyncHTTPTransport.handle_async_request` 与 `httpx.HTTPTransport.handle_request`（异步与同步），非回环主机一律抛 `AssertionError("real external HTTP is forbidden in tests")`；
    - 放行名单只保留 `127.0.0.1`、`::1`、`localhost`，`0.0.0.0` 不放行；
    - httpx.MockTransport（自身实现 transport）与 FastAPI TestClient（ASGI transport）不受影响；PostgreSQL / Docker Chroma 本地回环测试不受影响；
    - 披露层全部自动化测试使用 MockTransport，禁真实外网。
12. **不创建新数据库表或迁移**。
    - 本阶段不引入新表；Provider 能力快照、authority_tier 快照沿用 ADR-0008 契约；
    - 不修改 LangGraph 编排，不接入 LLM 或模型联网搜索。
13. **日志与验收报告脱敏**。
    - 探测日志只记录 provider_key、hostname、status、duration_ms、response_type、事件名；
    - 验收报告可记录 provider_key、hostname、status、Content-Type、文件大小、SHA-256、探测时间；不记录完整 query、响应正文、Cookie/Header、绝对路径。
14. **2B.2B 自动获取的实现门槛**。
    - 只有探测得出 `DOCUMENTED_API` 或 `PUBLIC_SERVER_RENDERED_HTML` 且通过人工验收，才实现生产 Adapter；
    - 否则记录停止原因并继续依赖用户上传与 URL 导入，不用隐藏接口替代；
    - **收口探测（2026-08-07）未满足启动门槛**，2B.2B 暂缓：SSE/CNINFO 均判为 `discovery_not_confirmed`，不创建生产 Disclosure Adapter。

## 后果

- 建立"官方公告发现"与"来源登记"的解耦：发现层可替换、可离线验收，不污染已有归档。
- 单元测试 357 项、集成测试 61 项；披露层测试全部使用 MockTransport，不访问外网。
- **首次 Probe 结果（2026-08-07 提交 c17e7b2 前）作废**：其将 SSE 判为 `public_direct_pdf_only` 但 `direct_pdf_verified=false`（违反不变量），将 CNINFO 判为 `unavailable`（入口 `/new/disclosure` 不可达）。作废结论保留在本 ADR 历史中，不作为后续决策依据。
- **2B.2A 收口 Probe 结果（2026-08-07 提交 `fix: finalize disclosure probe invariants` 前）**：SSE 与 CNINFO 首页均可达（HTTP 200），但 `search_request_applied=false`、`direct_pdf_verified=false`、`matching_candidate_count=0`，两者均保守判为 `discovery_not_confirmed`；总请求 2（≤12），`selected_candidate_provider=null`。该结果说明"尚未确认能按公司/日期自动发现"，不表示 Provider 不可用。
- **2B.2B 自动获取不满足启动门槛（暂缓）**：收口探测未得出 `documented_api` 或 `public_server_rendered_html`，未通过 2B.2B 前置门槛；记录停止原因后继续依赖用户上传 + URL 导入 + 后续网络搜索发现兜底。若未来发现合规公开通路（或通过人工验收确认某一入口），再启动 2B.2B。
- 遗留边界：`requires_javascript_or_internal_endpoint` 仅允许在明确证据下人工判定，当前阶段不自动返回该形态。
