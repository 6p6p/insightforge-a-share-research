# ADR-0011：Macro Provider 契约与 World Bank Indicators Provider（阶段 2C.1）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **宏观数据不复用当前 company-bound 的 SourceRecord**。
   - 当前 SourceRecord 面向"公司披露 PDF"：company-bound、document_type 面向年报/公告、原始字节经 RawArtifact 以 PDF 形态归档；
   - 宏观数据是"国家/指标/年份"粒度的结构化观测，不绑定单一公司，不是 PDF 字节流；强行复用会迫使宏观数据伪造 company_id、扭曲 document_type、把 JSON 塞进 PDF 归档；
   - 因此宏观数据使用独立领域契约（MacroQuery / MacroObservation / MacroFetchResult），并在 2C.2 建立独立的持久化与 JSON 归档路径，不扩展 RawArtifact 支持 JSON。
2. **2C.1 暂不持久化**。
   - 本阶段只验证 Provider 契约与获取语义：真实指标/国家元数据、年度观测值、Decimal 确定性、分页、缺失值、错误分类；
   - 不创建 MacroSeries / MacroObservation 表、不引入 migration、不写数据库；`MacroFetchResult` 只是内存快照；
   - 一旦持久化，就承担归档格式、快照版本、重跑语义等承诺；在获取语义稳定前不固化这些承诺。
3. **MacroDataProvider 契约只描述"获取结果"**。
   - `MacroQuery`（provider_key / indicator_code / country_code / start_year / end_year）描述一次查询；`MacroIndicator`、`MacroGeography` 为元数据；`MacroObservation` 为单条年度观测；`MacroPageInfo` 为分页；`MacroFetchResult` 为完整获取快照；
   - 契约不含 page/per_page/format/source 等 Provider 内部参数，天然禁止调用方控制请求细节；
   - 契约校验强制：country 规范化大写且拒绝 `ALL`（本阶段只支持单一国家）；年份闭区间 1960—当前年、最多 60 年；period 固定四位年份、period_start 固定该年 1 月 1 日；value 只能由 Decimal 构造、`value=None ⇔ is_missing=True`。
4. **World Bank 作为第一个 Macro Provider**。
   - 提供免费、无需认证、无频控的官方 Indicators API V2，适合验证"官方 API → 契约"的最小通路；
   - `WorldBankProvider` 从 Source Registry 读取快照（authority_tier / critical_claim_eligible / provider_capabilities / allowed_domains），acquisition_method 固定 `official_api`；
   - 只支持 `annual` 频率与 `country` 地理类型；不支持地区聚合、收入组或世界总量。
5. **固定 Indicators API V2（`api.worldbank.org/v2`）**。
   - 官方公开、稳定、长期维护；V2 是当前主流版本，分页/过滤语义明确；
   - 不调用任何非官方镜像、导出或内部数据服务接口。
6. **固定 `source=2`（World Development Indicators）**。
   - WDI 是 World Bank 标准指标库，SP.POP.TOTL 等核心人口/经济指标均在此源下；
   - `MacroFetchResult.source_id` 当前固定为 `"2"`，保证结果来源可追溯且单一。
7. **不允许调用方传任意 endpoint / query 参数**。
   - 客户端只暴露 `fetch_indicator_metadata` / `fetch_country_metadata` / `fetch_observations` 三个固定模板（URL 在代码内部构造），不接受任意 endpoint、任意 query；
   - format/source/date/page/per_page 全部由客户端固定，调用方无法通过 MacroQuery 之外的方式控制请求；
   - 这保证请求形状可审计、无参数注入面。
8. **数值使用 Decimal 并保持确定性**。
   - JSON 解析使用 `json.loads(..., parse_float=Decimal)`，禁止先转 float 再构造；
   - 观测 `value` 只允许 Decimal 构造，非有限值拒绝；`decimal_scale` 从 `as_tuple().exponent` 推导原始小数位数；
   - 不做单位换算、不除以百万/十亿、不计算同比/环比/CAGR；CLI 输出 Decimal 为字符串，禁止转 float；
   - 目的：人口等宏观数值可超 2^53，float 中间转换会丢精度；确定性需要十进制全程。
9. **保留 null observation（缺失值）**。
   - World Bank 对部分年份返回 `value=null`；这些记录保留为 `is_missing=True` 的 MacroObservation，不丢弃；
   - 缺失记录提供"该年确实没有该指标"的完整时间轴，2C.2 持久化后可区分"无数据"与"未查询"。
10. **不补齐缺失年份**。
    - 只返回 Provider 实际给出的观测；对缺失年份不插值、不生成占位记录；
    - 补齐年份会在时间轴与原始来源之间引入推断信息，破坏证据可追溯性。
