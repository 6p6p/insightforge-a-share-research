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
- 2B.2（后续）：官方公司披露文件自动获取。
- 不做通用爬虫；获取方式严格限定为官方 API / 官方下载 / 官方页面。阶段 2B.1 不执行外网请求、不解析 PDF 正文。

## 2C：宏观数据 Provider

- 只使用正式 API、官方下载或官方页面（NBS/FRED/World Bank 等）。

## 2D：新闻发现与大模型联网搜索兜底

- 搜索结果只是发现记录；原始页面才是证据来源。

## 2E：原始归档、哈希、解析、去重与阶段验收

- 统一归档 PDF、HTML、JSON、CSV、Excel 与上传文件。
