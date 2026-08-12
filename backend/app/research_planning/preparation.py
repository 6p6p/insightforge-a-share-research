"""Research preparation service (stage 7A.1 spec L/M/N/O).

按 ResearchPlan + SourceRoutePlan 从**现有** artifact repositories 解析每个 need
（company / analysis_as_of / period / provenance 过滤），产出：

- `resolved`：每个 need 的实际 artifact IDs（evidence_card / calculation /
  macro_driver_evidence / comparison）；
- `missing_needs`：解析失败的 need（reason_code：not_found /
  insufficient_evidence / missing_period / missing_metric /
  missing_macro_observation / missing_valuation_comparison /
  provider_unavailable / unsupported_need）；
- `module_inputs`：每个 analysis_module 的实际输入（business/risk 用解析到的
  document+event 证据池；financial 用解析到的 calculation；macro 用 driver +
  company 证据；valuation 用 comparison）；
- `ready_for_analysis`：**所有 need 都解析成功 且 每个已选 module 都有非空输入**；
- `stage4_request`：ready 时构造现有 `Stage4WorkflowRequest`（否则 None）。

硬边界（spec P/Q/S）：
- **0 fake readiness**：不给已选 module 硬塞 Evidence；module 输入为空 →
  not ready；
- **no-lookahead**：document 卡用 SourceRecord.published_at（fallback
  acquired_at），macro 卡用 MacroDatasetSnapshot.fetched_at —— 复用
  `macro_policy.resolve_availability`，禁止重实现；
- **company 过滤**：所有解析只取 plan.company_id 的 artifacts（跨公司证据排除）；
- **critical_claim_eligible 不提升**：ResolvedNeed 只投影真实
  `critical_claim_eligible_snapshot` 计数 / authority tier 最小值（元数据，
  不修改任何行、不因 critical-eligible 少而 fake ready）。

**0 real DeepSeek / 0 Retrieval / 0 Chroma query / 0 Web fetch**：只读现有 PG
artifact 表。不执行新抓取 / 下载 / World Bank / GDELT / ResearchBackflow /
Top-level workflow（7A.2 范围外）。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.contracts import MAX_EVIDENCE_PER_REQUEST
from app.analysis.financial.contracts import MAX_CALCULATIONS_PER_REQUEST
from app.analysis.macro.contracts import (
    MAX_COMPANY_EVIDENCE_PER_REQUEST,
    MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST,
)
from app.analysis.valuation.contracts import MAX_VALUATION_COMPARISONS_PER_REQUEST
from app.claims.macro_policy import resolve_availability
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.financial_calculation import (
    FinancialCalculationInputModel,
    FinancialCalculationModel,
)
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.relative_valuation_comparison import RelativeValuationComparisonModel
from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import EvidenceOrigin, compute_research_question_sha256
from app.financial.calculations.contracts import (
    CalculationCode,
    InputRole,
    calculation_input_roles,
    expected_metric_code,
)
from app.research_planning.contracts import (
    _GROWTH_CALCULATION_CODES,
    ResearchDocumentNeedType,
    ResearchPlanPayload,
)
from app.research_planning.router import (
    ResearchSourceRouter,
    validate_route_payload,
)
from app.research_planning.service import (
    ResearchPlanningService,
    VerifiedPlanExecutionContext,
)
from app.stage4.contracts import (
    FinancialWorkItem,
    GenericWorkItem,
    MacroWorkItem,
    Stage4WorkflowRequest,
    ValuationWorkItem,
)


class MissingReasonCode(StrEnum):
    """spec N：need 解析失败的原因分类。"""

    NOT_FOUND = "not_found"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    MISSING_PERIOD = "missing_period"
    MISSING_METRIC = "missing_metric"
    MISSING_MACRO_OBSERVATION = "missing_macro_observation"
    MISSING_VALUATION_COMPARISON = "missing_valuation_comparison"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNSUPPORTED_NEED = "unsupported_need"


@dataclass(frozen=True)
class MissingResearchNeed:
    """spec N：一个未能解析的 need（模块级 missing 用 need_code=module:<module>）。"""

    need_code: str
    need_kind: str
    reason_code: MissingReasonCode
    detail: str


@dataclass(frozen=True)
class ResolvedNeed:
    """spec O resolved：一个 need 的实际 artifact 解析结果。"""

    need_code: str
    need_kind: str  # document / financial / macro / event / valuation
    artifact_type: str  # evidence_card / calculation / macro_driver_evidence / comparison
    artifact_ids: tuple[UUID, ...]
    critical_claim_eligible_count: int = 0
    min_authority_tier: int | None = None


@dataclass(frozen=True)
class ModuleInput:
    """spec O module_inputs：一个 analysis_module 的实际 Stage4 输入。"""

    analysis_type: str  # business / risk / financial / macro / valuation
    artifact_ids: tuple[UUID, ...]  # 主池（evidence / calculation / comparison）
    secondary_artifact_ids: tuple[UUID, ...] = ()  # macro 的 company 证据池


@dataclass(frozen=True)
class ResearchPreparationResult:
    """spec O：ResearchPreparation contract（stage4_request 可空）。"""

    research_plan_id: UUID
    resolved: tuple[ResolvedNeed, ...]
    module_inputs: tuple[ModuleInput, ...]
    missing_needs: tuple[MissingResearchNeed, ...]
    ready_for_analysis: bool
    stage4_request: Stage4WorkflowRequest | None


@dataclass
class _ResolutionData:
    """一次 prepare 内加载的全部 artifact（短 session 内一次性读取）。"""

    source_by_id: dict[UUID, SourceRecordModel]
    cards_by_source: dict[UUID, list[EvidenceCardModel]]
    macro_cards: list[EvidenceCardModel]
    snapshots: dict[UUID, MacroDatasetSnapshotModel]
    observations: list[FinancialMetricObservationModel]
    calc_inputs: list[FinancialCalculationInputModel]
    calculations: list[FinancialCalculationModel]
    comparisons: list[RelativeValuationComparisonModel]


def _sorted_ids(ids: Iterable[UUID]) -> tuple[UUID, ...]:
    return tuple(sorted({uuid for uuid in ids}, key=str))


def _sorted_cards(cards: Iterable[EvidenceCardModel]) -> tuple[UUID, ...]:
    return _sorted_ids(card.evidence_card_id for card in cards)


def _source_available(source: SourceRecordModel, analysis_as_of: date) -> bool:
    availability = resolve_availability(
        origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
        snapshot_fetched_at=None,
        source_published_at=source.published_at,
        source_acquired_at=source.acquired_at,
    )
    return availability is not None and availability.date() <= analysis_as_of


def _macro_available(
    snapshot: MacroDatasetSnapshotModel,
    analysis_as_of: date,
) -> bool:
    availability = resolve_availability(
        origin_type=EvidenceOrigin.MACRO_OBSERVATION.value,
        snapshot_fetched_at=snapshot.fetched_at,
        source_published_at=None,
        source_acquired_at=None,
    )
    return availability is not None and availability.date() <= analysis_as_of


class ResearchPreparationService:
    """按 plan + route 从现有 artifacts 解析资料 → auto Stage4 WorkPlan / MissingNeeds。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        plan_service: ResearchPlanningService,
        router: ResearchSourceRouter,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._plan_service = plan_service
        self._router = router

    # ------------------------------------------------------------------ 主入口

    async def prepare_research(self, research_plan_id: UUID) -> ResearchPreparationResult:
        """verify plan + route → 解析 needs → module_inputs → ready/stage4_request。

        全部执行语义（research_question / analysis_as_of / company_id / payload）
        来自 Verified Plan Execution Context（frozen `planner_input_payload`），
        **不读当前 ResearchTask 字段**（spec 7A.2A B）——Task 在 Plan 创建后被修改
        不影响既有 Plan 的执行。Task row 只用于存在性/ownership/FK（verify 已交叉
        核对 task 仍属于同一 company identity）。
        """
        ctx = await self._plan_service.get_verified_execution_context(research_plan_id)
        route = await self._router.verify_research_plan_route_integrity(research_plan_id)
        payload = ctx.payload
        route_payload = validate_route_payload(route.route_payload)

        analysis_as_of = ctx.analysis_as_of
        # Gate C：document EvidenceCard 只有在其 research_question 与 **frozen plan
        # research_question** 一致（sha256）时才算 ready 输入；不匹配 → 不计入。
        target_question_sha256 = compute_research_question_sha256(ctx.research_question)

        provider_keys_by_code = {
            entry.need_code: entry.provider_keys for entry in route_payload.entries
        }

        data = await self._load_resolution_data(ctx.company_id)

        resolved: list[ResolvedNeed] = []
        missing: list[MissingResearchNeed] = []
        doc_evidence_pool: set[UUID] = set()
        event_evidence_pool: set[UUID] = set()
        financial_calc_pool: set[UUID] = set()
        macro_driver_pool: set[UUID] = set()
        valuation_comp_pool: set[UUID] = set()

        # --- document needs
        for need in payload.document_needs:
            if not self._route_provider_ok(need.need_code, provider_keys_by_code):
                missing.append(
                    MissingResearchNeed(
                        need_code=need.need_code,
                        need_kind="document",
                        reason_code=MissingReasonCode.PROVIDER_UNAVAILABLE,
                        detail="路由当时无 provider 能服务该 document need",
                    )
                )
                continue
            if need.source_type == ResearchDocumentNeedType.MACRO_DATASET:
                # 宏观数据集 → 本系统的宏观数据形态 = MacroObservation 证据。
                cards, reason, detail = self._resolve_macro_driver(data, analysis_as_of)
                if reason is None:
                    macro_driver_pool.update(card.evidence_card_id for card in cards)
                    resolved.append(
                        self._resolved_need(
                            need.need_code, "document", "macro_driver_evidence", cards
                        )
                    )
                else:
                    missing.append(MissingResearchNeed(need.need_code, "document", reason, detail))
                continue
            cards, reason, detail = self._resolve_document_evidence(
                data,
                source_type=need.source_type.value,
                period=need.period,
                analysis_as_of=analysis_as_of,
                research_question_sha256=target_question_sha256,
            )
            if reason is None:
                doc_evidence_pool.update(card.evidence_card_id for card in cards)
                resolved.append(
                    self._resolved_need(need.need_code, "document", "evidence_card", cards)
                )
            else:
                missing.append(MissingResearchNeed(need.need_code, "document", reason, detail))

        # --- financial needs
        for need in payload.financial_needs:
            if not self._route_provider_ok(need.need_code, provider_keys_by_code):
                missing.append(
                    MissingResearchNeed(
                        need_code=need.need_code,
                        need_kind="financial",
                        reason_code=MissingReasonCode.PROVIDER_UNAVAILABLE,
                        detail="路由当时无 provider 能服务该 financial need",
                    )
                )
                continue
            calcs, reason, detail = self._resolve_financial(
                data,
                calculation_code=need.calculation_code.value,
                metric_code=need.metric_code.value if need.metric_code is not None else None,
                period=need.period,
            )
            if reason is None:
                financial_calc_pool.update(calc.calculation_id for calc in calcs)
                resolved.append(
                    ResolvedNeed(
                        need_code=need.need_code,
                        need_kind="financial",
                        artifact_type="calculation",
                        artifact_ids=_sorted_ids(calc.calculation_id for calc in calcs),
                    )
                )
            else:
                missing.append(MissingResearchNeed(need.need_code, "financial", reason, detail))

        # --- macro needs
        for need in payload.macro_needs:
            if not self._route_provider_ok(need.need_code, provider_keys_by_code):
                missing.append(
                    MissingResearchNeed(
                        need_code=need.need_code,
                        need_kind="macro",
                        reason_code=MissingReasonCode.PROVIDER_UNAVAILABLE,
                        detail="路由当时无 provider 能服务该 macro need",
                    )
                )
                continue
            cards, reason, detail = self._resolve_macro_driver(data, analysis_as_of)
            if reason is None:
                macro_driver_pool.update(card.evidence_card_id for card in cards)
                resolved.append(
                    self._resolved_need(need.need_code, "macro", "macro_driver_evidence", cards)
                )
            else:
                missing.append(MissingResearchNeed(need.need_code, "macro", reason, detail))

        # --- event needs
        for need in payload.event_needs:
            if not self._route_provider_ok(need.need_code, provider_keys_by_code):
                missing.append(
                    MissingResearchNeed(
                        need_code=need.need_code,
                        need_kind="event",
                        reason_code=MissingReasonCode.PROVIDER_UNAVAILABLE,
                        detail="路由当时无 provider 能服务该 event need",
                    )
                )
                continue
            cards, reason, detail = self._resolve_document_evidence(
                data,
                source_type="news_article",
                period=None,
                analysis_as_of=analysis_as_of,
                research_question_sha256=target_question_sha256,
            )
            if reason is None:
                event_evidence_pool.update(card.evidence_card_id for card in cards)
                resolved.append(
                    self._resolved_need(need.need_code, "event", "evidence_card", cards)
                )
            else:
                missing.append(MissingResearchNeed(need.need_code, "event", reason, detail))

        # --- valuation needs
        for need in payload.valuation_needs:
            if not self._route_provider_ok(need.need_code, provider_keys_by_code):
                missing.append(
                    MissingResearchNeed(
                        need_code=need.need_code,
                        need_kind="valuation",
                        reason_code=MissingReasonCode.PROVIDER_UNAVAILABLE,
                        detail="路由当时无 provider 能服务该 valuation need",
                    )
                )
                continue
            comps, reason, detail = self._resolve_valuation(
                data, metric_code=need.metric_code.value, analysis_as_of=analysis_as_of
            )
            if reason is None:
                valuation_comp_pool.update(comp.comparison_id for comp in comps)
                resolved.append(
                    ResolvedNeed(
                        need_code=need.need_code,
                        need_kind="valuation",
                        artifact_type="comparison",
                        artifact_ids=_sorted_ids(comp.comparison_id for comp in comps),
                    )
                )
            else:
                missing.append(MissingResearchNeed(need.need_code, "valuation", reason, detail))

        # --- module inputs（只按 plan 声明的 modules 构造；空输入 → module missing）
        business_pool = _sorted_ids(doc_evidence_pool | event_evidence_pool)
        module_inputs = self._build_module_inputs(
            payload, business_pool, financial_calc_pool, macro_driver_pool, valuation_comp_pool
        )
        for input_ in module_inputs:
            if not input_.artifact_ids or (
                input_.analysis_type == "macro" and not input_.secondary_artifact_ids
            ):
                missing.append(
                    MissingResearchNeed(
                        need_code=f"module:{input_.analysis_type}",
                        need_kind="module",
                        reason_code=MissingReasonCode.INSUFFICIENT_EVIDENCE,
                        detail=f"analysis_module {input_.analysis_type} 无足够输入",
                    )
                )

        ready = not missing
        stage4_request = self._build_stage4_request(ctx, module_inputs) if ready else None
        return ResearchPreparationResult(
            research_plan_id=ctx.research_plan_id,
            resolved=tuple(resolved),
            module_inputs=tuple(module_inputs),
            missing_needs=tuple(missing),
            ready_for_analysis=ready,
            stage4_request=stage4_request,
        )

    # ------------------------------------------------------------------ 解析器

    @staticmethod
    def _route_provider_ok(
        need_code: str,
        provider_keys_by_code: dict[str, list[str]],
    ) -> bool:
        return bool(provider_keys_by_code.get(need_code))

    def _resolve_document_evidence(
        self,
        data: _ResolutionData,
        *,
        source_type: str,
        period: str | None,
        analysis_as_of: date,
        research_question_sha256: str | None,
    ) -> tuple[list[EvidenceCardModel], MissingReasonCode | None, str | None]:
        """document need：按 document_type / period / availability / **研究问题一致性**
        （Gate C）解析 evidence 卡。

        只有当卡提取时的 research_question 与当前任务研究问题 sha256 一致时，
        该 EvidenceCard 才算 ready 输入——同一 source 为另一个研究问题提取的证据
        不能冒充本任务证据（不误 ready）。
        """
        candidates = (
            list(data.source_by_id.values())
            if source_type == ResearchDocumentNeedType.OTHER.value
            else [
                source
                for source in data.source_by_id.values()
                if source.document_type == source_type
            ]
        )
        if not candidates:
            return [], MissingReasonCode.NOT_FOUND, f"无 document_type={source_type} 的 source"
        if period:
            period_candidates = [
                source
                for source in candidates
                if source.reporting_period_end is not None
                and source.reporting_period_end.year == int(period)
            ]
            if not period_candidates:
                return [], MissingReasonCode.MISSING_PERIOD, f"无 {period} 年期 source"
            candidates = period_candidates
        available = [source for source in candidates if _source_available(source, analysis_as_of)]
        if not available:
            return [], MissingReasonCode.INSUFFICIENT_EVIDENCE, "无在基准日之前可得的 source"
        all_cards = [
            card for source in available for card in data.cards_by_source.get(source.source_id, [])
        ]
        if not all_cards:
            return [], MissingReasonCode.INSUFFICIENT_EVIDENCE, "source 存在但无已提取 evidence"
        cards = [
            card
            for card in all_cards
            if research_question_sha256 is None
            or card.research_question_sha256 == research_question_sha256
        ]
        if not cards:
            return (
                [],
                MissingReasonCode.INSUFFICIENT_EVIDENCE,
                "source 存在但证据研究问题与当前任务不一致",
            )
        return cards, None, None

    def _resolve_financial(
        self,
        data: _ResolutionData,
        *,
        calculation_code: str,
        metric_code: str | None,
        period: str | None,
    ) -> tuple[list[FinancialCalculationModel], MissingReasonCode | None, str | None]:
        """financial need（calculation-centric，spec B）：从既有 calculations 解析。

        程序根据 calculation_code 推导所需 observations（input roles + 期望
        metric），只匹配**同一 calculation_code + 满足 metric/period 语义**的
        calculation——有 revenue yoy 但 plan 要 gross_margin → 不匹配（不误 ready）。
        """
        try:
            code = CalculationCode(calculation_code)
        except ValueError:
            return (
                [],
                MissingReasonCode.UNSUPPORTED_NEED,
                f"不支持的 calculation_code={calculation_code}",
            )

        calc_inputs_map = self._calc_observation_map(data)
        calcs = [calc for calc in data.calculations if calc.calculation_code == calculation_code]
        matched = [
            calc
            for calc in calcs
            if self._calc_matches(
                calc_inputs_map.get(calc.calculation_id, {}), code, metric_code, period
            )
        ]
        if matched:
            return matched, None, None

        if not self._observations_sufficient_for_calc(data, code, metric_code):
            return (
                [],
                MissingReasonCode.MISSING_METRIC,
                f"无 {calculation_code} 所需的 observation",
            )
        return (
            [],
            MissingReasonCode.INSUFFICIENT_EVIDENCE,
            f"observation 存在但无 {calculation_code} calculation",
        )

    @staticmethod
    def _calc_observation_map(
        data: _ResolutionData,
    ) -> dict[UUID, dict[str, FinancialMetricObservationModel]]:
        """calc_id → {input_role: observation}（一次构建，供多次 need 解析复用）。"""
        obs_by_id = {obs.metric_observation_id: obs for obs in data.observations}
        mapping: dict[UUID, dict[str, FinancialMetricObservationModel]] = {}
        for row in data.calc_inputs:
            obs = obs_by_id.get(row.metric_observation_id)
            if obs is not None:
                mapping.setdefault(row.calculation_id, {})[row.input_role] = obs
        return mapping

    @staticmethod
    def _calc_matches(
        inputs: dict[str, FinancialMetricObservationModel],
        code: CalculationCode,
        metric_code: str | None,
        period: str | None,
    ) -> bool:
        """一条 calculation 是否满足该 need 的 metric/period 语义。"""
        roles = calculation_input_roles(code)
        if set(inputs) != {role.value for role in roles}:
            return False
        for role in roles:
            obs = inputs.get(role.value)
            if obs is None:
                return False
            expected = expected_metric_code(role)
            if expected is None:
                if metric_code is not None and obs.metric_code != metric_code:
                    return False
            elif obs.metric_code != expected.value:
                return False
        if period:
            target_year = int(period)
            if code in _GROWTH_CALCULATION_CODES:
                current = inputs.get(InputRole.CURRENT.value)
                if current is None or current.period_end.year != target_year:
                    return False
            else:
                first = next(iter(inputs.values()))
                if first.period_end.year != target_year:
                    return False
        return True

    @staticmethod
    def _observations_sufficient_for_calc(
        data: _ResolutionData,
        code: CalculationCode,
        metric_code: str | None,
    ) -> bool:
        """所需 observations（按 calculation_code 推导）是否在库中存在。"""
        obs_metrics = {obs.metric_code for obs in data.observations}
        for role in calculation_input_roles(code):
            expected = expected_metric_code(role)
            need_metric = expected.value if expected is not None else metric_code
            if need_metric is None or need_metric not in obs_metrics:
                return False
        return True

    def _resolve_macro_driver(
        self,
        data: _ResolutionData,
        analysis_as_of: date,
    ) -> tuple[list[EvidenceCardModel], MissingReasonCode | None, str | None]:
        """macro need：driver-eligible（macro_observation）+ fetched_at no-lookahead。"""
        cards = [
            card
            for card in data.macro_cards
            if _macro_available(data.snapshots[card.macro_snapshot_id], analysis_as_of)
        ]
        if not cards:
            return [], MissingReasonCode.MISSING_MACRO_OBSERVATION, "无可用 macro observation 证据"
        return cards, None, None

    def _resolve_valuation(
        self,
        data: _ResolutionData,
        *,
        metric_code: str,
        analysis_as_of: date,
    ) -> tuple[
        list[RelativeValuationComparisonModel],
        MissingReasonCode | None,
        str | None,
    ]:
        """valuation need：target company + metric + metric_as_of no-lookahead。"""
        comps = [
            comp
            for comp in data.comparisons
            if comp.metric_code == metric_code and comp.metric_as_of <= analysis_as_of
        ]
        if not comps:
            detail = f"无 {metric_code} 的 comparison"
            return [], MissingReasonCode.MISSING_VALUATION_COMPARISON, detail
        return comps, None, None

    @staticmethod
    def _resolved_need(
        need_code: str,
        need_kind: str,
        artifact_type: str,
        cards: list[EvidenceCardModel],
    ) -> ResolvedNeed:
        """evidence 解析的 ResolvedNeed（投影真实 critical/authority 元数据，不提升）。"""
        eligible = sum(1 for card in cards if card.critical_claim_eligible_snapshot)
        tiers = [card.authority_tier_snapshot for card in cards]
        return ResolvedNeed(
            need_code=need_code,
            need_kind=need_kind,
            artifact_type=artifact_type,
            artifact_ids=_sorted_cards(cards),
            critical_claim_eligible_count=eligible,
            min_authority_tier=min(tiers) if tiers else None,
        )

    # ------------------------------------------------------------------ module 构造

    def _build_module_inputs(
        self,
        payload: ResearchPlanPayload,
        business_pool: tuple[UUID, ...],
        financial_calc_pool: set[UUID],
        macro_driver_pool: set[UUID],
        valuation_comp_pool: set[UUID],
    ) -> list[ModuleInput]:
        """只按 plan 声明的 analysis_modules 构造输入（去重保序）。"""
        inputs: list[ModuleInput] = []
        for module in dict.fromkeys(payload.analysis_modules):
            value = module.value
            if value == "business_event":
                inputs.append(
                    ModuleInput(
                        analysis_type="business",
                        artifact_ids=business_pool[:MAX_EVIDENCE_PER_REQUEST],
                    )
                )
            elif value == "risk":
                inputs.append(
                    ModuleInput(
                        analysis_type="risk",
                        artifact_ids=business_pool[:MAX_EVIDENCE_PER_REQUEST],
                    )
                )
            elif value == "financial":
                inputs.append(
                    ModuleInput(
                        analysis_type="financial",
                        artifact_ids=_sorted_ids(financial_calc_pool)[
                            :MAX_CALCULATIONS_PER_REQUEST
                        ],
                    )
                )
            elif value == "macro":
                inputs.append(
                    ModuleInput(
                        analysis_type="macro",
                        artifact_ids=_sorted_ids(macro_driver_pool)[
                            :MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST
                        ],
                        secondary_artifact_ids=business_pool[:MAX_COMPANY_EVIDENCE_PER_REQUEST],
                    )
                )
            elif value == "valuation":
                inputs.append(
                    ModuleInput(
                        analysis_type="valuation",
                        artifact_ids=_sorted_ids(valuation_comp_pool)[
                            :MAX_VALUATION_COMPARISONS_PER_REQUEST
                        ],
                    )
                )
        return inputs

    def _build_stage4_request(
        self,
        ctx: VerifiedPlanExecutionContext,
        module_inputs: list[ModuleInput],
    ) -> Stage4WorkflowRequest:
        """按 module_inputs 构造现有 Stage4WorkflowRequest（question/as_of 来自 frozen ctx）。"""
        items = []
        for index, input_ in enumerate(module_inputs, start=1):
            item_id = f"{input_.analysis_type}-{index}"
            if input_.analysis_type in ("business", "risk"):
                items.append(
                    GenericWorkItem(
                        item_id=item_id,
                        analysis_type=input_.analysis_type,
                        evidence_card_ids=list(input_.artifact_ids),
                    )
                )
            elif input_.analysis_type == "financial":
                items.append(
                    FinancialWorkItem(
                        item_id=item_id,
                        analysis_type="financial",
                        calculation_ids=list(input_.artifact_ids),
                        additional_evidence_ids=[],
                    )
                )
            elif input_.analysis_type == "macro":
                items.append(
                    MacroWorkItem(
                        item_id=item_id,
                        analysis_type="macro",
                        macro_driver_evidence_ids=list(input_.artifact_ids),
                        company_evidence_ids=list(input_.secondary_artifact_ids),
                    )
                )
            elif input_.analysis_type == "valuation":
                items.append(
                    ValuationWorkItem(
                        item_id=item_id,
                        analysis_type="valuation",
                        comparison_ids=list(input_.artifact_ids),
                    )
                )
        return Stage4WorkflowRequest(
            task_id=ctx.task_id,
            company_id=ctx.company_id,
            research_question=ctx.research_question,
            analysis_as_of=ctx.analysis_as_of,
            analysis_work_items=items,
        )

    # ------------------------------------------------------------------ 数据加载

    async def _load_resolution_data(self, company_id: UUID) -> _ResolutionData:
        async with self._sessionmaker() as session:
            source_rows = await session.execute(
                select(SourceRecordModel).where(SourceRecordModel.company_id == company_id)
            )
            sources = list(source_rows.scalars().all())

            card_rows = await session.execute(
                select(EvidenceCardModel).where(EvidenceCardModel.company_id == company_id)
            )
            cards = list(card_rows.scalars().all())

            macro_snapshot_ids = {
                card.macro_snapshot_id
                for card in cards
                if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
                and card.macro_snapshot_id is not None
            }
            if macro_snapshot_ids:
                snap_rows = await session.execute(
                    select(MacroDatasetSnapshotModel).where(
                        MacroDatasetSnapshotModel.snapshot_id.in_(macro_snapshot_ids)
                    )
                )
                snapshots = {row.snapshot_id: row for row in snap_rows.scalars().all()}
            else:
                snapshots = {}

            obs_rows = await session.execute(
                select(FinancialMetricObservationModel).where(
                    FinancialMetricObservationModel.company_id == company_id
                )
            )
            observations = list(obs_rows.scalars().all())

            obs_ids = {obs.metric_observation_id for obs in observations}
            if obs_ids:
                input_rows = await session.execute(
                    select(FinancialCalculationInputModel).where(
                        FinancialCalculationInputModel.metric_observation_id.in_(obs_ids)
                    )
                )
                calc_inputs = list(input_rows.scalars().all())
            else:
                calc_inputs = []

            calc_rows = await session.execute(
                select(FinancialCalculationModel).where(
                    FinancialCalculationModel.company_id == company_id
                )
            )
            calculations = list(calc_rows.scalars().all())

            comp_rows = await session.execute(
                select(RelativeValuationComparisonModel).where(
                    RelativeValuationComparisonModel.target_company_id == company_id
                )
            )
            comparisons = list(comp_rows.scalars().all())

        source_by_id = {source.source_id: source for source in sources}
        cards_by_source: dict[UUID, list[EvidenceCardModel]] = {}
        macro_cards: list[EvidenceCardModel] = []
        for card in cards:
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
                macro_cards.append(card)
            elif card.source_id is not None:
                cards_by_source.setdefault(card.source_id, []).append(card)
        return _ResolutionData(
            source_by_id=source_by_id,
            cards_by_source=cards_by_source,
            macro_cards=macro_cards,
            snapshots=snapshots,
            observations=observations,
            calc_inputs=calc_inputs,
            calculations=calculations,
            comparisons=comparisons,
        )
