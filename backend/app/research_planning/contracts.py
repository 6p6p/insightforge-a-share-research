"""Research planning contracts (stage 7A.1): ResearchPlan schema v1 + bounded vocabulary.

Research Planner 只回答「这个研究问题需要研究什么」——输出 **semantic research
needs**，**不输出任何内部 ID**（EvidenceCard / SourceRecord / RawArtifact /
Chunk / Calculation / Comparison / provider URL / SQL / Chroma metadata）。

bounded vocabulary（spec E，不让模型无限创造字符串）：
- `research_scope` / `analysis_modules` 只允许当前已实现 Analyst 的域；
- `document_needs.source_type` 复用项目真实 source vocabulary
  （SourceDocumentType 值 + macro_dataset）；
- `financial_needs.metric_code` 复用 `app.financial.contracts.MetricCode`
  （FinancialMetricObservation 真实支持的 11 个科目）；
- `valuation_needs.metric_code` 只允许 pe_ttm / pb_mrq / ps_ttm
  （relative_valuation_comparisons 表 CHECK 冻结集合）；
- 所有列表有明确 max 数量；Macro topic / purpose / focus 为受控短文本（长度 +
  数量限制）。

**禁止 internal ID-like 字段**：所有自由文本 field 拒绝 UUID（36 字符含 `-`）
与 64 hex fingerprint 形态（构造时 Pydantic 校验）。

计划本身只描述需要什么，**不假定数据已经存在、不输出事实结论、不给买卖建议**。
"""

import re
from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.claims.contracts import ClaimAnalysisDomain
from app.financial.contracts import MetricCode

# ---------------------------------------------------------------- schema 版本

# research_plans.plan_schema_version 的当前值（改 payload 结构时递增；已有计划
# 原样保留，新语义 → 新 fingerprint → 新行）。
RESEARCH_PLAN_SCHEMA_VERSION = 1

# planner prompt / 策略版本（spec H：prompt/strategy 版本进入 input fingerprint；
# 改 prompt → 新 fingerprint → 新计划，旧行保留）。
RESEARCH_PLAN_STRATEGY_VERSION = 1

# ---------------------------------------------------------------- enums

# research_scope 的允许值（复用 ClaimAnalysisDomain 的真实 Analyst 域）。
RESEARCH_SCOPES = (
    ClaimAnalysisDomain.BUSINESS.value,
    ClaimAnalysisDomain.EVENT.value,
    ClaimAnalysisDomain.RISK.value,
    ClaimAnalysisDomain.FINANCIAL.value,
    ClaimAnalysisDomain.MACRO.value,
    ClaimAnalysisDomain.VALUATION.value,
)


class ResearchScope(StrEnum):
    """研究范围（6 类，值 = ClaimAnalysisDomain 值）。"""

    BUSINESS = ClaimAnalysisDomain.BUSINESS.value
    EVENT = ClaimAnalysisDomain.EVENT.value
    RISK = ClaimAnalysisDomain.RISK.value
    FINANCIAL = ClaimAnalysisDomain.FINANCIAL.value
    MACRO = ClaimAnalysisDomain.MACRO.value
    VALUATION = ClaimAnalysisDomain.VALUATION.value


class AnalysisModule(StrEnum):
    """analysis_modules：只允许当前已实现的 Analyst（Stage4 节点）。

    business_event = business + event 两域合并为一个 analyst 工作项（Stage4
    work item 允许按需拆分 business / event，这里用分析模块粒度）。
    """

    BUSINESS_EVENT = "business_event"
    RISK = ClaimAnalysisDomain.RISK.value
    FINANCIAL = ClaimAnalysisDomain.FINANCIAL.value
    MACRO = ClaimAnalysisDomain.MACRO.value
    VALUATION = ClaimAnalysisDomain.VALUATION.value


class ResearchDocumentNeedType(StrEnum):
    """document_needs.source_type：复用项目真实 source vocabulary。

    值对齐 `SourceDocumentType`（annual_report 等）+ macro_dataset
    （宏观数据集文档 / 官方 macro provider 数据）。
    """

    ANNUAL_REPORT = "annual_report"
    SEMIANNUAL_REPORT = "semiannual_report"
    QUARTERLY_REPORT = "quarterly_report"
    COMPANY_ANNOUNCEMENT = "company_announcement"
    ISSUER_IR_MATERIAL = "issuer_ir_material"
    PROSPECTUS = "prospectus"
    NEWS_ARTICLE = "news_article"
    MACRO_DATASET = "macro_dataset"
    OTHER = "other"


class ValuationNeedMetric(StrEnum):
    """valuation_needs.metric_code：只允许当前实现支持的估值 metric。

    = relative_valuation_comparisons 表 `metric_code` CHECK 冻结集合。
    """

    PE_TTM = "pe_ttm"
    PB_MRQ = "pb_mrq"
    PS_TTM = "ps_ttm"


VALUATION_METRICS = frozenset(m.value for m in ValuationNeedMetric)


