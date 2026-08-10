"""Valuation data & comparison errors (stage 4C.2A).

稳定 `code` 供上游（4C.2B Relative Valuation Analyst / Stage 5 Audit / API）
稳定处理；错误消息不泄露 evidence 正文 / prompt / key / DB URL / raw content。
"""


class ValuationError(Exception):
    """Valuation 服务错误基类。"""

    code = "valuation_error"


class ValuationInputError(ValuationError):
    """draft 输入不合法（类型 / 空值 / 枚举越界 / peer 集合非法等）。"""

    code = "valuation_input_error"


class ValuationObservationEvidenceMismatch(ValuationError):
    """observation 绑定的 EvidenceCard 与 draft 不一致：缺失 / 跨公司 /
    origin 非 document_chunk / evidence_type 非 metric。不自动修复。"""

    code = "valuation_observation_evidence_mismatch"


class ValuationObservationNotFound(ValuationError):
    """comparison 引用的 observation（target / peer）不存在。"""

    code = "valuation_observation_not_found"


class ValuationCompanyNotFound(ValuationError):
    """comparison 引用的公司不存在。"""

    code = "valuation_company_not_found"


class ValuationCompanyMismatch(ValuationError):
    """draft.target_company_id 与 target observation 的公司不一致。"""

    code = "valuation_company_mismatch"


class ValuationPeerDuplicateError(ValuationError):
    """比较集合内存在重复 peer 公司（显式 peer 集合必须互不相同公司）。

    公司去重以真实 Observation 的 company_id 为准，不做自动过滤 / 自动修正。
    """

    code = "valuation_peer_duplicate"


class ValuationPeerIncludesTargetError(ValuationError):
    """peer 集合包含了 target 公司（peer 必须与 target 公司不同）。"""

    code = "valuation_peer_includes_target"


class ValuationValueNotFound(ValuationError):
    """source_value_text.strip() 不是 quote_text 中任何一个完整数字 token。

    与 Financial 同一 numeric grammar（find_financial_number_tokens）：禁止
    substring partial match（"市盈率30倍" 里 "30" 接受而 "3" / "0" 不是 token）。
    """

    code = "valuation_value_not_found"


class ValuationValueAmbiguous(ValuationError):
    """source_value_text.strip() 匹配 quote_text 中 >1 个完整数字 token。"""

    code = "valuation_value_ambiguous"


class ValuationValueNotNumeric(ValuationError):
    """source_value_text 无法按 v1 numeric grammar 解析为 Decimal（拒绝科学
    计数 / 百分号 / 中文数字 / 约 / 亿 / 万 等）。"""

    code = "valuation_value_not_numeric"


class ValuationStorageRangeError(ValuationError):
    """metric_value / 派生比较数值超出 NUMERIC(38,12) 存储范围。

    小数位 > 12 或 abs >= 10^26：PG 会静默 rounding / overflow，必须在应用层
    显式拒绝（禁止静默 quantize / round / truncate）。
    """

    code = "valuation_storage_range_error"


class ValuationMetricNotComparable(ValuationError):
    """relative valuation comparison v1 只接受 metric_value > 0 的 observation。

    0 / 负倍数可以作为来源事实快照存储，但不能参与相对估值比较（否则 median /
    premium 失去解释力）。
    """

    code = "valuation_metric_not_comparable"


class ValuationMetricMismatch(ValuationError):
    """比较集合内存在不同 metric_code（必须全部同一指标）。"""

    code = "valuation_metric_mismatch"


class ValuationDateMismatch(ValuationError):
    """比较集合内存在不同 metric_as_of（必须全部同一市场观测日）。

    严格 same-date，不自动就近交易日对齐。
    """

    code = "valuation_date_mismatch"


class ValuationFutureEvidence(ValuationError):
    """no-lookahead：某 observation 的来源文档 availability（SourceRecord.
    published_at 否则 acquired_at）晚于 analysis_as_of。绝不用
    reporting_period_end。"""

    code = "valuation_future_evidence"


class ValuationIntegrityError(ValuationError):
    """replay 校验发现既有 observation / comparison 损坏。不自动 repair。"""

    code = "valuation_integrity_error"


class ValuationPersistenceFailed(ValuationError):
    """持久化事务失败（已整批回滚，0 partial write）。"""

    code = "valuation_persistence_failed"
