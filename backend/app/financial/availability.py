"""Financial observation temporal eligibility (P0 research isolation).

任何进入当前研究任务上下文的 FinancialObservation，其 source evidence 的
availability（SourceRecord.published_at，否则 acquired_at）必须 <= 任务的
analysis_as_of（no-lookahead：2025 年年报 2026-03 才公开，对 as_of 更早的
历史任务属于未来信息，禁止使用）。

- `resolve_observation_availability`：批量把 observation → EvidenceCard →
  SourceRecord 的 availability 解析出来（复用 claims.macro_policy.resolve_availability
  的同一规则，保证与 synthesis no-lookahead guard 一致）；
- `filter_observations_eligible`：按 as_of 过滤；availability 无法解析
  （provenance 缺失）→ 保守排除（任务上下文宁可缺，不引入无法证明时点的观测，
  与 guard 的 TemporalEvidenceInsufficient 语义一致）。
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.claims.macro_policy import resolve_availability
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.source_record import SourceRecordModel

_OBSERVATION_ORIGINS = ("financial_extraction", "document_chunk")


async def resolve_observation_availability(
    session: AsyncSession,
    observations: list[FinancialMetricObservationModel],
) -> dict[UUID, datetime | None]:
    """批量解析 observation → availability（published_at else acquired_at）。

    返回 {metric_observation_id: availability | None}；provenance 缺失（卡或
    source 缺失）→ None（调用方按保守策略处理）。
    """
    if not observations:
        return {}
    card_ids = {obs.source_evidence_card_id for obs in observations if obs.source_evidence_card_id}
    if not card_ids:
        return {obs.metric_observation_id: None for obs in observations}
    cards = {
        card.evidence_card_id: card
        for card in (
            await session.execute(
                select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
            )
        )
        .scalars()
        .all()
    }
    source_ids = {card.source_id for card in cards.values() if card.source_id is not None}
    sources = {
        src.source_id: src
        for src in (
            await session.execute(
                select(SourceRecordModel).where(SourceRecordModel.source_id.in_(source_ids))
            )
        )
        .scalars()
        .all()
    }
    result: dict[UUID, datetime | None] = {}
    for obs in observations:
        card = cards.get(obs.source_evidence_card_id)
        if card is None or card.origin_type not in _OBSERVATION_ORIGINS:
            result[obs.metric_observation_id] = None
            continue
        source = sources.get(card.source_id)
        result[obs.metric_observation_id] = resolve_availability(
            origin_type=card.origin_type,
            snapshot_fetched_at=None,
            source_published_at=source.published_at if source else None,
            source_acquired_at=source.acquired_at if source else None,
        )
    return result


async def filter_observations_eligible(
    session: AsyncSession,
    observations: list[FinancialMetricObservationModel],
    analysis_as_of: date | None,
) -> list[FinancialMetricObservationModel]:
    """按 analysis_as_of 过滤 observation（company 过滤由调用方负责）。

    - analysis_as_of 为 None → 原样返回（调用方未声明基准日时不做时态裁剪，
      与既有行为一致）；
    - 否则仅保留 availability 可解析且 availability.date() <= analysis_as_of 的
      observation；availability 无法解析 → 排除（保守）。
    """
    if analysis_as_of is None or not observations:
        return list(observations)
    availability = await resolve_observation_availability(session, observations)
    eligible: list[FinancialMetricObservationModel] = []
    for obs in observations:
        avail = availability.get(obs.metric_observation_id)
        if avail is not None and avail.date() <= analysis_as_of:
            eligible.append(obs)
    return eligible


async def filter_observations_for_task(
    session: AsyncSession,
    observations: list[FinancialMetricObservationModel],
    analysis_as_of: date | None,
    research_question_sha256: str | None,
) -> list[FinancialMetricObservationModel]:
    """任务级观测隔离（P0 closure）：观测须同时满足

    - availability（source published/acquired）<= analysis_as_of（no-lookahead）；
    - source evidence card 的 research_question_sha256 == 当前任务 question
      （task-level user supplement / 跨任务观测隔离：其他任务人工转录的
      user_supplied 观测、或其他研究问题下抽取的观测，一律不得进入本任务
      上下文）。

    research_question_sha256 为 None 时退化为仅时态过滤（兼容旧调用方）。
    """
    eligible = await filter_observations_eligible(session, observations, analysis_as_of)
    if research_question_sha256 is None or not eligible:
        return eligible
    card_ids = {obs.source_evidence_card_id for obs in eligible}
    cards = {
        card.evidence_card_id: card
        for card in (
            await session.execute(
                select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
            )
        )
        .scalars()
        .all()
    }
    result: list[FinancialMetricObservationModel] = []
    for obs in eligible:
        card = cards.get(obs.source_evidence_card_id)
        if card is not None and card.research_question_sha256 == research_question_sha256:
            result.append(obs)
    return result
