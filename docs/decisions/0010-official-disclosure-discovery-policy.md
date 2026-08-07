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
   - HTML 响应流式读取上限 2 MiB；PDF 探测只发 HEAD 语义请求、不拉取正文、不保留文件；
   - **PDF 验证基于 Content-Type（`application/pdf`），不只依赖 .pdf 后缀**；链接提取使用 `html.parser.HTMLParser` 轻量实现，只处理 `<a href>`，不执行 JS、不解析 onclick；
   - 单个 Provider 请求计数上限，超限即终止本次探测。
4. **探测结果只描述"公开通路形态"**。
   - `DisclosureProbeResult` 记录 listing 页面可达性、HTTP 状态码、最终 hostname（不含路径/query）、响应类型、是否匹配到公司候选、是否直接验证官方 PDF、是否发现文档化 API、是否要求认证、`search_request_applied`、请求数与稳定 note 代码；
   - 不保存 HTML 正文、完整 query、URL 或响应正文；`final_hostname` 只保存 hostname；
   - **日期范围语义固定为闭区间**：inclusive_days = `(end_date - start_date).days + 1`，必须落在 1—366 内（`2026-01-01` 至 `2027-01-01` 合法，`2027-01-02` 非法）。
5. **候选识别基于页面真实链接（不采用通用关键词）**。
   - 删除基于"公告/披露/董事会"等通用关键词的结果识别；目标候选必须同时满足 5 个条件：页面中出现目标 security_code、非空标题、发布时间/日期文本、可解析 `<a href>` 链接、urljoin 后重新执行 https + allowlist 校验；
   - 无法识别时 `matching_candidate_count=0`，不伪造 Candidate；
   - 查询框、栏目、"公告"文字本身不是结果。
6. **接入形态决策规则（严格不变量，优先级 1—6）**。
   - ① 确认官方 API 文档入口且明确出现 API Key / App ID / access token / 合同 / 订阅 / 申请权限 → `requires_auth_or_contract`（认证判定只允许在 `documented_api_found=True` 时触发）；
   - ② 确认官方 API 文档入口且无需认证 → `documented_api`；
   - ③ 页面可达、按公司/日期匹配到候选、且候选直接解析为官方 PDF → `public_server_rendered_html`（不变量：`matching_candidate_count >= 1`）；
   - ④ 能确认官方 PDF 可公开下载、但无法按公司匹配候选行 → `public_direct_pdf_only`（不变量：`direct_pdf_verified` 必须为 True）；
   - ⑤ 页面可达但候选行不可识别 / 需要 JS 或未公开接口 → `requires_javascript_or_internal_endpoint`（不变量：`matching_candidate_count == 0`）；
   - ⑥ 页面不可达且无任何通路证据 → `unavailable`。
   - **绝对禁止**：`direct_pdf_verified=False` 返回 `public_direct_pdf_only`；`matching_candidate_count=0` 返回 `public_server_rendered_html`；仅凭页面出现"登录/注册"就返回 `requires_auth_or_contract`。
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
    - 否则记录停止原因并继续依赖用户上传与 URL 导入，不用隐藏接口替代。

## 后果

- 建立"官方公告发现"与"来源登记"的解耦：发现层可替换、可离线验收，不污染已有归档。
- 单元测试 347 项、集成测试 61 项；披露层测试全部使用 MockTransport，不访问外网。
- **首次 Probe 结果（2026-08-07 提交 c17e7b2 前）作废**：其将 SSE 判为 `public_direct_pdf_only` 但 `direct_pdf_verified=false`（违反不变量），将 CNINFO 判为 `unavailable`（入口 `/new/disclosure` 不可达）。作废结论保留在本 ADR 历史中，不作为后续决策依据。
- 遗留边界：SSE / CNINFO 的真实探测结果由人工验收阶段记录（当前入口语义下预期为 `requires_javascript_or_internal_endpoint` 等非自动形态）；若发现合规通路，在 2B.2B 实现生产 Adapter。