11. **分页与请求上限**。
    - 单次 MacroQuery 总请求 ≤ `REQUEST_LIMIT=20`（超出报 `request_limit_exceeded`）；
    - `per_page=1000` 从 page=1 开始分页；响应 page 必须匹配请求 page；pages 不能跨请求增长/缩小；pages 上限 `MAX_PAGES=20`；
    - 跨页合并按 `(value, is_missing, observation_status)` 去重，同 period 冲突报 `response_conflict`；
    - 保留首页 page_info（含 Provider total），合并结果按 period_start 升序稳定排序。
12. **Provider 策略快照**。
    - 第一次短 Session 只读 Source Registry 中 `world_bank` 配置并立即关闭；网络 I/O 期间不持有 AsyncSession；
    - 快照校验：Provider 存在、enabled、含 `macro_data` 能力、含 `official_api` 获取方式、不要求 API Key；任一不满足报 `provider_not_ready`；
    - 快照字段（authority_tier / critical_claim_eligible / capabilities / allowed_domains）随 `MacroFetchResult` 返回，供调用方决策；
    - 持久化的 Provider 快照属于阶段 2C.2。
13. **HTTP 安全规则（WorldBankClient）**。
    - 仅 https；host 固定 `api.worldbank.org`，且 URL 仍必须通过 Source Registry `is_url_allowed` allowlist 校验（子域匹配 `worldbank.org`）；
    - 固定 v2 + source=2；`trust_env=False`；不发送 Cookie / Authorization / API Key；不接受用户 Header；`follow_redirects=False`，同 allowlist 重定向最多 3 次、跨 allowlist 拒绝、循环拒绝；
    - 不自动重试；connect/read/write/pool 超时明确设置；单响应正文流式读取上限 5 MiB；Content-Type 必须为 `application/json`；
    - 日志只记录 provider_key、hostname、status、duration_ms、operation、page，不记录完整 URL query 与响应正文；
    - 错误分类稳定：`provider_not_ready` / `request_failed` / `response_too_large` / `invalid_content_type` / `invalid_json` / `api_error` / `malformed_response` / `response_conflict` / `request_limit_exceeded`。
14. **开发期 CLI `fetch_world_bank_macro`**。
    - 从 Source Registry 读取 world_bank 快照，输出 JSON 报告到 stdout（日志走 stderr）；Decimal → 字符串、date/datetime → ISO；
    - 不写数据库、不保存响应正文、不写本地文件；失败输出稳定 error code；退出码：0 成功 / 2 输入错误 / 3 Provider 配置错误 / 4 API/网络/响应错误。
15. **当前结果不是 Evidence**。
    - `MacroFetchResult` 不创建 Evidence、Claim、Report 或 DocumentChunk，不进 LangGraph 编排；
    - 只有 2C.2 完成原始响应归档、来源快照与持久化后，宏观数据才能进入 Evidence/Claim 管线。
16. **2C.2 如何持久化并归档原始 JSON**。
    - 为宏观数据建立独立持久化（不同于公司 PDF SourceRecord）：指标/国家元数据与观测值表 + Provider 快照表；
    - 原始 JSON 响应归档到专用存储路径（不扩展 RawArtifact 的 PDF 语义），保留 SHA-256 与获取时间；
    - 后续阶段再决定从归档重建观测、增量更新与版本对比。
17. **为什么 FRED 与 NBS 延后**。
    - FRED 需要 API Key、指标/频率语义更复杂（月度/季度/每日、多个 source），先由 World Bank 验证契约后再演进；
    - 国内官方宏观数据（NBS 等）接入形态尚未确认（可能要求注册或非文档化接口），不在本阶段启动；
    - 本阶段只支持 `annual` 频率，为 FRED 月度/季度留下显式演进路径（MacroFrequency 枚举扩展）。

## 后果

- 建立独立的宏观数据领域契约与受控客户端，与公司披露来源（SourceRecord / RawArtifact）解耦。
- 单元测试 146 项（contracts / parser / client / pagination / provider / CLI 六层），全部使用 MockTransport，共享顶层网络 Guard（真实 sync/async 外网仍被禁止）。
- **受控真实验收（§十一，2026-08-07）在本环境无法成功执行**：按规范命令运行 CLI `fetch_world_bank_macro --country CHN --indicator SP.POP.TOTL --start-year 2020 --end-year 2024` 返回 `{"error":"request_failed"}`（exit 4）。DB 前置条件已满足（`source_providers.world_bank` 存在：enabled / authority_tier=1 / requires_api_key=false / allowed_domains=['worldbank.org']，只读验证），唯一受阻点是本机网络对 `worldbank.org` 的域名级出口阻断：DNS 被劫持到合成地址（28.0.0.x）、TLS 握手被丢弃（curl schannel、openssl read 0 bytes、`--resolve` 直连真实 Cloudflare IP 172.64.145.25 仍失败）、明文 HTTP 空回复。验收命令、断言不变量与脚本已记录，可在具备 World Bank 出网的环境补跑；在跑通前 2C.1 视为"当前进行"而非"已完成"。
- 遗留边界：2C.1 不写数据库、不创建表/migration；宏观数据不是 Evidence；FRED、NBS、月度/季度频率、多国家查询均未实现。
