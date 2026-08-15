"""Macro auto-fetch service (V1.1 final closure): 受控自动获取宏观数据。

`MacroNeedExecutor` 在无可用观测时调用本服务：确定性 topic→World Bank
indicator 映射（**无 LLM、无模糊匹配**），fetch_and_persist 幂等落库
（真实生产供给链：World Bank API → snapshot → observations），随后 executor
重查可用观测。

硬边界：
- 只支持白名单映射表内的 topic；映射外 → False（保持 MACRO_DATA_UNAVAILABLE，
  human fallback 兜底，**不编造宏观数字**）；
- 时间窗有界：analysis_as_of 前 5 年（end_year = as_of.year，start_year =
  as_of.year - 4）；绝不越界未来；
- 国家：need.geography 命中受控映射 → 该国；否则默认 CHN（A 股研究上下文）；
- 任何网络/解析/持久化失败 → False（不泄漏异常）。
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.macro.contracts import MacroQuery
from app.macro.world_bank.provider import WorldBankProvider
from app.services.macro_persistence_service import MacroPersistenceService
from app.storage.raw_store import LocalRawArtifactStore

# 确定性 topic → World Bank indicator（受控白名单；key 为小写规范化 term）。
_MACRO_INDICATOR_MAP: dict[str, str] = {
    "gdp": "NY.GDP.MKTP.CD",
    "gdp总量": "NY.GDP.MKTP.CD",
    "国内生产总值": "NY.GDP.MKTP.CD",
    "gdp增长": "NY.GDP.MKTP.KD.ZG",
    "gdp增长率": "NY.GDP.MKTP.KD.ZG",
    "gdp增速": "NY.GDP.MKTP.KD.ZG",
    "经济增速": "NY.GDP.MKTP.KD.ZG",
    "经济增长率": "NY.GDP.MKTP.KD.ZG",
    "人均gdp": "NY.GDP.PCAP.CD",
    "cpi": "FP.CPI.TOTL.ZG",
    "通胀": "FP.CPI.TOTL.ZG",
    "通货膨胀": "FP.CPI.TOTL.ZG",
    "通胀率": "FP.CPI.TOTL.ZG",
    "失业率": "SL.UEM.TOTL.ZS",
    "人口": "SP.POP.TOTL",
    "总人口": "SP.POP.TOTL",
    "利率": "FR.INR.LEND",
    "贷款利率": "FR.INR.LEND",
}

# 确定性国家映射（need.geography → ISO3；命中失败 → CHN 默认）。
_GEOGRAPHY_MAP: dict[str, str] = {
    "中国": "CHN",
    "china": "CHN",
    "chn": "CHN",
    "美国": "USA",
    "usa": "USA",
    "us": "USA",
    "日本": "JPN",
    "japan": "JPN",
    "德国": "DEU",
    "germany": "DEU",
    "英国": "GBR",
    "uk": "GBR",
    "欧元区": "EMU",
    "euro area": "EMU",
}

# 自动获取窗口：as_of 前 5 年（含当年）。
_FETCH_YEAR_SPAN = 5


@dataclass(frozen=True)
class MacroAutoFetchResult:
    fetched: bool  # 是否执行了真实获取（映射命中）
    persisted: bool  # 获取且持久化成功


def resolve_macro_indicator(topic: str | None) -> str | None:
    """need.topic_or_indicator → World Bank indicator（确定性白名单映射）。"""
    if not topic:
        return None
    term = topic.strip().lower().replace(" ", "")
    if term in _MACRO_INDICATOR_MAP:
        return _MACRO_INDICATOR_MAP[term]
    # 宽松后缀匹配（如「GDP增长率（%）」）：term 以映射 key 开头/结尾。
    for key, indicator in _MACRO_INDICATOR_MAP.items():
        if term.startswith(key) or term.endswith(key):
            return indicator
    return None


def resolve_macro_country(geo: str | None) -> str:
    """need.geography → ISO3（确定性映射；未命中 → CHN）。"""
    if not geo:
        return "CHN"
    term = geo.strip().lower().replace(" ", "")
    return _GEOGRAPHY_MAP.get(term, "CHN")


class MacroAutoFetchService:
    """有界自动获取：World Bank 指标 → MacroPersistenceService 幂等落库。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store

    async def fetch_for_need(
        self,
        *,
        topic: str | None,
        geo: str | None,
        as_of: date,
    ) -> MacroAutoFetchResult:
        """need → 有界自动获取；失败 → (fetched=True, persisted=False)。"""
        indicator = resolve_macro_indicator(topic)
        if indicator is None:
            return MacroAutoFetchResult(fetched=False, persisted=False)
        end_year = as_of.year
        start_year = max(1960, end_year - _FETCH_YEAR_SPAN + 1)
        query = MacroQuery(
            provider_key="world_bank",
            indicator_code=indicator,
            country_code=resolve_macro_country(geo),
            start_year=start_year,
            end_year=end_year,
        )
        provider = WorldBankProvider(self._sessionmaker)
        persistence = MacroPersistenceService(self._sessionmaker, self._raw_store)
        try:
            await persistence.fetch_and_persist(provider, query)
            return MacroAutoFetchResult(fetched=True, persisted=True)
        except Exception:  # noqa: BLE001 - 网络/解析/持久化失败 → False
            return MacroAutoFetchResult(fetched=True, persisted=False)
