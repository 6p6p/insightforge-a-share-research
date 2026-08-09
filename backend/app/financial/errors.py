"""Financial metric observation errors (stage 4B.2A).

稳定 `code` 供上游（未来 4B.2B 确定性计算 / 4B.2C Financial Analyst / Stage 5）
稳定处理；错误消息不泄露 evidence 正文 / prompt / key / DB URL / raw content。
"""


class FinancialMetricError(Exception):
    """FinancialMetricService 错误基类。"""

    code = "financial_metric_error"


class FinancialMetricInputError(FinancialMetricError):
    """draft 输入不合法（类型 / 空值 / 枚举越界等）。"""

    code = "financial_metric_input_error"


class FinancialMetricEvidenceMismatch(FinancialMetricError):
    """Evidence 与 draft 不一致：缺失 / 跨公司 / origin 非 document_chunk /
    evidence_type 非 metric。不自动修复。"""

    code = "financial_metric_evidence_mismatch"


class FinancialMetricPeriodError(FinancialMetricError):
    """period 与 metric_code 的 expected period_kind 不匹配（balance → instant
    且 period_start 必须 NULL；income/cash-flow → duration 且 period_start
    必须非空且 <= period_end）。"""

    code = "financial_metric_period_error"


class FinancialMetricScopeError(FinancialMetricError):
    """母公司指标只能登记 consolidated 口径。

    net_profit_parent / net_profit_parent_excl_nonrecurring / equity_parent
    只允许 statement_scope=consolidated。这是**结构化口径语义政策**（结构化
    策略约束），不自动识别报表口径——只做白名单口径校验，不推断真实口径。
    """

    code = "financial_metric_scope_error"


class FinancialMetricValueNotFound(FinancialMetricError):
    """source_value_text.strip() 不是 quote_text 中任何一个完整数字 token。

    与 `find_financial_number_tokens`（= parse 同一 grammar）对齐：禁止 substring
    partial match（"收入1000万元" 里 "100" / "000" 不是 token）。
    """

    code = "financial_metric_value_not_found"


class FinancialMetricValueAmbiguous(FinancialMetricError):
    """source_value_text.strip() 匹配 quote_text 中 >1 个完整数字 token。"""

    code = "financial_metric_value_ambiguous"


class FinancialMetricValueNotNumeric(FinancialMetricError):
    """source_value_text 无法按 v1 语法解析为 Decimal（拒绝科学计数 /
    百分号 / 中文数字 / 约 / 亿 / 万元 等）。"""

    code = "financial_metric_value_not_numeric"


class FinancialMetricIntegrityError(FinancialMetricError):
    """replay 校验发现既有 observation 损坏。不自动 repair。"""

    code = "financial_metric_integrity_error"


class FinancialMetricPersistenceFailed(FinancialMetricError):
    """持久化事务失败（已整批回滚，0 partial write）。"""

    code = "financial_metric_persistence_failed"


class FinancialMetricStorageRangeError(FinancialMetricError):
    """raw_value / normalized_value_cny 超出 NUMERIC(38,12) 存储范围。

    小数位 > 12 或 abs >= 10^26：PG 会静默 rounding / overflow，必须在应用层
    显式拒绝（禁止 quantize / round / truncate 后落库）。
    """

    code = "financial_metric_storage_range_error"
