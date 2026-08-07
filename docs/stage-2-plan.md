# 阶段 2 计划概览

> 阶段 2 各子阶段目标概览；详细接口在对应阶段冻结。

## 2A：CompanyIdentity、Source Registry、获取策略（当前）

- 公司标准身份（ExchangeCode/MarketBoard/identity_key）、别名与来源登记。
- Source Registry：权威等级与获取方式分离，8 个默认 Provider，URL allowlist。

## 2B：官方公司披露文件 MVP、用户上传和 URL 导入

- **2B.1（已完成，2026-08-07）**：原始文件归档与来源登记。
  - RawArtifact（SHA-256 内容寻址不可变字节归档）与 SourceRecord（一次来源登记，引用 artifact_id）两个独立表。
  - 用户上传（multipart）与安全 URL 导入（SafePdfFetcher，重定向重校验域名、双重大小上限、MockTransport 测试）。
  - 决策记录：[docs/decisions/0009-source-ingestion-and-raw-artifacts.md](decisions/0009-source-ingestion-and-raw-artifacts.md)。
- **2B.2A（已完成，2026-08-07）**：官方披露来源可行性探测与 Discovery 契约。
  - Discovery 契约（SearchRequest / Candidate / Provider Protocol）只做发现候选，不下载、不落库、不解析 PDF。
  - 受控 ProbeClient + Probe CLI：只探测 sse/cninfo，单 Provider ≤6 请求、整次 ≤12，仅访问 Source Registry allowlist 内的 https URL，无 Cookie/Auth、不执行 JS、不逆向内部 API、不调用内部数据服务接口。
  - PDF 探测使用流式 GET 只读取前 8192 字节文件头验证（2xx + Content-Type `application/pdf` + Content-Length ≤ 10 MiB + `%PDF-` 签名），不下载正文；旧 `client.get()` 完整下载实现的缺陷已修复。
  - 候选识别基于页面真实链接（security_code + 非空标题 + 日期文本 + urljoin 后 allowlist），不采用通用关键词；日期范围语义固定为闭区间 1—366 天。
  - 接入形态决策采用严格不变量（7 条优先级）；新增保守形态 `discovery_not_confirmed`；`documented_api` 与 `public_server_rendered_html` 可自动发现，当前阶段不自动返回 `requires_javascript_or_internal_endpoint`。
  - 收口 Probe（2026-08-07）：SSE 与 CNINFO 首页均可达（HTTP 200）但未确认自动发现通路，两者判为 `discovery_not_confirmed`，`selected_candidate_provider=null`，总请求 2。
  - CNINFO 只从官方首页开始，不进入 `/new/disclosure`；发现 `webapi.cninfo.com.cn` 只记录、不调用。
  - 决策记录：[docs/decisions/0010-official-disclosure-discovery-policy.md](decisions/0010-official-disclosure-discovery-policy.md)。
- **2B.2B（暂缓，2026-08-07）**：官方公司披露文件自动获取。
  - 前置门槛：探测得出 `documented_api` 或 `public_server_rendered_html` 且通过人工验收；否则记录停止原因，继续依赖用户上传与 URL 导入。
  - 收口探测未满足启动门槛，2B.2B 暂缓；若未来确认合规公开通路（或通过人工验收）再启动。
- 不做通用爬虫；获取方式严格限定为官方 API / 官方下载 / 官方页面。阶段 2B.1 不执行外网请求、不解析 PDF 正文；2B.2A 只做受控探测，不批量抓取。

## 2C：宏观数据 Provider

- 只使用正式 API、官方下载或官方页面（NBS/FRED/World Bank 等）。

## 2D：新闻发现与大模型联网搜索兜底

- 搜索结果只是发现记录；原始页面才是证据来源。

## 2E：原始归档、哈希、解析、去重与阶段验收

- 统一归档 PDF、HTML、JSON、CSV、Excel 与上传文件。
