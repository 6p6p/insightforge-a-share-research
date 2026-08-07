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

- **2C.1（当前，2026-08-07）**：Macro Provider 契约 + World Bank Indicators Provider。
  - 状态拆分：**implementation：completed / automated tests：completed / live external acceptance：pending**。
    - implementation 与自动化测试（2C.1.1/2C.1.2 收口）已完成并冻结；
    - live external acceptance 因本机对 `worldbank.org` 的域名级出口阻断保持 pending——**网络阻断不是代码失败**，允许离线推进 2C.2A；
    - 在真实验收跑通前：不开放生产宏观采集、不把 Macro Snapshot 视为 Evidence、不进入 Claim/Report，也不把 2C.1 标为完整生产可用。
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
  - **2C.1.2 收口（2026-08-07）**：country metadata 字段约束（`country_id`/`iso2Code`/`name` 严格解析 + 空白规范化）、响应国家与请求一致（ISO2 对 `iso2Code`、ISO3 对 country id，不匹配 `malformed_response`，不按名称猜测）、`MacroFetchResult` 跨对象一致性（Provider/Indicator/Geography/时间范围/Frequency/Source 六项构造时强制一致）、indicator 请求显式 `source=2`、CLI `malformed_response` 稳定脱敏输出均已冻结并通过测试；**2C.1 仍为"当前进行 / acceptance pending"**（真实验收受网络阻断，见下）。
  - **受控真实验收（§十一）待网络环境**：本机网络对 `worldbank.org` 域名级阻断（DNS 劫持到 28.0.0.x、TLS 握手被丢弃、`--resolve` 直连真实 IP 仍失败），CLI 按规范命令运行返回 `{"error":"request_failed","message":"World Bank API request failed"}`（exit 4，稳定非空错误）；DB 前置条件已满足（`source_providers.world_bank` 已登记）。验收命令与断言不变量见 ADR-0011，可在具备 World Bank 出网的环境补跑；跑通前 2C.1 的 **live external acceptance 保持 pending**（不影响离线推进 2C.2A）。
- **2C.2A（实现与自动化测试已完成，2026-08-07）**：宏观数据持久化数据模型。
  - RawArtifact 媒体类型从 PDF-only 泛化为 PDF+JSON（保留全部 PDF 行为、既有 PDF storage_key 与 PDF 测试）；LocalRawArtifactStore 新增 JSON 原始字节归档路径（`sha256/ab/cd/<hash>.json`）与严格校验；四张 Macro 业务表（`macro_series` / `macro_dataset_snapshots` / `macro_snapshot_artifacts` / `macro_observations`）+ migration 0009（已应用，`alembic current` = 0009）；对应 Repository（ON CONFLICT 并发去重、稳定排序、不 commit）。
  - Macro JSON 原始响应只归档到 RawArtifact，不包装成 SourceRecord；SourceRecord 仍保持 company-bound、PDF-only 语义，不受影响。
  - 本阶段不实现 MacroPersistenceService、不创建 Macro API、不把 Macro 数据接入 Evidence/Claim、不接入 LangGraph/LLM/Agent/RAG/Chroma；没有已持久化的真实 World Bank 数据。
  - 测试：新增 61 项单元测试 + 29 项集成测试（`test_macro_persistence_schema.py`）；全部集成测试（61 既有 + 29 新增 = 90）通过。
  - 决策记录：[docs/decisions/0012-macro-snapshot-persistence-model.md](decisions/0012-macro-snapshot-persistence-model.md)。
- **2C.2B（completed，2026-08-07，含 2C.2B.1 最终验收）**：Macro 原始响应捕获、Snapshot Fingerprint 与事务化持久化 Service。
  - `MacroRawJsonResponse` 冻结原始响应（role/page/2xx 状态/裸 hostname/`application/json`/非空且 ≤ 5 MiB/时区感知，构造时 8 项校验）；`fetch_with_capture` 响应顺序固定 indicator → country → observations pages，逐份捕获原始字节。
  - `validate_captured_macro_fetch` 11 项完整性校验（元数据各恰一条、分页完整 1..pages、总数 = 2+pages、hostname/content-type/source_id/provider_key），失败在文件/DB 写入前拦截。
  - 原始响应先内容寻址归档（`put_json_bytes`，文件 I/O 先于 DB transaction）；孤儿文件保留等待后续 GC。
  - Snapshot Fingerprint v1：canonical JSON + SHA-256，golden vector 固定；排除 fetched_at/request_count（可重放）、输入顺序无关、基于归档 artifact 的 content SHA-256。
  - `MacroPersistenceService`（`persist_captured_fetch` / `fetch_and_persist`）严格写入顺序 A-K：网络 I/O 不持有 AsyncSession；并发幂等（ON CONFLICT DO NOTHING，仅赢家写 Links/Observations）；replay 完整性检查失败抛 `MacroSnapshotIntegrityError`，不自动修复。
  - 4 类稳定错误（`macro_capture_invalid` / `macro_artifact_conflict` / `macro_snapshot_integrity_error` / `macro_persistence_failed`），消息不含 raw body / storage 路径 / DB URL / 完整 URL / allowed_domains 全集。
  - migration 0010 新增 `fingerprint_version` / `normalization_version` + CHECK（已应用，`alembic current` = 0010 head）；**不创建 RetrievalAttempt 表**（设计决策见 ADR-0013）。
  - 本阶段不创建 Macro API、不把 Macro 数据接入 Evidence/Claim、不接 LangGraph/LLM/Agent/RAG/Chroma；没有持久化的真实 World Bank 数据。
  - 测试：新增 30 项单元测试（capture 21 + fingerprint 9）+ 13 项 MockTransport E2E 集成测试；2C.2B.1 补齐 hostname validation、JSON-only Artifact 冲突防线、事务原子性故障注入（A-D，series/snapshot/links/observations 四阶段失败均无 partial 行）与 replay 完整性（删除 Link/Observation 后抛 `MacroSnapshotIntegrityError`，不自动补回）。
  - 决策记录：[docs/decisions/0013-macro-captured-persistence-service.md](decisions/0013-macro-captured-persistence-service.md)。