class PeerPolicy(StrEnum):
    """valuation_needs.peer_policy：只允许当前确定性派生方法。"""

    PEER_MEDIAN = "peer_median"


# ---------------------------------------------------------------- 数量边界

MAX_DOCUMENT_NEEDS = 8
MAX_FINANCIAL_NEEDS = 12
MAX_MACRO_NEEDS = 6
MAX_EVENT_NEEDS = 6
MAX_VALUATION_NEEDS = 3  # pe / pb / ps 各一
MAX_FOCUS_ITEMS = 5
MAX_SCOPE_ITEMS = 6
MAX_ANALYSIS_MODULES = 5

# 受控短文本长度（防止 planner 输出长文本 / 幻觉事实）。
_MAX_FREE_TEXT = 80
_MAX_FOCUS_TEXT = 40
_MAX_GEOGRAPHY_TEXT = 20

# need_code 形态：小写字母开头，后续小写字母 / 数字 / 下划线。
_NEED_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# period 只允许年度形态（4 位数字）。
_PERIOD_PATTERN = re.compile(r"^[0-9]{4}$")
# 禁止的 internal ID-like 形态：UUID（36 字符含 `-`）与 64 hex fingerprint。
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _reject_internal_ids(text: str) -> str:
    """防御性拒绝 internal ID-like 文本（UUID / 64 hex fingerprint）。"""
    if _UUID_PATTERN.search(text):
        raise ValueError("planner 文本不得包含 UUID-like 内部 ID")
    if _HEX64_PATTERN.fullmatch(text):
        raise ValueError("planner 文本不得包含 fingerprint-like 内部 ID")
    return text


class CompanyIdentitySnapshot(BaseModel):
    """planner request 的 company 身份（**非内部 UUID**，spec D）。

    只含语义身份：security_code / official_name / exchange / board / aliases。
    由 preparation 从真实 Company 派生；planner 输入**不发送** company_id。
    """

    model_config = ConfigDict(frozen=True)

    security_code: str
    official_name: str
    exchange: str
    board: str
    aliases: list[str] = Field(default_factory=list)

    @field_validator("security_code", "official_name", "exchange", "board")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("company identity 字段不能为空（trim 后）")
        return text


