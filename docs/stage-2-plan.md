# 阶段 2 计划概览

> 阶段 2 各子阶段目标概览；详细接口在对应阶段冻结。

## 2A：CompanyIdentity、Source Registry、获取策略（当前）

- 公司标准身份（ExchangeCode/MarketBoard/identity_key）、别名与来源登记。
- Source Registry：权威等级与获取方式分离，8 个默认 Provider，URL allowlist。

## 2B：官方公司披露文件 MVP、用户上传和 URL 导入

- 官方披露文件获取、用户上传、URL 导入。
- 不做通用爬虫；获取方式严格限定为官方 API / 官方下载 / 官方页面。

## 2C：宏观数据 Provider

- 只使用正式 API、官方下载或官方页面（NBS/FRED/World Bank 等）。

## 2D：新闻发现与大模型联网搜索兜底

- 搜索结果只是发现记录；原始页面才是证据来源。

## 2E：原始归档、哈希、解析、去重与阶段验收

- 统一归档 PDF、HTML、JSON、CSV、Excel 与上传文件。
