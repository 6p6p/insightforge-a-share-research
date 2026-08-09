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


class FinancialMetricValueNotFound(FinancialMetricError):
    """source_value_text 不是 EvidenceCard.quote_text 的 exact substring。"""

    code = "financial_metric_value_not_found"


class FinancialMetricValueAmbiguous(FinancialMetricError):
    """source_value_text 在 quote_text 中出现 >1 次，无法确定取哪一个。"""

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
