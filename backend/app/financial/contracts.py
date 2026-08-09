"""Financial metric observation contracts (stage 4B.2A).

目标：把来源于真实财务 Evidence 的**原始财务数值**登记为
`FinancialMetricObservation`，用于后续（4B.2B）确定性财务计算。本阶段
**Document Evidence → Observation**，不计算同比 / 环比 / margin / ratio、
不调用 LLM、不自动从 PDF 表格猜财务数字。

冻结：
- `FINANCIAL_METRIC_SCHEMA_VERSION = 1`；v1 metric_code 先少而精（11 个科目，
  见 `MetricCode` / `statement_family`），不一次写几十个科目。
- `StatementScope`（consolidated / parent）、`PeriodKind`（duration / instant）、
  `RawUnit`（yuan / thousand_yuan / ten_thousand_yuan / hundred_million_yuan）；
  货币 v1 只支持 CNY（`normalized_value_cny`）。
- metric_code → statement family 用**确定性 mapping**（income statement /
  cash flow / balance sheet），**不让 caller 传 statement_type**，避免
  metric/type 不一致。
- period 规则：balance sheet → period_kind=instant、period_start=NULL；
  income/cash-flow → period_kind=duration、period_start NOT NULL 且 <=
  period_end。Service 根据 metric_code 确定 expected period_kind。
- `FinancialMetricDraft` 只允许提供语义输入（company_id /
  source_evidence_card_id / metric_code / statement_scope / period_start /
  period_end / source_value_text / raw_unit）；**不得**提供 raw_value /
  normalized_value_cny / provider / source_id / authority tier / fingerprint。
- `metric_fingerprint` = canonical JSON + SHA-256（不含 metric_observation_id /
  created_at）；同一完全相同 observation → replay 同一行；value / unit /
  period / metric code / source evidence 任一变化 → 新 observation，旧行保留。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from app.financial.errors import FinancialMetricInputError

# financial_metric_observations.metric_schema_version 的当前值（改结构时递增）。
FINANCIAL_METRIC_SCHEMA_VERSION = 1


class MetricCode(StrEnum):
    """v1 冻结 metric_code（先少而精，不一次写几十个科目）。"""

    REVENUE = "revenue"
    OPERATING_COST = "operating_cost"
    OPERATING_PROFIT = "operating_profit"
    PROFIT_BEFORE_TAX = "profit_before_tax"
    NET_PROFIT = "net_profit"
    NET_PROFIT_PARENT = "net_profit_parent"
    NET_PROFIT_PARENT_EXCL_NONRECURRING = "net_profit_parent_excl_nonrecurring"
    OPERATING_CASH_FLOW_NET = "operating_cash_flow_net"
    TOTAL_ASSETS = "total_assets"
    TOTAL_LIABILITIES = "total_liabilities"
    EQUITY_PARENT = "equity_parent"


class StatementFamily(StrEnum):
    """metric_code → statement family（确定性 mapping，caller 不传 statement_type）。"""

    INCOME_STATEMENT = "income_statement"
    CASH_FLOW = "cash_flow"
    BALANCE_SHEET = "balance_sheet"


class StatementScope(StrEnum):
    """报表口径：合并 / 母公司。"""

    CONSOLIDATED = "consolidated"
    PARENT = "parent"


class PeriodKind(StrEnum):
    """期间性质：duration（利润表/现金流量表，含期初-期末区间）；
    instant（资产负债表，期末时点）。"""

    DURATION = "duration"
    INSTANT = "instant"


class RawUnit(StrEnum):
    """source_value_text 的原始单位（货币 v1 只支持 CNY）。"""

    YUAN = "yuan"
    THOUSAND_YUAN = "thousand_yuan"
    TEN_THOUSAND_YUAN = "ten_thousand_yuan"
    HUNDRED_MILLION_YUAN = "hundred_million_yuan"


# metric_code → statement family（income / cash flow / balance sheet）。
_METRIC_FAMILY: dict[MetricCode, StatementFamily] = {
    MetricCode.REVENUE: StatementFamily.INCOME_STATEMENT,
    MetricCode.OPERATING_COST: StatementFamily.INCOME_STATEMENT,
    MetricCode.OPERATING_PROFIT: StatementFamily.INCOME_STATEMENT,
    MetricCode.PROFIT_BEFORE_TAX: StatementFamily.INCOME_STATEMENT,
    MetricCode.NET_PROFIT: StatementFamily.INCOME_STATEMENT,
    MetricCode.NET_PROFIT_PARENT: StatementFamily.INCOME_STATEMENT,
    MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING: StatementFamily.INCOME_STATEMENT,
    MetricCode.OPERATING_CASH_FLOW_NET: StatementFamily.CASH_FLOW,
    MetricCode.TOTAL_ASSETS: StatementFamily.BALANCE_SHEET,
    MetricCode.TOTAL_LIABILITIES: StatementFamily.BALANCE_SHEET,
    MetricCode.EQUITY_PARENT: StatementFamily.BALANCE_SHEET,
}


def supported_metric_codes() -> tuple[MetricCode, ...]:
    """v1 支持的全部 metric_code（冻结顺序）。"""
    return tuple(_METRIC_FAMILY)


def statement_family(metric_code: MetricCode) -> StatementFamily:
    """metric_code → statement family（确定性 mapping，非法 code → InputError）。"""
    if not isinstance(metric_code, MetricCode):
        raise FinancialMetricInputError("metric_code 必须是 MetricCode")
    family = _METRIC_FAMILY.get(metric_code)
    if family is None:
        raise FinancialMetricInputError(f"不支持 metric_code: {metric_code}")
    return family


def expected_period_kind(metric_code: MetricCode) -> PeriodKind:
    """根据 metric_code 的 statement family 确定 expected period_kind：
    balance sheet → instant；income statement / cash flow → duration。"""
    return (
        PeriodKind.INSTANT
        if statement_family(metric_code) == StatementFamily.BALANCE_SHEET
        else PeriodKind.DURATION
    )


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class FinancialMetricDraft:
    """调用方提交的财务数值语义输入（构造时校验，不可变）。

    只允许提供：company_id / source_evidence_card_id / metric_code /
    statement_scope / period_start / period_end / source_value_text / raw_unit。
    **不得**提供 raw_value / normalized_value_cny / provider / source_id /
    authority tier / fingerprint（由 FinancialMetricService 从真实 Evidence
    确定性派生）。

    - source_value_text：trim 后非空；必须是 EvidenceCard.quote_text 的
      **exact substring**（Service 校验，禁止 fuzzy / normalize / LLM 修正）；
    - period 规则（balance → instant + NULL start；income/cash-flow →
      duration + start <= end）由 Service 根据 metric_code 校验并抛
      `FinancialMetricPeriodError`。
    """

    company_id: UUID
    source_evidence_card_id: UUID
    metric_code: MetricCode
    statement_scope: StatementScope
    period_start: date | None
    period_end: date
    source_value_text: str
    raw_unit: RawUnit

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise FinancialMetricInputError("company_id 必须是 UUID")
        if isinstance(self.source_evidence_card_id, bool) or not isinstance(
            self.source_evidence_card_id, UUID
        ):
            raise FinancialMetricInputError("source_evidence_card_id 必须是 UUID")
        if not isinstance(self.metric_code, MetricCode):
            raise FinancialMetricInputError("metric_code 必须是 MetricCode")
        statement_family(self.metric_code)  # 校验 code 在 v1 支持列表内
        if not isinstance(self.statement_scope, StatementScope):
            raise FinancialMetricInputError("statement_scope 必须是 StatementScope")
        if self.period_start is not None and (
            isinstance(self.period_start, bool) or not isinstance(self.period_start, date)
        ):
            raise FinancialMetricInputError("period_start 必须是 date 或 None")
        if isinstance(self.period_end, bool) or not isinstance(self.period_end, date):
            raise FinancialMetricInputError("period_end 必须是 date")
        if self.period_start is not None and self.period_start > self.period_end:
            raise FinancialMetricInputError("period_start 必须 <= period_end")
        value_text = self.source_value_text.strip()
        if not value_text:
            raise FinancialMetricInputError("source_value_text 不能为空（trim 后）")
        if not isinstance(self.raw_unit, RawUnit):
            raise FinancialMetricInputError("raw_unit 必须是 RawUnit")
        object.__setattr__(self, "source_value_text", value_text)


@dataclass(frozen=True)
class FinancialMetricResult:
    """一次 create_observation 的结果摘要（不含任何 evidence 正文 / 数值细节）。"""

    metric_observation_id: UUID
    metric_fingerprint: str
    replayed: bool


def compute_metric_fingerprint(
    *,
    metric_schema_version: int,
    company_id: UUID,
    source_evidence_card_id: UUID,
    metric_code: str,
    statement_scope: str,
    period_start: date | None,
    period_end: date,
    period_kind: str,
    source_value_text: str,
    raw_value: Decimal,
    raw_unit: str,
    normalized_value_cny: Decimal,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：metric_schema_version / company_id / source_evidence_card_id /
    metric_code / statement_scope / period_start / period_end / period_kind /
    source_value_text / raw_value（canonical decimal string）/ raw_unit /
    normalized_value_cny（canonical decimal string）。

    **不得包含** metric_observation_id / created_at。同一完全相同 observation
    → 同一指纹 → replay 同一行；value / unit / period / metric code / source
    evidence 任一变化 → 新指纹 → 新行，旧行保留（修订 = 新 observation，
    无 update API）。Decimal 用 str() 序列化（规范形式，无 float 精度歧义）。
    """
    payload = {
        "metric_schema_version": metric_schema_version,
        "company_id": str(company_id),
        "source_evidence_card_id": str(source_evidence_card_id),
        "metric_code": metric_code,
        "statement_scope": statement_scope,
        "period_start": period_start.isoformat() if period_start is not None else None,
        "period_end": period_end.isoformat(),
        "period_kind": period_kind,
        "source_value_text": source_value_text,
        "raw_value": str(raw_value),
        "raw_unit": raw_unit,
        "normalized_value_cny": str(normalized_value_cny),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
