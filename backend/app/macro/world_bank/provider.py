"""World Bank macro provider (stage 2C.1).

Provider 快照语义：
- 第一次短 Session 只读取 Source Registry 中的 world_bank 配置并立即关闭；
- 网络 I/O 期间不持有 AsyncSession；
- MacroFetchResult 保存 authority_tier / critical_claim_eligible / provider_capabilities，
  不写数据库，这只是内存中的获取快照（持久化快照在阶段 2C.2 实现）。
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)
from app.macro.contracts import (
    MacroFetchResult,
    MacroObservation,
    MacroPageInfo,
    MacroQuery,
)
from app.macro.world_bank.client import (
    MAX_OBSERVATION_PAGES,
    PER_PAGE,
    WorldBankClient,
)
from app.macro.world_bank.errors import (
    WorldBankMalformedResponse,
    WorldBankProviderNotReady,
    WorldBankRequestLimitExceeded,
    WorldBankResponseConflict,
)
from app.macro.world_bank.parser import (
    parse_geography,
    parse_indicator,
    parse_observations,
)
from app.repositories.source_provider_repository import SourceProviderRepository

_CAPABILITIES_BY_VALUE = {cap.value: cap for cap in SourceCapability}


def _known_capabilities(values: list[str]) -> tuple[SourceCapability, ...]:
    return tuple(
        sorted(
            (cap for value in values if (cap := _CAPABILITIES_BY_VALUE.get(value)) is not None),
            key=lambda cap: cap.value,
        )
    )


def _dedupe(observations: list[MacroObservation]) -> list[MacroObservation]:
    """同一 period 出现多条：相同完整记录确定性去重；值或状态冲突报错。"""
    seen: dict[str, tuple[object, bool, str | None]] = {}
    result: list[MacroObservation] = []
    for obs in observations:
        key = (obs.value, obs.is_missing, obs.observation_status)
        previous = seen.get(obs.period)
        if previous is not None:
            if previous != key:
                raise WorldBankResponseConflict(f"conflicting observations for period {obs.period}")
            continue
        seen[obs.period] = key
        result.append(obs)
    return result


class WorldBankProvider:
    """World Bank Indicators API V2 Provider。

    通过 Source Registry 读取配置快照；一次 fetch 对应一次完整 MacroQuery。
    """

    provider_key = "world_bank"

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def _load_provider_snapshot(self) -> dict:
        # 短 Session：读取配置后立即关闭，网络 I/O 不持有 AsyncSession。
        async with self._sessionmaker() as session:
            provider = await SourceProviderRepository(session).get_by_key(self.provider_key)
        if provider is None:
            raise WorldBankProviderNotReady("provider not found in source registry")
        if not provider.enabled:
            raise WorldBankProviderNotReady("provider is disabled")
        capabilities = {cap for cap in provider.capabilities}
        if "macro_data" not in capabilities:
            raise WorldBankProviderNotReady("macro_data capability missing")
        methods = {method for method in provider.acquisition_methods}
        if "official_api" not in methods:
            raise WorldBankProviderNotReady("official_api acquisition method missing")
        if provider.requires_api_key:
            raise WorldBankProviderNotReady("provider requires api key")
        return {
            "authority_tier": SourceAuthorityTier(provider.authority_tier),
            "critical_claim_eligible": provider.critical_claim_eligible,
            "capabilities": _known_capabilities(provider.capabilities),
            "allowed_domains": list(provider.allowed_domains),
        }

    async def fetch(self, query: MacroQuery) -> MacroFetchResult:
        snapshot = await self._load_provider_snapshot()
        client = WorldBankClient(allowed_domains=snapshot["allowed_domains"])
        fetched_at = datetime.now(UTC)

        indicator_raw = await client.fetch_indicator_metadata(query.indicator_code)
        indicator = parse_indicator(
            indicator_raw,
            indicator_code=query.indicator_code,
            provider_key=self.provider_key,
        )

        country_raw = await client.fetch_country_metadata(query.country_code)
        geography = parse_geography(country_raw, requested_code=query.country_code)

        observations, page_info = await self._fetch_all_observations(client, query, geography)

        return MacroFetchResult(
            provider_key=self.provider_key,
            query=query,
            indicator=indicator,
            geography=geography,
            observations=tuple(observations),
            page_info=page_info,
            fetched_at=fetched_at,
            request_count=client.request_count,
            acquisition_method=AcquisitionMethod.OFFICIAL_API,
            authority_tier=snapshot["authority_tier"],
            critical_claim_eligible=snapshot["critical_claim_eligible"],
            provider_capabilities=snapshot["capabilities"],
        )

    async def _fetch_all_observations(
        self,
        client: WorldBankClient,
        query: MacroQuery,
        geography,
    ) -> tuple[list[MacroObservation], MacroPageInfo]:
        """从 page=1 开始分页；合并全部观测；保留首页 page_info（含 Provider total）。"""
        page = 1
        merged: list[MacroObservation] = []
        first_page_info: MacroPageInfo | None = None
        first_pages: int | None = None
        while True:
            raw = await client.fetch_observations(query, page=page, per_page=PER_PAGE)
            page_info, rows = parse_observations(
                raw,
                query=query,
                geography=geography,
                provider_key=self.provider_key,
            )
            # 请求预算：2（indicator + country 元数据）+ N（观测分页）≤ 20，N ≤ 18；
            # 第一页 pages 超上限立即拒绝，不继续请求下一页。
            if page_info.pages > MAX_OBSERVATION_PAGES:
                raise WorldBankRequestLimitExceeded(
                    f"pages {page_info.pages} exceeds {MAX_OBSERVATION_PAGES}"
                )
            if page_info.page != page:
                raise WorldBankMalformedResponse("response page does not match requested page")
            if first_pages is None:
                first_pages = page_info.pages
            elif page_info.pages > first_pages:
                raise WorldBankMalformedResponse("pages grew across requests")
            if page_info.pages < page:
                raise WorldBankMalformedResponse("pages shrank below current page")
            if first_page_info is None:
                first_page_info = page_info
            merged.extend(rows)
            if page >= page_info.pages:
                break
            page += 1
        if first_page_info is None:
            raise WorldBankMalformedResponse("no page info received")
        return _dedupe(merged), first_page_info
