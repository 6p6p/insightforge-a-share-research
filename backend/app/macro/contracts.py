"""Macro data provider contracts (stage 2C.1).

宏观数据契约只描述"获取结果"：查询、指标元数据、国家元数据、年度观测值、
分页信息与获取快照。本阶段不写数据库、不持久化；结果不是 Evidence。

数值语义（确定性）：
- value 只能由 Decimal 构造，禁止 float 中间转换；
- null 观测保留为 is_missing=True，不插值、不补齐缺失年份；
- 不做单位换算、不除以百万/十亿、不计算同比/环比/CAGR；
- observation_status 只保存 Provider 原始状态，不推断 forecast/actual。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)

_MIN_YEAR = 1960
_MAX_YEAR_SPAN = 60  # 闭区间最多 60 年
_WDI_SOURCE_ID = "2"

_ALLOWED_PROVIDER_KEYS = frozenset({"world_bank"})
_INDICATOR_CODE_RE = re.compile(r"^[A-Z0-9._-]{1,64}$")
_COUNTRY_CODE_RE = re.compile(r"^[A-Za-z]{2,3}$")
_YEAR_RE = re.compile(r"^\d{4}$")

_ERROR_MSG = {
    "provider_key": f"provider_key 当前必须为 {sorted(_ALLOWED_PROVIDER_KEYS)}",
    "provider_key_blank": "provider_key 不能为空",
    "indicator_code": "indicator_code 必须为 1—64 位 ASCII 大写字母/数字/点/下划线/连字符",
    "country_code": "country_code 必须为两位或三位 ASCII 字母",
    "country_all": "country_code 不接受 all",
    "year_type": "start_year/end_year 必须是 int",
    "year_range": f"start_year/end_year 必须在 {_MIN_YEAR}—当前年份之间",
    "year_order": "start_year 必须不晚于 end_year",
    "year_span": f"年份闭区间最多 {_MAX_YEAR_SPAN} 年",
    "period_format": "period 必须为四位年份",
    "normalized_period_start": "normalized_period_start 必须为该年 1 月 1 日",
    "period_semantics": "period_semantics 当前必须为 provider_year_label",
    "frequency": "frequency 必须是 MacroFrequency",
    "decimal_only": "value 必须由 Decimal 构造，禁止 float",
    "value_not_finite": "value 必须是有限 Decimal",
    "missing_flag": "value 为 None 时 is_missing=true，否则 is_missing=false",
    "request_count": "request_count 不能为负",
    "acquisition_method": "acquisition_method 必须为 official_api",
    "source_id": "source_id 当前必须固定为 WDI 数据源 '2'",
    "indicator_source_consistency": "indicator.source_id 必须与 source_id 一致",
}


def current_macro_year() -> int:
    """当前日历年份，作为 MacroQuery 年份上限（动态，避免硬编码）。"""
    return date.today().year


def decimal_scale_of(value: Decimal) -> int:
    """返回 Decimal 的原始小数位数（exponent 为负时的绝对值；否则 0）。"""
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < 0:
        return -exponent
    return 0


class MacroFrequency(StrEnum):
    """宏观数据频率。当前只支持年度；月度/季度由后续 FRED 阶段显式演进。"""

    ANNUAL = "annual"


class MacroPeriodSemantics(StrEnum):
    """period 标签的语义。

    当前只有 provider_year_label：period 是 Provider 给出的年份标签，
    normalized_period_start 只是为排序/索引而规范化的 1 月 1 日，
    不表示 Provider 真实的统计周期起始日（如财政年度被归入的自然年份）。
    """

    PROVIDER_YEAR_LABEL = "provider_year_label"


class MacroGeographyType(StrEnum):
    """宏观数据地理粒度。当前只支持国家；不支持地区聚合、收入组或世界总量。"""

    COUNTRY = "country"


@dataclass(frozen=True)
class MacroQuery:
    """一次宏观数据查询。

    - 不允许调用方传入 page/per_page/format/source 等 Provider 内部参数
      （本契约不含这些字段，天然排除）；
    - 只支持单一国家，不接受 all / 分号分隔的多国家 / 聚合区域代码。
    """

    provider_key: str
    indicator_code: str
    country_code: str
    start_year: int
    end_year: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_key, str)
            or self.provider_key not in _ALLOWED_PROVIDER_KEYS
        ):
            raise ValueError(_ERROR_MSG["provider_key"])
        if not isinstance(self.indicator_code, str) or not _INDICATOR_CODE_RE.match(
            self.indicator_code
        ):
            raise ValueError(_ERROR_MSG["indicator_code"])
        if not isinstance(self.country_code, str) or not _COUNTRY_CODE_RE.match(self.country_code):
            raise ValueError(_ERROR_MSG["country_code"])
        normalized = self.country_code.upper()
        if normalized == "ALL":
            raise ValueError(_ERROR_MSG["country_all"])
        object.__setattr__(self, "country_code", normalized)
        if not isinstance(self.start_year, int) or not isinstance(self.end_year, int):
            raise ValueError(_ERROR_MSG["year_type"])
        upper = current_macro_year()
        if not (_MIN_YEAR <= self.start_year <= upper and _MIN_YEAR <= self.end_year <= upper):
            raise ValueError(_ERROR_MSG["year_range"])
        if self.start_year > self.end_year:
            raise ValueError(_ERROR_MSG["year_order"])
        if self.end_year - self.start_year + 1 > _MAX_YEAR_SPAN:
            raise ValueError(_ERROR_MSG["year_span"])


@dataclass(frozen=True)
class MacroTopic:
    """指标归属主题（World Bank topics 数组中的一项）。"""

    topic_id: str
    name: str


@dataclass(frozen=True)
class MacroIndicator:
    """指标元数据：来自 Provider 指标详情，与观测记录共享 external_indicator_id。"""

    provider_key: str
    external_indicator_id: str
    name: str
    unit: str
    source_id: str
    source_name: str
    source_note: str
    source_organization: str
    topics: tuple[MacroTopic, ...] = ()


@dataclass(frozen=True)
class MacroGeography:
    """国家元数据。

    - requested_code：查询中规范化后（大写）的请求代码；
    - provider_country_id：Provider 内部国家 id（World Bank 对真实国家即 ISO3）；
    - region_name / income_level_name 可空。
    """

    geography_type: MacroGeographyType
    requested_code: str
    provider_country_id: str
    iso2_code: str
    iso3_code: str
    name: str
    region_name: str | None = None
    income_level_name: str | None = None


@dataclass(frozen=True)
class MacroObservation:
    """一条年度观测值。

    - period 必须为四位年份（Provider 的年份标签）；
    - normalized_period_start 固定为 date(int(period), 1, 1)，只用于排序/索引与统一时间轴，
      不表示 Provider 真实统计周期起始日；
    - period_semantics 当前固定为 MacroPeriodSemantics.PROVIDER_YEAR_LABEL；
    - value 只能由 Decimal 构造；value=None 时 is_missing=true；
    - decimal_scale：value 为空时为空；否则记录 Decimal 原始小数位数（自动推导）；
    - observation_status 只保存 Provider 原始状态。
    """

    provider_key: str
    external_indicator_id: str
    geography_code: str
    period: str
    normalized_period_start: date
    frequency: MacroFrequency
    value: Decimal | None
    is_missing: bool
    period_semantics: MacroPeriodSemantics = MacroPeriodSemantics.PROVIDER_YEAR_LABEL
    observation_status: str | None = None
    decimal_scale: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_key, str) or not self.provider_key:
            raise ValueError(_ERROR_MSG["provider_key_blank"])
        if not isinstance(self.external_indicator_id, str) or not self.external_indicator_id:
            raise ValueError("external_indicator_id 不能为空")
        if not isinstance(self.geography_code, str) or not self.geography_code:
            raise ValueError("geography_code 不能为空")
        if not isinstance(self.period, str) or not _YEAR_RE.match(self.period):
            raise ValueError(_ERROR_MSG["period_format"])
        year = int(self.period)
        expected_start = date(year, 1, 1)
        if not isinstance(self.normalized_period_start, date) or (
            self.normalized_period_start != expected_start
        ):
            raise ValueError(_ERROR_MSG["normalized_period_start"])
        if self.period_semantics != MacroPeriodSemantics.PROVIDER_YEAR_LABEL:
            raise ValueError(_ERROR_MSG["period_semantics"])
        if not isinstance(self.frequency, MacroFrequency):
            raise ValueError(_ERROR_MSG["frequency"])
        if self.value is None:
            if not self.is_missing:
                raise ValueError(_ERROR_MSG["missing_flag"])
            object.__setattr__(self, "decimal_scale", None)
        else:
            if self.is_missing:
                raise ValueError(_ERROR_MSG["missing_flag"])
            if not isinstance(self.value, Decimal):
                raise ValueError(_ERROR_MSG["decimal_only"])
            if not self.value.is_finite():
                raise ValueError(_ERROR_MSG["value_not_finite"])
            object.__setattr__(self, "decimal_scale", decimal_scale_of(self.value))


@dataclass(frozen=True)
class MacroPageInfo:
    """分页信息（来自 Provider 元数据对象）。last_updated 可空。"""

    page: int
    pages: int
    per_page: int
    total: int
    last_updated: str | None = None


@dataclass(frozen=True)
class MacroFetchResult:
    """一次完整获取结果。

    - observations 按 normalized_period_start 升序稳定排序；
    - 包含 Provider 返回的缺失值记录（不插值）；
    - request_count 统计本次查询的全部请求（指标元数据 + 国家元数据 + 全部分页）；
    - provider_capabilities 稳定排序；
    - source_id 当前固定表示 World Development Indicators 数据源 "2"。
    """

    provider_key: str
    query: MacroQuery
    indicator: MacroIndicator
    geography: MacroGeography
    observations: tuple[MacroObservation, ...]
    page_info: MacroPageInfo
    fetched_at: datetime
    request_count: int
    acquisition_method: AcquisitionMethod
    authority_tier: SourceAuthorityTier
    critical_claim_eligible: bool
    provider_capabilities: tuple[SourceCapability, ...]
    source_id: str = _WDI_SOURCE_ID

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple):
            raise ValueError("observations 必须是 tuple")
        if not isinstance(self.request_count, int) or self.request_count < 0:
            raise ValueError(_ERROR_MSG["request_count"])
        if not isinstance(self.acquisition_method, AcquisitionMethod):
            raise ValueError("acquisition_method 必须是 AcquisitionMethod")
        if self.acquisition_method != AcquisitionMethod.OFFICIAL_API:
            raise ValueError(_ERROR_MSG["acquisition_method"])
        if not isinstance(self.authority_tier, SourceAuthorityTier):
            raise ValueError("authority_tier 必须是 SourceAuthorityTier")
        if self.source_id != _WDI_SOURCE_ID:
            raise ValueError(_ERROR_MSG["source_id"])
        if self.indicator.source_id != self.source_id:
            raise ValueError(_ERROR_MSG["indicator_source_consistency"])
        # 按 normalized_period_start 升序稳定排序（同一 period 的多条记录保持原相对顺序）。
        ordered = tuple(
            sorted(self.observations, key=lambda o: (o.normalized_period_start, o.period))
        )
        object.__setattr__(self, "observations", ordered)
        caps = tuple(sorted(self.provider_capabilities, key=lambda c: c.value))
        object.__setattr__(self, "provider_capabilities", caps)
