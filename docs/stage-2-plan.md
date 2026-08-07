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

- **2C.1（当前进行，2026-08-07）**：Macro Provider 契约 + World Bank Indicators Provider。
  - 宏观领域契约（`MacroQuery` / `MacroIndicator` / `MacroGeography` / `MacroObservation` / `MacroPageInfo` / `MacroFetchResult`）：只描述"获取结果"，不含 page/per_page/format/source 等 Provider 内部参数；country 规范化大写且拒绝 `ALL`；年份闭区间 1960—当前年、最多 60 年；`value=None ⇔ is_missing=True`，缺失值保留、不补齐缺失年份；value 只能由 Decimal 构造（`parse_float=Decimal`），`decimal_scale` 从 exponent 推导。
  - `WorldBankProvider` 从 Source Registry 读取快照（短 Session，网络 I/O 不持有 AsyncSession），校验 enabled / `macro_data` / `official_api` / 无 API Key；`MacroFetchResult` 携带 authority_tier、critical_claim_eligible、provider_capabilities，`acquisition_method` 固定 `official_api`。
  - 受控 `WorldBankClient`：仅 https、固定 `api.worldbank.org/v2` + `source=2`（World Development Indicators）、URL 仍通过 Registry allowlist（子域匹配）、`trust_env=False`、无 Cookie/Auth/Header、手动重定向（同 allowlist ≤3 次、跨域拒绝）、不重试、单响应流式上限 5 MiB、Content-Type 必须 JSON、日志只记录 hostname 不记录 query。
  - 分页：`per_page=1000`、请求预算 `METADATA_REQUEST_COUNT=2` + 观测分页 ≤ 20、观测分页上限 `MAX_OBSERVATION_PAGES=18`、响应 page 必须匹配请求 page、跨页去重与冲突检测；观测按 `normalized_period_start` 升序。
  - 单一国家约束：country metadata 的 `region.value` 规范化后等于 `Aggregates` 或缺失/无法确定时保守拒绝为 `geography_not_country`（不维护聚合代码黑名单）；拒绝后不继续获取 observations。
  - 年度时间语义：`period` 为 Provider 年份标签，`normalized_period_start=date(int(period),1,1)` 仅用于排序/索引/统一时间轴，不表示真实统计周期起始日；`period_semantics` 固定 `provider_year_label`。
  - 稳定错误分类：provider_not_ready / geography_not_country / request_failed / response_too_large / invalid_content_type / invalid_json / api_error / malformed_response / response_conflict / request_limit_exceeded；传输失败 stdout 只输出稳定非空消息（不泄漏 hostname/IP/query/TLS 细节）。
  - 本阶段只支持 `annual` 频率与 `country` 地理类型；不支持 FRED、NBS、月度/季度、多国家。
  - 开发期 CLI `fetch_world_bank_macro` 输出 JSON 报告到 stdout（日志走 stderr，Decimal→字符串），退出码 0/2/3/4，不写数据库、不写文件。
  - 决策记录：[docs/decisions/0011-macro-provider-and-world-bank.md](decisions/0011-macro-provider-and-world-bank.md)。
  - **2C.1.1 收口（2026-08-07）**：单一国家约束（`geography_not_country`）、年度时间语义（`normalized_period_start` + `period_semantics`）、请求预算（2+N≤20、N≤18）、`source_id` 契约、严格 JSON/数字解析（拒绝 bool/NaN/Infinity）、移除 Client 构造的全局日志副作用、稳定错误消息均已冻结并通过测试。
  - **受控真实验收（§十一）待网络环境**：本机网络对 `worldbank.org` 域名级阻断（DNS 劫持到 28.0.0.x、TLS 握手被丢弃、`--resolve` 直连真实 IP 仍失败），CLI 按规范命令运行返回 `{"error":"request_failed","message":"World Bank API request failed"}`（exit 4，稳定非空错误）；DB 前置条件已满足（`source_providers.world_bank` 已登记）。验收命令与断言不变量见 ADR-0011，可在具备 World Bank 出网的环境补跑；跑通前 2C.1 视为"当前进行"，2C.2 不开始。
- **2C.2（尚未开始）**：宏观数据持久化、Provider 快照、原始 JSON 归档。
- **2C.3（尚未开始）**：FRED Provider。
- **2C.4（尚未开始）**：国内官方宏观数据接入（NBS 等）。

### 阶段 2C 边界

1. 2C.1 不写数据库：不创建 MacroSeries / MacroObservation 表，不引入 migration。
2. 2C.1 的结果不是 Evidence：不创建 Evidence、Claim、Report 或 DocumentChunk。
3. 只有 2C.2 完成原始响应归档、来源快照与持久化后，宏观数据才能进入 Evidence/Claim 管线。
4. 宏观数据与公司 PDF SourceRecord 是不同资料形态：不强行复用当前 company-bound、PDF-only 的 SourceRecord，也不扩展 RawArtifact 支持 JSON。
5. 不修改阶段 2B 的表结构。
6. 宏观 Provider 只使用正式 API / 官方下载 / 官方页面（World Bank / FRED / NBS 等）。

## 2D：新闻发现与大模型联网搜索兜底

- 搜索结果只是发现记录；原始页面才是证据来源。

## 2E：原始归档、哈希、解析、去重与阶段验收

- 统一归档 PDF、HTML、JSON、CSV、Excel 与上传文件。
