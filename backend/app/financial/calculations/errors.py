"""Financial calculation errors (stage 4B.2B).

稳定 `code` 供上游（4B.2C Financial Analyst / Stage 5）稳定处理；错误消息不
泄露 observation 正文 / evidence 正文 / prompt / key / DB URL / raw content。
"""


class FinancialCalculationError(Exception):
    """FinancialCalculationService 错误基类。"""

    code = "financial_calculation_error"


class FinancialCalculationInputError(FinancialCalculationError):
    """draft 输入不合法（类型 / 空值 / role 集合 / 枚举越界等）。"""

    code = "financial_calculation_input_error"


class FinancialCalculationObservationNotFound(FinancialCalculationError):
    """draft 里的 input_observation_id 在 PG 中不存在。"""

    code = "financial_calculation_observation_not_found"


class FinancialCalculationCompanyMismatch(FinancialCalculationError):
    """输入 observation 的 company 与 draft 不一致（跨公司计算拒绝）。"""

    code = "financial_calculation_company_mismatch"


class FinancialCalculationScopeMismatch(FinancialCalculationError):
    """输入 observation 的 statement_scope 不完全相同（合并/母公司混合拒绝）。"""

    code = "financial_calculation_scope_mismatch"


class FinancialCalculationInputMismatch(FinancialCalculationError):
    """输入 observation 的 metric_code 与 role 期望不匹配（如 margin 需要
    revenue / operating_cost）；或 growth 类 current 与 baseline 的
    metric_code 不同。必须精确匹配，不自动纠错。"""

    code = "financial_calculation_input_mismatch"


class FinancialCalculationPeriodMismatch(FinancialCalculationError):
    """输入 observation 的 period 不满足可比性规则：

    - absolute_change：period_kind 必须相同；
    - YoY：月/日对应且 baseline 年份 = current 年份 - 1（duration 同时要求
      period_start 对应）；
    - QoQ：duration 必须是标准单季度且连续；instant 的 period_end 必须是
      03-31 / 06-30 / 09-30 / 12-31 且连续。
    """

    code = "financial_calculation_period_mismatch"


class FinancialCalculationGrowthBaseNotPositive(FinancialCalculationError):
    """增长率（同比 / 环比）的 baseline 必须 > 0（负数 / 0 的增长率无意义）。"""

    code = "financial_calculation_growth_base_not_positive"


class FinancialCalculationZeroDenominator(FinancialCalculationError):
    """ratio 公式的分母必须 > 0（revenue / total_assets），否则拒绝。"""

    code = "financial_calculation_zero_denominator"


class FinancialCalculationStorageRangeError(FinancialCalculationError):
    """result_value 超出 NUMERIC(38,12) 存储范围（小数位 > 12 或 abs >= 10^26）。

    禁止静默 quantize / round / truncate 后落库——数值失真必须在应用层显式拒绝。
    """

    code = "financial_calculation_storage_range_error"


class FinancialCalculationIntegrityError(FinancialCalculationError):
    """replay 校验发现既有 calculation 损坏。不自动 repair。"""

    code = "financial_calculation_integrity_error"


class FinancialCalculationPersistenceFailed(FinancialCalculationError):
    """持久化事务失败（已整批回滚，0 partial write）。"""

    code = "financial_calculation_persistence_failed"
