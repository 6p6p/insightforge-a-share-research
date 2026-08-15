"""Pydantic contracts for user-supplied financial observations (V1.1 closure).

用户从官方报告**人工转录**财务数值的提交契约：
- 系统先创建 user_supplied EvidenceCard（quote = 用户粘贴的原文引文，必须
  包含 `source_value_text` 这个精确数字 token——确定性解析的 provenance）；
- 再创建 FinancialMetricObservation（绑定该证据卡，Tier-4 / critical_claim_
  eligible=False 快照，绝不伪装成官方自动提取）。

**禁止** LLM 编造数字：所有数值必须来自用户粘贴的引文原文。
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.source_records import SourceDocumentType
from app.financial.contracts import MetricCode, RawUnit, StatementScope


class UserSuppliedFinancialObservationRequest(BaseModel):
    """用户转录财务数值提交（evidence 卡 + metric observation 一次完成）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    metric_code: MetricCode
    statement_scope: StatementScope = StatementScope.CONSOLIDATED
    period_start: date | None = None
    period_end: date
    raw_unit: RawUnit
    # source_value_text：引文中的**精确数字 token**（与 FinancialMetricDraft
    # 同一 grammar；系统校验其是 quote_text 的唯一完整数字 token）。
    source_value_text: str = Field(min_length=1, max_length=100)
    # quote_text：用户粘贴的原文引文（含 source_value_text 数字 token）。
    quote_text: str = Field(min_length=1, max_length=2000)
    evidence_statement: str = Field(min_length=1, max_length=2000)
    source_title: str = Field(min_length=1, max_length=500)
    source_url: str | None = Field(default=None, max_length=2000)
    document_type: SourceDocumentType = SourceDocumentType.OTHER

    @field_validator("period_start", "period_end")
    @classmethod
    def _check_dates(cls, value: date | None) -> date | None:
        if value is not None and isinstance(value, bool):
            raise ValueError("period 必须是 date")
        return value

    @field_validator("source_url")
    @classmethod
    def _check_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("source_url 必须是 https URL")
        return value


class UserSuppliedFinancialObservationResponse(BaseModel):
    """提交结果（evidence 卡 + metric observation 的确定性身份）。"""

    evidence_card_id: UUID
    source_id: UUID
    metric_observation_id: UUID
    metric_fingerprint: str
    replayed: bool
