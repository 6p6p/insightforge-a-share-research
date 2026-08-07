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
   - 不批量分页、不扫描路径、不猜 API endpoint、不读 JS bundle、不使用开发者工具。
3. **受控 ProbeClient**。
   - 仅 https；URL 必须通过 Source Registry `is_url_allowed` allowlist（含已登记子域）；同域重定向仍重新执行域名校验、跨域重定向拒绝；
   - `trust_env=False` 不读代理环境变量；无 Cookie、无 Authorization、无自定义 Header、不自动重试；
   - HTML 响应流式读取上限 2 MiB；PDF 探测只发 HEAD 语义请求、不拉取正文；
   - 单个 Provider 请求计数上限，超限即终止本次探测。
4. **探测结果只描述"公开通路形态"**。
   - `DisclosureProbeResult` 记录 listing 页面可达性、结果是否出现在首次返回的 HTML、官方 PDF 链接是否可验证、是否发现文档化 API、是否要求认证、请求数与稳定 note 代码；
   - 不保存 HTML 正文、完整 query 或响应正文。
5. **接入形态决策规则（7 条）**。
   - ① 有正式公开文档 API → `documented_api`；
   - ② 搜索结果存在于首次返回的公开 HTML 且含可验证 PDF 链接 → `public_server_rendered_html`；
   - ③ 只能确认公开 PDF 下载、但无法通过合规页面发现 → `public_direct_pdf_only`；
   - ④ 需要注册/授权/合同/API Key → `requires_auth_or_contract`；
   - ⑤ 页面只有搜索外壳、结果需 JS 或未公开接口 → `requires_javascript_or_internal_endpoint`；
   - ⑥ 不得因为发现浏览器内部 JSON 请求就标记 `documented_api`；
   - ⑦ 全部不可用 → `unavailable`。
6. **自动发现仅限两类接入形态**。
   - `DOCUMENTED_API` 与 `PUBLIC_SERVER_RENDERED_HTML` 可自动发现；其余形态不实现生产 Adapter；
   - 保留用户上传 + URL 导入 + 后续网络搜索发现作为兜底，不因为缺少自动通路而改用隐藏接口。
7. **不做通用爬虫、不绕过访问控制**。
   - 不绕验证码、Cookie、Referer、登录或频控；不通过开发者工具逆向内部 API；不调用未公开接口；
   - 遇 403 / captcha / rate-limit 即停止；不批量抓取公告、不同步全部 A 股公司。
8. **探测 CLI 是开发期诊断工具**。
   - 从 Source Registry 读取 enabled Provider 与 allowed_domains，输出 JSON 报告到 stdout，不写数据库、不下载/保留响应正文；
   - 探测结果只代表探测当时的可达性，**不承诺第三方 API 免费或稳定**，也不代表自动公告采集已实现。
9. **测试级真实网络隔离为共享机制**。
   - 顶层 conftest 的 autouse fixture 替换 `httpx.AsyncHTTPTransport.handle_async_request`，非回环主机一律抛 `AssertionError("real external HTTP is forbidden in tests")`；
   - httpx.MockTransport（自身实现 transport）与 FastAPI TestClient（ASGI transport）不受影响；PostgreSQL / Docker Chroma 本地回环测试不受影响；
   - 披露层全部自动化测试使用 MockTransport，禁真实外网。
10. **不创建新数据库表或迁移**。
    - 本阶段不引入新表；Provider 能力快照、authority_tier 快照沿用 ADR-0008 契约；
    - 不修改 LangGraph 编排，不接入 LLM 或模型联网搜索。
11. **日志与验收报告脱敏**。
    - 探测日志只记录 provider_key、hostname、status、duration_ms、response_type、事件名；
    - 验收报告可记录 provider_key、hostname、status、Content-Type、文件大小、SHA-256、探测时间；不记录完整 query、响应正文、Cookie/Header、绝对路径。
12. **2B.2B 自动获取的实现门槛**。
    - 只有探测得出 `DOCUMENTED_API` 或 `PUBLIC_SERVER_RENDERED_HTML` 且通过人工验收，才实现生产 Adapter；
    - 否则记录停止原因并继续依赖用户上传与 URL 导入，不用隐藏接口替代。

## 后果

- 建立"官方公告发现"与"来源登记"的解耦：发现层可替换、可离线验收，不污染已有归档。
- 单元测试 337 项、集成测试 61 项；披露层测试全部使用 MockTransport，不访问外网。
- 遗留边界：SSE / CNINFO 的真实探测结果由人工验收阶段记录；若发现合规通路，在 2B.2B 实现生产 Adapter。
