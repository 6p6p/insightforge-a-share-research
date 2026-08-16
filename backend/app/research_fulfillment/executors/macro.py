"""Macro need executor (stage 7A.2A spec N): macro Evidence replay。

对一条 missing macro need（或 macro_dataset document need）自动补证据：
1. 从既有 MacroObservation / MacroDatasetSnapshot / MacroSeries 找出**可用**
   观测——`snapshot.fetched_at <= analysis_as_of`（no-lookahead，镜像
   preparation 的 `_macro_available`，**不 live World Bank fetch**）；
2. 按 need 的 topic_or_indicator / geography 与 snapshot.indicator_name /
   geography_name / iso3_code 做**确定性匹配**；
3. 每个匹配观测 → `MacroEvidenceService.create_macro_card`（company-context
   macro-origin Evidence，fingerprint replay → 幂等，spec Q）；
4. 无匹配 → MACRO_DATA_UNAVAILABLE（unresolved）；provider_keys 空 →
   PROVIDER_UNAVAILABLE（不 fetch）。

硬边界（spec N/M）：**0 LLM / 0 Retrieval / 0 Chroma / 0 Web**；只消费已
抓取的宏观数据。executor 不抛确定性错误。
"""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.claims.macro_policy import resolve_availability
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.evidence.contracts import EvidenceOrigin, MacroEvidenceDraft
from app.research_fulfillment.contracts import (
    FulfillmentAttempt,
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.service import FulfillmentContext
from app.research_planning.preparation import MissingResearchNeed
from app.research_planning.router import SourceRouteEntry
from app.services.macro_auto_fetch_service import MacroAutoFetchResult
from app.services.macro_evidence_service import MacroEvidenceService
from app.services.source_discovery.contracts import SourceDiscoveryRequest
from app.services.source_discovery.service import SourceDiscoveryService

# 单条 need 最多 replay 的宏观卡数（确定性强、足够驱动 macro analyst）。
_MAX_MACRO_CARDS = 5
# macro fulfillment 的 extractor 身份（**0 LLM**：extractor_model_id=None）。
MACRO_FULFILLMENT_EXTRACTOR_NAME = "macro_fulfillment"
MACRO_FULFILLMENT_EXTRACTOR_VERSION = 1


def _macro_available(snapshot: MacroDatasetSnapshotModel, analysis_as_of: date) -> bool:
    """no-lookahead：只有基准日之前已取得的观测才算可用（镜像 preparation）。"""
    availability = resolve_availability(
        origin_type=EvidenceOrigin.MACRO_OBSERVATION.value,
        snapshot_fetched_at=snapshot.fetched_at,
        source_published_at=None,
        source_acquired_at=None,
    )
    return availability is not None and availability.date() <= analysis_as_of


class MacroNeedExecutor:
    """macro need 自动补证据：可用 MacroObservation → create_macro_card → 重跑。

    V1.1 closure：`auto_fetch`（MacroAutoFetchService）注入后，无可用观测时
    先尝试**有界自动获取**（确定性 topic→World Bank indicator 映射 → 真实
    获取落库）→ 重查；获取失败保持 MACRO_DATA_UNAVAILABLE（human fallback
    兜底，绝不编造宏观数字）。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        macro_service: MacroEvidenceService | None = None,
        auto_fetch=None,
        discovery: SourceDiscoveryService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._macro_service = macro_service or MacroEvidenceService(sessionmaker)
        # P1：统一 Source Discovery Layer（优先）；legacy auto_fetch 回退。
        self._auto_fetch = auto_fetch
        self._discovery = discovery

    # ------------------------------------------------------------ 主入口

    async def fulfill(
        self,
        *,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
    ) -> FulfillmentAttempt:
        if entry is None or not entry.provider_keys:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.PROVIDER_UNAVAILABLE,
                error_code=FulfillmentErrorCode.PROVIDER_UNAVAILABLE,
            )
        topic, geo = self._match_terms(context, need)
        rows = await self._load_available_observations(context, need, topic, geo)
        if not rows and (self._discovery is not None or self._auto_fetch is not None):
            # P1 统一发现层优先（MacroDiscoveryProvider 包装有界自动获取）；
            # legacy auto_fetch 回退。失败保持 MACRO_DATA_UNAVAILABLE 原语义。
            try:
                if self._discovery is not None:
                    outcome = await self._discovery.discover(
                        SourceDiscoveryRequest(
                            company_id=context.company_id,
                            security_code="",
                            need_kind="macro",
                            as_of=context.analysis_as_of,
                            topic=topic,
                            geo=geo,
                        )
                    )
                    result = (
                        MacroAutoFetchResult(fetched=True, persisted=True)
                        if outcome.acquired
                        else None
                    )
                else:
                    result = await self._auto_fetch.fetch_for_need(
                        topic=topic, geo=geo, as_of=context.analysis_as_of
                    )
            except Exception:  # noqa: BLE001 - 获取失败 → 保持 MACRO_DATA_UNAVAILABLE
                result = None
            if result is not None and result.persisted:
                rows = await self._load_available_observations(context, need, topic, geo)
        if not rows:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.MACRO_DATA_UNAVAILABLE,
            )
        created: list[UUID] = []
        existing: list[UUID] = []
        for obs, snapshot, series in rows[:_MAX_MACRO_CARDS]:
            draft = MacroEvidenceDraft(
                company_id=context.company_id,
                research_question=context.research_question,
                macro_observation_id=obs.observation_id,
                evidence_statement=self._statement(obs, snapshot, series),
                extractor_name=MACRO_FULFILLMENT_EXTRACTOR_NAME,
                extractor_version=MACRO_FULFILLMENT_EXTRACTOR_VERSION,
            )
            result = await self._macro_service.create_macro_card(draft)
            (existing if result.replayed else created).append(result.evidence_card_id)
        if not created and not existing:
            # 理论上不会发生（rows 非空即应创建成功），防御性兜底。
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.MACRO_EVIDENCE_MISSING,
            )
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value,
            status=FulfillmentStatus.RESOLVED,
            created_artifact_ids=created,
            existing_artifact_ids=existing,
        )

    # ------------------------------------------------------------ 匹配

    @staticmethod
    def _match_terms(
        context: FulfillmentContext, need: MissingResearchNeed
    ) -> tuple[str | None, str | None]:
        """need → (topic, geography)。

        - macro need：用 MacroNeed.topic_or_indicator / geography 过滤；
        - macro_dataset document need：无 topic 语义 → 任意可用观测（宽松）。
        """
        if need.need_kind == "macro":
            macro_need = next(
                (item for item in context.payload.macro_needs if item.need_code == need.need_code),
                None,
            )
            if macro_need is not None:
                return macro_need.topic_or_indicator, macro_need.geography
        return None, None

    @staticmethod
    def _observation_matches(
        *,
        topic: str | None,
        geo: str | None,
        snapshot: MacroDatasetSnapshotModel,
        series: MacroSeriesModel,
    ) -> bool:
        """确定性匹配：indicator 与 geography 均为宽松子串/相等（不误判宽松）。"""
        if topic:
            indicator = (snapshot.indicator_name or "").lower()
            term = topic.strip().lower()
            if not term or (term not in indicator and indicator not in term):
                return False
        if geo:
            term = geo.strip().lower()
            if not term:
                return True
            geography_name = (snapshot.geography_name or "").lower()
            iso3 = (snapshot.iso3_code or "").lower()
            geography_code = (series.geography_code or "").lower()
            if not (
                term in geography_name
                or geography_name in term
                or term == iso3
                or term == geography_code
            ):
                return False
        return True

    @staticmethod
    def _statement(
        obs: MacroObservationModel, snapshot: MacroDatasetSnapshotModel, series: MacroSeriesModel
    ) -> str:
        """company-context 的宏观证据陈述（确定性拼装，不解释数值）。"""
        value = "缺失" if obs.is_missing else str(obs.value_numeric)
        geography = (
            snapshot.geography_name or snapshot.iso3_code or series.geography_code or "未知地区"
        )
        return f"{snapshot.indicator_name}（{geography}）在 {obs.period} 的观测值为 {value}"

    # ------------------------------------------------------------ 数据

    async def _load_available_observations(
        self,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        topic: str | None,
        geo: str | None,
    ) -> list[tuple[MacroObservationModel, MacroDatasetSnapshotModel, MacroSeriesModel]]:
        stmt = (
            select(MacroObservationModel, MacroDatasetSnapshotModel, MacroSeriesModel)
            .join(
                MacroDatasetSnapshotModel,
                MacroDatasetSnapshotModel.snapshot_id == MacroObservationModel.snapshot_id,
            )
            .join(
                MacroSeriesModel,
                MacroSeriesModel.series_id == MacroDatasetSnapshotModel.series_id,
            )
            .order_by(MacroObservationModel.normalized_period_start.desc())
        )
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            rows = result.all()
        rows = [
            (obs, snapshot, series)
            for obs, snapshot, series in rows
            if _macro_available(snapshot, context.analysis_as_of)
            and self._observation_matches(topic=topic, geo=geo, snapshot=snapshot, series=series)
        ]
        return rows

    # ------------------------------------------------------------ attempt

    @staticmethod
    def _attempt(
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
        status: FulfillmentStatus,
        *,
        error_code: FulfillmentErrorCode | None = None,
    ) -> FulfillmentAttempt:
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value if entry is not None else "",
            status=status,
            error_code=error_code,
        )
