# ADR-0008：CompanyIdentity 与来源策略

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 决策

1. **CompanyIdentity 与 ResearchTask 分开**。
   - 公司身份是跨任务可复用的事实；ResearchTask 是用户的一次研究请求，两者职责不同。
2. **当前不把 company_id 写入 research_tasks**。
   - 避免 1A 的稳定契约被身份同步阻塞；解析后的关联在后续阶段以迁移字段加入。
3. **单独六位代码不根据前缀猜 exchange**。
   - A 股代码跨交易所可能不唯一；只做精确查询（所有 exchange），唯一命中 resolved、多个 ambiguous、无则 not_found。
4. **identity_key 使用 EXCHANGE:CODE**。
   - 稳定、可推导、可被数据库 CHECK 校验（`identity_key = exchange || ':' || security_code`）。
5. **名称只做精确 Alias**。
   - 不做模糊匹配、LIKE、拼音或编辑距离，避免误配与隐式启发。
6. **Alias 必须有来源和有效期**。
   - 每条别名记录 `source_provider_key`、`source_url`、`valid_from/to`，可追溯。
7. **来源权威性与获取方式分开**。
   - `authority_tier` 回答"来源本身是否权威"；`acquisition_method` 回答"这次通过什么方式获得"；两者不可合并。
8. **模型联网搜索是 discovery method，不是 SourceProvider**。
   - 搜索引擎与 LLM 不是原始事实发布者；`model_web_search_discovery` 只是获取候选 URL 的方式，最终来源仍是交易所/监管等原始发布者。
9. **搜索摘要不能单独支撑关键结论**。
   - 原始页面（官方披露文件）才是证据来源；摘要只用于发现与定位。
10. **Registry 不保存爬虫细节**。
    - 不保存 CSS selector、XPath、Cookie、Token、隐藏 API 参数或反爬方案；只登记来源、权威级别、能力、允许域名与公开获取方式。
11. **NBS 当前不标记 official_api**。
    - 未确认正式公开 API 文档；只登记官方页面与文件下载。
12. **FRED 标记 requires_api_key**。
    - FRED API 需要 key；字段只说明外部配置需求，不保存密钥。
13. **阶段 2A 不执行网络请求**。
    - 只建立 CompanyIdentity 契约与 Source Registry，不抓取公告、不同步公司目录、不调用任何外部服务。
14. **阶段 2B 先实现官方披露文件 + 用户上传/URL 导入**。
    - 不做通用爬虫；获取方式严格限定为官方 API、官方下载或官方页面。
15. **suspended 是交易状态，不属于 CompanyListingStatus**。
    - `CompanyListingStatus` 只表达公司生命周期事实：`listed / delisted / unknown`。停牌、退市整理等交易/行情状态不属于公司身份，当前领域不包含；后续如需要另建交易状态模型，不混入公司身份。
16. **critical_claim_eligible 只在 Provider 声明能力范围内有效**。
    - `critical_claim_eligible=true` 表示该 Provider 登记的能力可支撑关键结论，前提是结论落在其 `capabilities` 之内；能力范围之外的关键结论不能引用该 Provider 作为证据来源。
17. **FRED 的 macro_data 资格不覆盖公司层事实**。
    - FRED 只登记 `macro_data` 能力；其 `critical_claim_eligible` 只对宏观数据成立，不能用来支撑公司公告或公司财务事实。公司层事实只能引用登记了公司能力（company_directory / company_announcement / issuer_ir 等）的 Provider。
18. **阶段 2B 创建 CompanyIdentity/CompanyAlias 时的来源约束**。
    - `source_provider_key` 必须指向 Source Registry 中已存在的 Provider；
    - `source_url` 必须通过该 Provider `allowed_domains` 校验（`is_url_allowed`）；
    - `user_upload` 是 acquisition_method（获取方式），不代表存在名为 "user" 的 SourceProvider；用户上传内容的原始事实来源仍是其发布者。

## 后果

- 公司解析是确定性、可测试的（精确匹配，无启发式）。
- Source Registry 只登记"谁、多权威、支持什么、允许哪些域名、怎么获取"，为阶段 2B 的官方披露文件获取提供稳定基础。
