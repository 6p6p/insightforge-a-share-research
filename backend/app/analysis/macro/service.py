"""Structured macro context analysis service (stage 4C.1B): Macro Evidence Pack → LLM → Claim。

流程（10 步，镜像 FinancialAnalysisService）：
1. 防御性 request 校验（构造已校验，服务层再兜底）；
2. 短 DB session：加载全部 Macro Evidence（macro_driver 池）+ Company Evidence
   （company 池）并**逐条校验**（任一缺失 → EvidenceNotFound、跨公司 →
   CompanyMismatch、provenance 链缺失 → Corrupted；macro_driver 池逐条满足 v3
   资格——macro_observation 或 news_article + evidence_type ∈ {event, fact,
   statement}，违反 → OriginViolation；company 池每条 origin_type=document_chunk，
   违反 → OriginViolation；全部 availability 解析（缺失 → TemporalInsufficient），
   任何 future（availability > analysis_as_of）→ FutureEvidence）；
3. 关闭 DB session（**LLM 调用期间不持有 DB transaction / connection**）；
4. 构造 M/E alias（MacroDriver Pack + Company Evidence Pack，两池 namespace 严格分离）；
5. 调 MacroAnalysisModel.analyze → MacroAnalysisDecision（provider 失败 →
   ModelUnavailable；输出无法解析 → MalformedOutput）；
6. 防御性 double-check（模型可能返回 raw dict，再做一次 schema 校验）；
7. relevant=false → 0-claims 结果（不写任何 Claim）；
8. macro numeric-literal guard v1（任一 Claim statement 含数字/百分比/中文定量
   表达 → MacroAnalysisNumericLiteralForbidden，整次失败 0 写；**不自动删数字 /
   不改写 / 不让第二个 LLM 修正**）；
9. M/E ref resolution（未知 M/E → UnknownRef；跨 relation → RelationConflict；
   全部 candidate 先完成，任一失败 → 整次 0 写）+ overclaim policy 防线；
10. 构造全部 MacroClaimDraft（v6；固定 analyst_name=MACRO_ANALYST_NAME、
    analyst_version=1、analyst_model_id=model.model_id、analysis_as_of=
    request.analysis_as_of）+ claim_kind policy；
11. MacroClaimService.create_claim_batch（1..3 drafts，单 transaction）→
    MacroAnalysisResult（relevant / claim_ids ordered / created_count /
    replayed_count / reason_code）。

**不创建 Report / DraftSection / ReviewIssue / Audit**；不接 LangGraph 分析节点；
不调用 Retrieval / Chroma / RawArtifact / tools / web search。Macro Analyst 不做
任何宏观定量计算、不编造数字、不做估值。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.macro.contracts import (
    _ALLOWED_KINDS_MACRO_ANALYST,
    MACRO_ANALYST_FOCUS,
    MACRO_ANALYST_NAME,
    MACRO_ANALYST_VERSION,
    MacroAnalysisContext,
    MacroAnalysisDecision,
    MacroAnalysisRequest,
    MacroAnalysisResult,
)
from app.analysis.macro.errors import (
    MacroAnalysisClaimKindPolicy,
    MacroAnalysisEvidenceCompanyMismatch,
    MacroAnalysisEvidenceCorrupted,
    MacroAnalysisEvidenceNotFound,
    MacroAnalysisFutureEvidence,
    MacroAnalysisInputError,
    MacroAnalysisMalformedOutput,
    MacroAnalysisOriginViolation,
    MacroAnalysisOverclaimPolicy,
    MacroAnalysisTemporalEvidenceInsufficient,
)
from app.analysis.macro.model import MacroAnalysisModel
from app.analysis.macro.packs import (
    CompanyEvidencePackSource,
    MacroDriverPackSource,
    ResolvedMacroClaim,
    assert_macro_statement_has_no_numeric_literals,
    build_company_evidence_pack,
    build_macro_driver_pack,
    resolve_decision_refs,
)
from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MacroClaimDraft,
    MacroClaimImportance,
    MacroImpactStatus,
    MacroTimeAlignment,
)
from app.claims.macro_policy import driver_evidence_eligible, resolve_availability
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import EvidenceOrigin
from app.services.macro_claim_service import MacroClaimService


@dataclass(frozen=True)
class _LoadedMacroAnalysisSources:
    """短 DB session 的加载产物（session 关闭后不再持有连接）。"""

    macro_driver_sources: list[MacroDriverPackSource]
    company_evidence_sources: list[CompanyEvidencePackSource]


class MacroAnalysisService:
    def __init__(self, sessionmaker: async_sessionmaker, model: MacroAnalysisModel) -> None:
        self._sessionmaker = sessionmaker
        self._model = model

    async def analyze(self, request: MacroAnalysisRequest) -> MacroAnalysisResult:
        # 1. 防御性 request 校验（构造已校验，服务层再兜底）。
        self._check_request(request)

        # 2. 短 DB session：加载并校验全部 Macro Evidence + Company Evidence
        #    （任一 missing / company mismatch / corrupted / origin 违反 /
        #    future evidence → 稳定错误，**不调用 LLM**）。
        loaded = await self._load_sources(request)

        # 3. DB session 已关闭；构造 M/E alias（两池 namespace 严格分离）。
        driver_pack = build_macro_driver_pack(loaded.macro_driver_sources)
        company_pack = build_company_evidence_pack(loaded.company_evidence_sources)

        # 4-5. 调模型（结构化决策；LLM 调用期间不持有 DB transaction）。
        context = MacroAnalysisContext(
            research_question=request.research_question,
            analysis_as_of=request.analysis_as_of,
            strategy=MACRO_ANALYST_FOCUS,
        )
        decision = await self._call_model(context, driver_pack, company_pack)

        # 6. relevant=false → 0-claims 结果（不写任何 Claim）。
        if not decision.relevant:
            return MacroAnalysisResult(
                relevant=False,
                claim_ids=[],
                created_count=0,
                replayed_count=0,
                reason_code=decision.reason_code,
            )

        # 7. macro numeric-literal guard v1（任一 Claim 含数字/百分比/中文定量表达
        #    → 整次失败 0 写；不自动删数字 / 不改写 / 不让第二个 LLM 修正）。
        for candidate in decision.claims:
            assert_macro_statement_has_no_numeric_literals(candidate.statement)

        # 8. M/E ref resolution（全部 candidate 先完成，任一失败 → 整次 0 写）。
        resolved = resolve_decision_refs(decision, driver_pack, company_pack)

        # 9. 构造全部 MacroClaimDraft(v6) + overclaim/kind policy 防线。
        drafts = self._build_drafts(request, resolved)
        self._check_overclaim_policy(resolved)
        self._check_kind_policy(drafts)

        # 10. 原子持久化（create_claim_batch：全部 draft 先 validate，单 transaction）。
        batch = await MacroClaimService(self._sessionmaker).create_claim_batch(drafts)
        return MacroAnalysisResult(
            relevant=True,
            claim_ids=list(batch.claim_ids),
            created_count=len(batch.created),
            replayed_count=len(batch.replayed),
            reason_code=None,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: MacroAnalysisRequest) -> None:
        # 构造时已做校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if (
            not request.research_question.strip()
            or not request.macro_driver_evidence_ids
            or not request.company_evidence_ids
        ):
            raise MacroAnalysisInputError("invalid macro analysis request")

    async def _load_sources(self, request: MacroAnalysisRequest) -> _LoadedMacroAnalysisSources:
        """短 DB session 加载并校验全部 Evidence（不调用 LLM 时先验证上游）。"""
        async with self._sessionmaker() as session:
            cards = await self._load_evidence_cards(session, request)
            snapshots = await self._load_snapshots(session, cards)
            observations = await self._load_observations(session, cards)
            series = await self._load_series(session, cards)
            source_records = await self._load_source_records(session, cards)
            driver_sources = self._build_driver_sources(
                request, cards, observations, snapshots, series, source_records
            )
            company_sources = self._build_company_sources(request, cards, source_records)
        return _LoadedMacroAnalysisSources(
            macro_driver_sources=driver_sources,
            company_evidence_sources=company_sources,
        )

    @staticmethod
    async def _load_evidence_cards(
        session,
        request: MacroAnalysisRequest,
    ) -> dict[UUID, EvidenceCardModel]:
        """从真实 PG 加载全部 Evidence（两池去重后）；缺失 → EvidenceNotFound。

        公司一致性统一在此校验（跨公司 → CompanyMismatch）。
        """
        all_ids = set(request.macro_driver_evidence_ids) | set(request.company_evidence_ids)
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(all_ids))
        )
        rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(all_ids):
            raise MacroAnalysisEvidenceNotFound()
        for card in by_id.values():
            if card.company_id != request.company_id:
                raise MacroAnalysisEvidenceCompanyMismatch()
        return by_id

    @staticmethod
    async def _load_snapshots(
        session,
        cards: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, MacroDatasetSnapshotModel]:
        """加载 macro 卡的 MacroDatasetSnapshot（availability + indicator 投影）。"""
        snapshot_ids = {
            card.macro_snapshot_id
            for card in cards.values()
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
            and card.macro_snapshot_id is not None
        }
        if not snapshot_ids:
            return {}
        result = await session.execute(
            select(MacroDatasetSnapshotModel).where(
                MacroDatasetSnapshotModel.snapshot_id.in_(snapshot_ids)
            )
        )
        by_id = {row.snapshot_id: row for row in result.scalars().all()}
        if len(by_id) != len(snapshot_ids):
            raise MacroAnalysisEvidenceCorrupted(
                "macro analysis evidence snapshot missing (corrupted provenance)"
            )
        return by_id

    @staticmethod
    async def _load_observations(
        session,
        cards: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, MacroObservationModel]:
        """加载 macro 卡的 MacroObservation（period / value / unit 投影）。"""
        obs_ids = {
            card.macro_observation_id
            for card in cards.values()
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
            and card.macro_observation_id is not None
        }
        if not obs_ids:
            return {}
        result = await session.execute(
            select(MacroObservationModel).where(MacroObservationModel.observation_id.in_(obs_ids))
        )
        by_id = {row.observation_id: row for row in result.scalars().all()}
        if len(by_id) != len(obs_ids):
            raise MacroAnalysisEvidenceCorrupted(
                "macro analysis evidence observation missing (corrupted provenance)"
            )
        return by_id

    @staticmethod
    async def _load_series(
        session,
        cards: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, MacroSeriesModel]:
        """加载 macro 卡的 MacroSeries（series identity 投影）。"""
        series_ids = {
            card.macro_series_id
            for card in cards.values()
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
            and card.macro_series_id is not None
        }
        if not series_ids:
            return {}
        result = await session.execute(
            select(MacroSeriesModel).where(MacroSeriesModel.series_id.in_(series_ids))
        )
        by_id = {row.series_id: row for row in result.scalars().all()}
        if len(by_id) != len(series_ids):
            raise MacroAnalysisEvidenceCorrupted(
                "macro analysis evidence series missing (corrupted provenance)"
            )
        return by_id

    @staticmethod
    async def _load_source_records(
        session,
        cards: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, SourceRecordModel]:
        """加载 document 卡的 SourceRecord（availability + document_type 校验）。"""
        source_ids = {
            card.source_id
            for card in cards.values()
            if card.origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value
            and card.source_id is not None
        }
        if not source_ids:
            return {}
        result = await session.execute(
            select(SourceRecordModel).where(SourceRecordModel.source_id.in_(source_ids))
        )
        by_id = {row.source_id: row for row in result.scalars().all()}
        if len(by_id) != len(source_ids):
            raise MacroAnalysisEvidenceCorrupted(
                "macro analysis evidence source missing (corrupted provenance)"
            )
        return by_id

    def _build_driver_sources(
        self,
        request: MacroAnalysisRequest,
        cards: dict[UUID, EvidenceCardModel],
        observations: dict[UUID, MacroObservationModel],
        snapshots: dict[UUID, MacroDatasetSnapshotModel],
        series: dict[UUID, MacroSeriesModel],
        source_records: dict[UUID, SourceRecordModel],
    ) -> list[MacroDriverPackSource]:
        """macro_driver 池：逐条校验 v3 资格 + availability，再构造最小投影。"""
        sources: list[MacroDriverPackSource] = []
        for card_id in request.macro_driver_evidence_ids:
            card = cards[card_id]
            source_document_type = None
            if card.origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value:
                source = source_records.get(card.source_id)
                if source is None:
                    raise MacroAnalysisEvidenceCorrupted(
                        "macro analysis evidence source missing (corrupted provenance)"
                    )
                source_document_type = source.document_type
            if not driver_evidence_eligible(
                origin_type=card.origin_type,
                evidence_type=card.evidence_type,
                source_document_type=source_document_type,
            ):
                raise MacroAnalysisOriginViolation(
                    "macro_driver evidence must be macro_observation or an eligible "
                    "external event document (news_article + event/fact/statement)"
                )
            availability = self._availability(card, snapshots, source_records)
            if availability is None:
                raise MacroAnalysisTemporalEvidenceInsufficient()
            if self._normalize_availability(availability).date() > request.analysis_as_of:
                raise MacroAnalysisFutureEvidence()
            sources.append(
                self._driver_source(
                    card,
                    observations,
                    snapshots,
                    series,
                    source_records,
                    availability,
                )
            )
        return sources

    def _build_company_sources(
        self,
        request: MacroAnalysisRequest,
        cards: dict[UUID, EvidenceCardModel],
        source_records: dict[UUID, SourceRecordModel],
    ) -> list[CompanyEvidencePackSource]:
        """company 池：每条必须 origin_type=document_chunk + availability 校验。"""
        sources: list[CompanyEvidencePackSource] = []
        for card_id in request.company_evidence_ids:
            card = cards[card_id]
            if card.origin_type != EvidenceOrigin.DOCUMENT_CHUNK.value:
                raise MacroAnalysisOriginViolation(
                    "company exposure evidence must be origin_type=document_chunk"
                )
            availability = self._availability(card, {}, source_records)
            if availability is None:
                raise MacroAnalysisTemporalEvidenceInsufficient()
            if self._normalize_availability(availability).date() > request.analysis_as_of:
                raise MacroAnalysisFutureEvidence()
            sources.append(
                CompanyEvidencePackSource(
                    evidence_card_id=card.evidence_card_id,
                    evidence_statement=card.evidence_statement,
                    evidence_type=card.evidence_type,
                    provider_key=card.provider_key,
                    authority_tier_snapshot=card.authority_tier_snapshot,
                    availability=availability,
                    quote_text=card.quote_text,
                    published_at=card.source_published_at,
                    reporting_period_end=card.reporting_period_end,
                )
            )
        return sources

    def _driver_source(
        self,
        card: EvidenceCardModel,
        observations: dict[UUID, MacroObservationModel],
        snapshots: dict[UUID, MacroDatasetSnapshotModel],
        series: dict[UUID, MacroSeriesModel],
        source_records: dict[UUID, SourceRecordModel],
        availability: datetime,
    ) -> MacroDriverPackSource:
        """构造单条 MacroDriverPackSource（macro_observation / document_chunk 两种投影）。"""
        if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
            obs = observations.get(card.macro_observation_id)
            snapshot = snapshots.get(card.macro_snapshot_id)
            ser = series.get(card.macro_series_id)
            if obs is None or snapshot is None or ser is None:
                raise MacroAnalysisEvidenceCorrupted(
                    "macro analysis evidence provenance chain corrupted"
                )
            return MacroDriverPackSource(
                evidence_card_id=card.evidence_card_id,
                origin_type=card.origin_type,
                evidence_statement=card.evidence_statement,
                evidence_type=card.evidence_type,
                provider_key=card.provider_key,
                authority_tier_snapshot=card.authority_tier_snapshot,
                availability=availability,
                effective_period_summary=f"观测期 {obs.period}（{obs.period_semantics}）",
                indicator_name=snapshot.indicator_name,
                series_identity=f"{ser.provider_key} {ser.geography_code} {ser.frequency}",
                observation_period=obs.period,
                value_summary=self._value_summary(obs, snapshot),
                indicator_unit=snapshot.indicator_unit,
            )
        source = source_records.get(card.source_id)
        if source is None:
            raise MacroAnalysisEvidenceCorrupted(
                "macro analysis evidence source missing (corrupted provenance)"
            )
        return MacroDriverPackSource(
            evidence_card_id=card.evidence_card_id,
            origin_type=card.origin_type,
            evidence_statement=card.evidence_statement,
            evidence_type=card.evidence_type,
            provider_key=card.provider_key,
            authority_tier_snapshot=card.authority_tier_snapshot,
            availability=availability,
            effective_period_summary=self._document_period_summary(card, source),
            quote_text=card.quote_text,
            document_type=source.document_type,
            published_at=card.source_published_at,
            reporting_period_end=card.reporting_period_end,
        )

    @staticmethod
    def _value_summary(obs: MacroObservationModel, snapshot: MacroDatasetSnapshotModel) -> str:
        """确定性的观测值摘要（真实数值 + 单位；缺失 → '缺失'，不编造）。"""
        if obs.is_missing:
            return "缺失（is_missing）"
        if obs.value_numeric is not None:
            unit = snapshot.indicator_unit or ""
            return f"{format(obs.value_numeric, 'f')} {unit}".strip()
        return "未披露数值"

    @staticmethod
    def _document_period_summary(
        card: EvidenceCardModel, source: SourceRecordModel
    ) -> str:
        """确定性的文档期间摘要（报告期优先，否则发布期）。"""
        if card.reporting_period_end is not None:
            return f"报告期 {card.reporting_period_end.isoformat()}"
        if source.published_at is not None:
            return f"发布于 {source.published_at.date().isoformat()}"
        return "期间未披露"

    @staticmethod
    def _normalize_availability(dt: datetime) -> datetime:
        """availability normalize 为 UTC aware datetime（day-granularity cutoff）。"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    @staticmethod
    def _availability(
        card: EvidenceCardModel,
        snapshots: dict[UUID, MacroDatasetSnapshotModel],
        source_records: dict[UUID, SourceRecordModel],
    ) -> datetime | None:
        """v2/v3 information availability（真实 provenance，不伪造缺失日期）。

        provenance 值解析委托 macro_policy.resolve_availability——与
        MacroClaimService **共用**同一 no-lookahead 策略，禁止重复实现；
        本方法只负责把缺失 provenance 映射为数据损坏（Corrupted）。
        """
        if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
            snapshot = snapshots.get(card.macro_snapshot_id)
            if snapshot is None:
                raise MacroAnalysisEvidenceCorrupted(
                    "macro analysis evidence snapshot missing (corrupted provenance)"
                )
            return resolve_availability(
                origin_type=card.origin_type,
                snapshot_fetched_at=snapshot.fetched_at,
                source_published_at=None,
                source_acquired_at=None,
            )
        source = source_records.get(card.source_id)
        if source is None:
            raise MacroAnalysisEvidenceCorrupted(
                "macro analysis evidence source missing (corrupted provenance)"
            )
        return resolve_availability(
            origin_type=card.origin_type,
            snapshot_fetched_at=None,
            source_published_at=source.published_at,
            source_acquired_at=source.acquired_at,
        )

    async def _call_model(
        self,
        context: MacroAnalysisContext,
        driver_pack,
        company_pack,
    ) -> MacroAnalysisDecision:
        """调用模型并归一到 MacroAnalysisDecision（防御性 double-check）。

        模型层负责解析；这里再对返回结果做一次 schema 校验（provider 可能
        返回 raw dict / 已构造对象），ValidationError → MalformedOutput。
        """
        raw = await self._model.analyze(context, driver_pack, company_pack)
        if isinstance(raw, MacroAnalysisDecision):
            return raw
        try:
            return MacroAnalysisDecision.model_validate(raw)
        except ValidationError as exc:
            raise MacroAnalysisMalformedOutput() from exc

    def _build_drafts(
        self,
        request: MacroAnalysisRequest,
        resolved: list[ResolvedMacroClaim],
    ) -> list[MacroClaimDraft]:
        """把解析后的 Claim 候选构造为 MacroClaimDraft（v6；analyst 身份固定）。

        MacroClaimDraft 构造时已做去重 + canonical 排序（幂等）。
        """
        drafts: list[MacroClaimDraft] = []
        for claim in resolved:
            drafts.append(
                MacroClaimDraft(
                    company_id=request.company_id,
                    research_question=request.research_question,
                    analysis_as_of=request.analysis_as_of,
                    statement=claim.statement,
                    claim_kind=claim.claim_kind,
                    confidence=claim.confidence,
                    importance=claim.importance,
                    channel_type=claim.channel_type,
                    effect_direction=claim.effect_direction,
                    impact_status=claim.impact_status,
                    time_alignment=claim.time_alignment,
                    macro_driver_evidence_ids=list(claim.macro_driver_ids),
                    company_exposure_evidence_ids=list(claim.company_exposure_ids),
                    observed_effect_evidence_ids=list(claim.observed_effect_ids),
                    additional_support_evidence_ids=list(claim.additional_supports),
                    additional_contradict_evidence_ids=list(claim.additional_contradicts),
                    additional_context_evidence_ids=list(claim.additional_context),
                    analyst_name=MACRO_ANALYST_NAME,
                    analyst_version=MACRO_ANALYST_VERSION,
                    analyst_model_id=self._model.model_id,
                )
            )
        return drafts

    @staticmethod
    def _check_overclaim_policy(resolved: list[ResolvedMacroClaim]) -> None:
        """overclaim contract 防线：observed_impact 需 ≥1 observed_effect；uncertain
        只允许 plausible + risk + normal。

        MacroClaimCandidate schema 已拒绝违规组合；此处对最终 ResolvedMacroClaim
        再做一次兜底（即使绕过 Pydantic，违规也 → MacroAnalysisOverclaimPolicy，
        而不是落在 claim-domain 错误上）。
        """
        for claim in resolved:
            if (
                claim.impact_status == MacroImpactStatus.OBSERVED_IMPACT
                and not claim.observed_effect_ids
            ):
                raise MacroAnalysisOverclaimPolicy(
                    "observed_impact 需要 ≥1 observed_effect（否则只能 plausible_impact）"
                )
            if claim.time_alignment == MacroTimeAlignment.UNCERTAIN and (
                claim.claim_kind != ClaimKind.RISK
                or claim.importance != MacroClaimImportance.NORMAL
                or claim.impact_status != MacroImpactStatus.PLAUSIBLE_IMPACT
            ):
                raise MacroAnalysisOverclaimPolicy(
                    "time_alignment=uncertain 只允许 plausible_impact + risk + normal"
                )

    @staticmethod
    def _check_kind_policy(drafts: list[MacroClaimDraft]) -> None:
        """claim_kind 防线：Macro Analyst 只允许 inference / risk。

        MacroClaimCandidate schema 已拒绝 fact / relative_valuation；此处对最终
        MacroClaimDraft 再做一次兜底（即使绕过 Pydantic，fact 也会 →
        MacroAnalysisClaimKindPolicy）。MacroClaimDraft 本身仍支持 fact（更低层
        domain contract），本防线只作用于 Macro Analysis 路径。
        """
        for draft in drafts:
            if draft.claim_kind not in _ALLOWED_KINDS_MACRO_ANALYST:
                raise MacroAnalysisClaimKindPolicy(
                    f"claim_kind {draft.claim_kind.value} incompatible with macro analysis"
                )
