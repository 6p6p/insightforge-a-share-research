"""Response parsing for the World Bank Indicators API V2.

解析规则（信任但验证）：
- 顶层必须是长度为 2 的 list；第二项允许 list / null / 空 list；
- page/pages/per_page/total 支持整数或数字字符串；
- value 允许 JSON number（parse_float=Decimal）/ 数字字符串 / null；
  NaN / Infinity / 空字符串 / 非数字字符串拒绝；禁止 float 中间转换；
- date 必须为四位年份且落在请求范围内；indicator/country 必须与请求一致；
- 输出是原始观测行，跨页去重/冲突由 Provider 合并层处理。
"""

import re
from datetime import date
from decimal import Decimal

from app.macro.contracts import (
    MacroFrequency,
    MacroGeography,
    MacroGeographyType,
    MacroIndicator,
    MacroObservation,
    MacroPageInfo,
    MacroQuery,
    MacroTopic,
)
from app.macro.world_bank.client import SOURCE_ID
from app.macro.world_bank.errors import (
    WorldBankApiError,
    WorldBankMalformedResponse,
)

_YEAR_RE = re.compile(r"^\d{4}$")


def _coerce_int(value: object) -> int:
    """page/pages/per_page/total：整数或数字字符串；其余拒绝。"""
    if isinstance(value, bool):
        raise WorldBankMalformedResponse("bool is not a valid page field")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip() and value.strip().lstrip("-").isdigit():
        return int(value)
    raise WorldBankMalformedResponse("invalid page field")


def _looks_like_api_error(metadata: dict) -> bool:
    return "message" in metadata or "success" in metadata


def split_response(raw: object) -> tuple[dict, list | None]:
    """验证顶层结构并返回 (metadata, rows)。rows 可为 None（无数据）。"""
    if not isinstance(raw, list) or len(raw) != 2:
        raise WorldBankMalformedResponse("top-level must be a 2-element list")
    metadata = raw[0]
    if not isinstance(metadata, dict):
        raise WorldBankMalformedResponse("metadata must be an object")
    if _looks_like_api_error(metadata):
        raise WorldBankApiError("api returned an error object")
    rows = raw[1]
    if rows is None:
        return metadata, None
    if not isinstance(rows, list):
        raise WorldBankMalformedResponse("rows must be a list or null")
    return metadata, rows


def parse_page_info(metadata: dict) -> MacroPageInfo:
    page = _coerce_int(metadata.get("page"))
    pages = _coerce_int(metadata.get("pages"))
    per_page = _coerce_int(metadata.get("per_page"))
    total = _coerce_int(metadata.get("total"))
    if page < 1:
        raise WorldBankMalformedResponse("page must be >= 1")
    if pages < 1:
        raise WorldBankMalformedResponse("pages must be >= 1")
    if page > pages:
        raise WorldBankMalformedResponse("page must not exceed pages")
    if total < 0:
        raise WorldBankMalformedResponse("total must not be negative")
    last_updated = metadata.get("lastupdated")
    last_updated = last_updated if isinstance(last_updated, str) else None
    return MacroPageInfo(
        page=page,
        pages=pages,
        per_page=per_page,
        total=total,
        last_updated=last_updated,
    )


def parse_indicator(
    raw: object,
    *,
    indicator_code: str,
    provider_key: str,
) -> MacroIndicator:
    metadata, rows = split_response(raw)
    parse_page_info(metadata)  # 顺带验证 metadata 完整性
    if not rows:
        raise WorldBankMalformedResponse("indicator metadata rows missing")
    row = rows[0]
    if not isinstance(row, dict):
        raise WorldBankMalformedResponse("indicator row must be an object")
    if row.get("id") != indicator_code:
        raise WorldBankMalformedResponse("indicator id mismatch")
    source = row.get("source")
    if not isinstance(source, dict) or source.get("id") != SOURCE_ID:
        raise WorldBankMalformedResponse("indicator source mismatch")
    topics: list[MacroTopic] = []
    for topic in row.get("topics") or []:
        if isinstance(topic, dict):
            topics.append(
                MacroTopic(
                    topic_id=str(topic.get("id") or ""),
                    name=str(topic.get("value") or ""),
                )
            )
    return MacroIndicator(
        provider_key=provider_key,
        external_indicator_id=indicator_code,
        name=str(row.get("name") or ""),
        unit=str(row.get("unit") or ""),
        source_id=str(source.get("id") or ""),
        source_name=str(source.get("value") or ""),
        source_note=str(row.get("sourceNote") or ""),
        source_organization=str(row.get("sourceOrganization") or ""),
        topics=tuple(topics),
    )