- **2C.3（尚未开始）**：FRED Provider。
- **2C.4（尚未开始）**：国内官方宏观数据接入（NBS 等）。

### 阶段 2C 边界

1. 2C.1 不写数据库：不创建 MacroSeries / MacroObservation 表，不引入 migration。
2. 2C.1 的结果不是 Evidence：不创建 Evidence、Claim、Report 或 DocumentChunk。
3. 只有 2C.2（2C.2A + 2C.2B）完成原始响应归档、来源快照与持久化后，宏观数据才能进入 Evidence/Claim 管线。
4. 宏观数据与公司 PDF SourceRecord 是不同资料形态：不强行复用当前 company-bound、PDF-only 的 SourceRecord，也不把 Macro JSON 包装成 SourceRecord；2C.2A 将 RawArtifact 媒体类型从 PDF-only 泛化为 PDF+JSON（PDF 行为、storage_key 与既有归档不变），JSON 仅用于 Macro 原始响应归档。
5. 不修改阶段 2B 的表结构。
6. 宏观 Provider 只使用正式 API / 官方下载 / 官方页面（World Bank / FRED / NBS 等）。

## 2D：新闻发现与大模型联网搜索兜底

**发现（Discovery）与事实来源（Source）分离是 2D 的顶层不变量。** GDELT、搜索引擎、LLM 搜索都是 **Discovery Provider / Acquisition mechanism**（`AcquisitionMethod.WEB_SEARCH_DISCOVERY` 等），它们不是：事实发布者、SourceProvider、Evidence source。真正的新闻来源是 `candidate.discovered_url`（本阶段契约字段名 `discovered_url`）指向的**原始发布网页**。只有完成 2D.2 的 original-source verification + 原文归档后，新闻材料才有资格进入后续 Evidence 管线。

- **2D.1（completed，2026-08-07）**：News Discovery 基础 + GDELT DOC 2.0 Discovery Provider。
  - 通用 News Discovery 契约（`NewsDiscoveryQuery` / `NewsDiscoveryCandidate` / `NewsDiscoveryProvider` Protocol / `NewsDiscoveryResult`）。
  - `GdeltNewsDiscoveryProvider`：固定 endpoint `https://api.gdeltproject.org/api/v2/doc/doc`，仅 `mode=artlist&format=json&sort=datedesc&maxrecords=1..100&startdatetime&enddatetime`；安全 HTTP 规则（仅 https、固定 hostname、`trust_env=false`、无 Cookie/Auth/API Key、手动重定向 ≤3 次且 hostname 不变、不自动重试、5 MiB 流式上限、日志脱敏）。
  - Discovery Run / Discovery Candidate 持久化（migration 0011，`news_discovery_runs` + `news_discovery_candidates`）；GDELT 原始 JSON 搜索响应归档为 RawArtifact；确定性 query/result 去重（query fingerprint + replay）。
  - 75 项 News 单元测试（Contracts/Client/Parser/Fingerprint）+ 7 项 MockTransport E2E 集成测试通过；受控真实 Probe 已执行（本机对 `api.gdeltproject.org` 连接超时，acceptance 记录为 pending，与 2C.1 相同的网络阻断环境）。
  - **GDELT 不进 Source Registry**：`source_providers` seed 禁止 `gdelt`/`gdelt_doc`/`openai`/`chatgpt`/`search_engine`；GDELT 不伪装成 Tier 3/4 SourceProvider。
  - 本阶段不下载新闻正文、不解析 HTML、不把 Candidate 当 Source、不创建 Evidence/Claim、不用 LLM、不接 LangGraph。
- **2D.2（尚未开始）**：Original Source Verification + HTML RawArtifact archival。
  - 对 Candidate 指向的原始发布网页做分类与安全获取（safe fetch）、HTML RawArtifact 归档、verification status 演进（verified / rejected / archived / evidence_ready）。只有通过 2D.2 验证并归档原文后，新闻材料才有资格进入 Evidence 管线。
- **2D.3（尚未开始）**：Model Web Search fallback + Discovery Router。
  - 在 2D.1 的 GDELT 发现（主要覆盖英文机器翻译内容）之外提供大模型联网搜索兜底与路由。GDELT 不是中文原生全文搜索的可靠替代，不承诺对 A 股中文媒体拥有完整 recall。

## 2E：原始归档、哈希、解析、去重与阶段验收

- 统一归档 PDF、HTML、JSON、CSV、Excel 与上传文件。