class ResearchPlannerRequest(BaseModel):
    """Planner 入口（spec D）：输入只来自 ResearchTask + CompanyIdentity + as-of。

    - task_id：UUID（结果归属的 ResearchTask）；
    - company：语义身份快照；
    - research_question：trim 后非空；
    - analysis_as_of：分析基准日（no-lookahead 语义边界）。

    不发送：内部 UUID（除 task_id 外）、fingerprint、storage metadata、
    prompt history。
    """

    model_config = ConfigDict(frozen=True)

    task_id: UUID
    company: CompanyIdentitySnapshot
    research_question: str
    analysis_as_of: date

    @field_validator("research_question", mode="before")
    @classmethod
    def _strip_question(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _validate_question(self) -> "ResearchPlannerRequest":
        question = self.research_question.strip()
        if not question:
            raise ValueError("research_question 不能为空（trim 后）")
        _reject_internal_ids(question)
        return self


# ---------------------------------------------------------------- plan needs


class _NeedBase(BaseModel):
    """need 基类：need_code 唯一语义标识 + purpose 受控短文本。"""

    model_config = ConfigDict(frozen=True)

    need_code: str
    purpose: str

    @field_validator("need_code")
    @classmethod
    def _valid_need_code(cls, value: str) -> str:
        code = value.strip()
        if not _NEED_CODE_PATTERN.fullmatch(code):
            raise ValueError("need_code 必须是 [a-z][a-z0-9_]{0,31}")
        return code

    @field_validator("purpose")
    @classmethod
    def _valid_purpose(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("purpose 不能为空（trim 后）")
        if len(text) > _MAX_FREE_TEXT:
            raise ValueError(f"purpose 最多 {_MAX_FREE_TEXT} 字符")
        return _reject_internal_ids(text)


class DocumentNeed(_NeedBase):
    """document_needs：需要哪类披露/新闻文档。"""

    source_type: ResearchDocumentNeedType
    period: str | None = None

    @field_validator("period")
    @classmethod
    def _valid_period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        period = value.strip()
        if not _PERIOD_PATTERN.fullmatch(period):
            raise ValueError("period 必须是 4 位年度（如 2023）或 None")
        return period


class FinancialNeed(_NeedBase):
    """financial_needs：需要哪个财务科目（metric_code 复用 MetricCode）。"""

    metric_code: MetricCode
    period: str | None = None

    @field_validator("period")
    @classmethod
    def _valid_period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        period = value.strip()
        if not _PERIOD_PATTERN.fullmatch(period):
            raise ValueError("period 必须是 4 位年度（如 2023）或 None")
        return period


class MacroNeed(_NeedBase):
    """macro_needs：宏观 topic / indicator（受控短文本）+ 可选 geography。"""

    topic_or_indicator: str
    geography: str | None = None

    @field_validator("topic_or_indicator")
    @classmethod
    def _valid_topic(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("topic_or_indicator 不能为空（trim 后）")
        if len(text) > _MAX_FREE_TEXT:
            raise ValueError(f"topic_or_indicator 最多 {_MAX_FREE_TEXT} 字符")
        return _reject_internal_ids(text)

    @field_validator("geography")
    @classmethod
    def _valid_geography(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if len(text) > _MAX_GEOGRAPHY_TEXT:
            raise ValueError(f"geography 最多 {_MAX_GEOGRAPHY_TEXT} 字符")
        return _reject_internal_ids(text)


class EventNeed(_NeedBase):
    """event_needs：需要哪类事件（受控短文本）。"""

    topic: str

    @field_validator("topic")
    @classmethod
    def _valid_topic(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("topic 不能为空（trim 后）")
        if len(text) > _MAX_FREE_TEXT:
            raise ValueError(f"topic 最多 {_MAX_FREE_TEXT} 字符")
        return _reject_internal_ids(text)


class ValuationNeed(BaseModel):
    """valuation_needs：需要哪个估值 metric 的相对比较（peer_median）。"""

    model_config = ConfigDict(frozen=True)

    need_code: str
    metric_code: ValuationNeedMetric
    peer_policy: PeerPolicy = PeerPolicy.PEER_MEDIAN

    @field_validator("need_code")
    @classmethod
    def _valid_need_code(cls, value: str) -> str:
        code = value.strip()
        if not _NEED_CODE_PATTERN.fullmatch(code):
            raise ValueError("need_code 必须是 [a-z][a-z0-9_]{0,31}")
        return code


# ---------------------------------------------------------------- plan payload


class ResearchPlanPayload(BaseModel):
    """ResearchPlan schema v1 的 payload（plan_payload JSONB 内容）。

    全部列表有明确 max 数量；need_code 全局唯一；analysis_modules / research_scope
    只允许受控值。不输出内部 ID、不输出事实结论、不给买卖建议。
    """

    model_config = ConfigDict(frozen=True)

    research_scope: list[ResearchScope] = Field(default_factory=list)
    document_needs: list[DocumentNeed] = Field(default_factory=list)
    financial_needs: list[FinancialNeed] = Field(default_factory=list)
    macro_needs: list[MacroNeed] = Field(default_factory=list)
    event_needs: list[EventNeed] = Field(default_factory=list)
    valuation_needs: list[ValuationNeed] = Field(default_factory=list)
    analysis_modules: list[AnalysisModule] = Field(default_factory=list)
    research_focus: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_counts_and_codes(self) -> "ResearchPlanPayload":
        if not (1 <= len(self.research_scope) <= MAX_SCOPE_ITEMS):
            raise ValueError(f"research_scope 必须在 1..{MAX_SCOPE_ITEMS}")
        if not (1 <= len(self.analysis_modules) <= MAX_ANALYSIS_MODULES):
            raise ValueError(f"analysis_modules 必须在 1..{MAX_ANALYSIS_MODULES}")
        if len(self.document_needs) > MAX_DOCUMENT_NEEDS:
            raise ValueError(f"document_needs 最多 {MAX_DOCUMENT_NEEDS} 条")
        if len(self.financial_needs) > MAX_FINANCIAL_NEEDS:
            raise ValueError(f"financial_needs 最多 {MAX_FINANCIAL_NEEDS} 条")
        if len(self.macro_needs) > MAX_MACRO_NEEDS:
            raise ValueError(f"macro_needs 最多 {MAX_MACRO_NEEDS} 条")
        if len(self.event_needs) > MAX_EVENT_NEEDS:
            raise ValueError(f"event_needs 最多 {MAX_EVENT_NEEDS} 条")
        if len(self.valuation_needs) > MAX_VALUATION_NEEDS:
            raise ValueError(f"valuation_needs 最多 {MAX_VALUATION_NEEDS} 条")
        if len(self.research_focus) > MAX_FOCUS_ITEMS:
            raise ValueError(f"research_focus 最多 {MAX_FOCUS_ITEMS} 条")

        # need_code 全局唯一（跨所有 need 列表）。
        seen: set[str] = set()
        for code in (
            *(need.need_code for need in self.document_needs),
            *(need.need_code for need in self.financial_needs),
            *(need.need_code for need in self.macro_needs),
            *(need.need_code for need in self.event_needs),
            *(need.need_code for need in self.valuation_needs),
        ):
            if code in seen:
                raise ValueError(f"need_code 必须全局唯一: {code!r}")
            seen.add(code)

        # research_focus 条目受控短文本 + 拒绝 internal ID-like。
        for focus in self.research_focus:
            text = focus.strip()
            if not text:
                raise ValueError("research_focus 条目不能为空（trim 后）")
            if len(text) > _MAX_FOCUS_TEXT:
                raise ValueError(f"research_focus 条目最多 {_MAX_FOCUS_TEXT} 字符")
            _reject_internal_ids(text)
        return self

    def normalized_payload(self) -> dict:
        """canonical JSON payload（plan fingerprint 用；sort_keys + model_dump）。"""
        return self.model_dump(mode="json")