def parse_geography(raw: object, *, requested_code: str) -> MacroGeography:
    metadata, rows = split_response(raw)
    parse_page_info(metadata)
    if not rows:
        raise WorldBankMalformedResponse("country metadata rows missing")
    row = rows[0]
    if not isinstance(row, dict):
        raise WorldBankMalformedResponse("country row must be an object")
    country_id = row.get("id")
    if not isinstance(country_id, str) or not country_id:
        raise WorldBankMalformedResponse("country id missing")
    region = row.get("region")
    income = row.get("incomeLevel")

    def _name(value: object) -> str | None:
        if isinstance(value, dict):
            text = value.get("value")
            if isinstance(text, str) and text:
                return text
        return None

    return MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code=requested_code,
        provider_country_id=country_id,
        iso2_code=str(row.get("iso2Code") or ""),
        # World Bank 对真实国家，country id 即 ISO3（请求代码已排除聚合区域）。
        iso3_code=country_id,
        name=str(row.get("name") or ""),
        region_name=_name(region),
        income_level_name=_name(income),
    )


def _parse_value(value: object) -> tuple[Decimal | None, bool]:
    """value 解析：None → 缺失；int/Decimal → 直接 Decimal；字符串 → Decimal(s)。

    禁止 float 中间转换；NaN / Infinity / 空字符串 / 非数字字符串拒绝。
    """
    if value is None:
        return None, True
    if isinstance(value, bool):
        raise WorldBankMalformedResponse("boolean value not allowed")
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, float):
        raise WorldBankMalformedResponse("float value not allowed")
    elif isinstance(value, str):
        if not value.strip():
            raise WorldBankMalformedResponse("empty string value not allowed")
        try:
            decimal_value = Decimal(value)
        except ArithmeticError:
            raise WorldBankMalformedResponse("non-numeric string value") from None
    else:
        raise WorldBankMalformedResponse("unsupported value type")
    if not decimal_value.is_finite():
        raise WorldBankMalformedResponse("non-finite value")
    return decimal_value, False


def parse_observations(
    raw: object,
    *,
    query: MacroQuery,
    geography: MacroGeography,
    provider_key: str,
) -> tuple[MacroPageInfo, list[MacroObservation]]:
    metadata, rows = split_response(raw)
    page_info = parse_page_info(metadata)
    observations: list[MacroObservation] = []
    if not rows:
        return page_info, observations
    for row in rows:
        if not isinstance(row, dict):
            raise WorldBankMalformedResponse("observation row must be an object")
        observations.append(_parse_observation(row, query, geography, provider_key))
    return page_info, observations


def _parse_observation(
    row: dict,
    query: MacroQuery,
    geography: MacroGeography,
    provider_key: str,
) -> MacroObservation:
    indicator = row.get("indicator")
    if not isinstance(indicator, dict) or indicator.get("id") != query.indicator_code:
        raise WorldBankMalformedResponse("indicator id mismatch")
    country = row.get("country")
    country_id = country.get("id") if isinstance(country, dict) else None
    iso3 = row.get("countryiso3code")
    if country_id != geography.provider_country_id or iso3 != geography.iso3_code:
        raise WorldBankMalformedResponse("country mismatch")
    date_str = row.get("date")
    if not isinstance(date_str, str) or not _YEAR_RE.match(date_str):
        raise WorldBankMalformedResponse("date must be a 4-digit year")
    year = int(date_str)
    if not (query.start_year <= year <= query.end_year):
        raise WorldBankMalformedResponse("date out of requested range")
    value, is_missing = _parse_value(row.get("value"))
    obs_status = row.get("obs_status")
    obs_status = obs_status if isinstance(obs_status, str) and obs_status else None
    return MacroObservation(
        provider_key=provider_key,
        external_indicator_id=query.indicator_code,
        geography_code=geography.iso3_code,
        period=date_str,
        period_start=date(year, 1, 1),
        frequency=MacroFrequency.ANNUAL,
        value=value,
        is_missing=is_missing,
        observation_status=obs_status,
    )
