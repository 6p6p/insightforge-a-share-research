"""Financial need executor (stage 7A.2A spec M): calculation → re-preparation。

对一条 missing financial need（calculation-centric，spec B）自动补证据：
1. **calculation_code → 确定 requirements**：`calculation_input_roles(code)` 给
   出每个 input role；fixed role 的期望 metric 用 `expected_metric_code(role)`，
   growth（current/baseline）的 metric 用 `FinancialNeed.metric_code`；
2. 从既有 `financial_metric_observations` 按 (metric, period, statement_scope)
   匹配每个 role 的 Observation——**缺失 → MISSING_UNDERLYING_OBSERVATION**
   （unresolved，spec P：underlying metric 不存在，不凭空造数）；
3. 全部 role 齐备 → `FinancialCalculationService.create_calculation`（确定性
   create-or-get → 重新 preparation）；
4. 计算输入不兼容（period/scope/metric）→ CALCULATION_INPUT_ERROR。

硬边界（spec M/J）：**0 LLM / 0 Retrieval / 0 Chroma / 0 Web**；只消费已登记
Observation；不 fetch / 不下载披露。幂等：fingerprint replay → 第 2 次调用
0 新增写（spec Q）。executor 不抛确定性错误。
"""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.source_record import SourceRecordModel
from app.financial.calculations.contracts import (
    CalculationCode,
    FinancialCalculationDraft,
    InputRole,
    calculation_input_roles,
    expected_metric_code,
)
from app.financial.calculations.errors import FinancialCalculationError
from app.financial.calculations.service import FinancialCalculationService
from app.financial.extraction.contracts import (
    FinancialExtractionProvider,
    FinancialExtractionRequest,
)
from app.financial.extraction.ingestion import FinancialExtractionIngestionService
from app.financial.extraction.service import FinancialExtractionService
from app.research_fulfillment.contracts import (
    FulfillmentAttempt,
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.service import FulfillmentContext
from app.research_planning.contracts import _GROWTH_CALCULATION_CODES
from app.research_planning.preparation import MissingResearchNeed
from app.research_planning.router import SourceRouteEntry


class _FinancialError(Exception):
    """内部错误分类（翻译为 FulfillmentErrorCode 前的本地状态）。"""

    MISSING_OBSERVATION = "missing_observation"
    CALCULATION_REJECTED = "calculation_rejected"

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _question_sha(research_question: str | None) -> str | None:
    """研究问题 -> sha256（与 evidence card 同一函数；None → None）。"""
    if not research_question:
        return None
    from app.claims.contracts import compute_research_question_sha256

    return compute_research_question_sha256(research_question)


class FinancialNeedExecutor:
    """financial need 自动补证据：Observation → create_calculation → 重跑。

    F1（Final Autonomous Research）：注入 `extraction`（FinancialExtraction-
    IngestionService）后，缺失底层 Observation 时先尝试**自动财务提取**——
    从公司 eligible 年报的 parsed blocks 确定性提取指标（0 LLM）→ 证据卡 +
    observation 落库 → 重查；提取失败保持 MISSING_UNDERLYING_OBSERVATION
    （human fallback，绝不编造数字）。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        extraction: FinancialExtractionIngestionService | None = None,
        extraction_service: FinancialExtractionService | None = None,
        provider: FinancialExtractionProvider | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._extraction = extraction
        self._extraction_service = extraction_service
        self._provider = provider

    # ------------------------------------------------------------ 主入口

    async def fulfill(
        self,
        *,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
    ) -> FulfillmentAttempt:
        if entry is None or not entry.provider_keys:
            # 路由当时无 provider 能服务该 need：不 fetch（spec M）。
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.PROVIDER_UNAVAILABLE,
                error_code=FulfillmentErrorCode.PROVIDER_UNAVAILABLE,
            )
        fin_need = next(
            (item for item in context.payload.financial_needs if item.need_code == need.need_code),
            None,
        )
        if fin_need is None:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNSUPPORTED,
                error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
            )
        return await self._fulfill_calculation(context, need, entry, fin_need)

    # ------------------------------------------------------------ 计算

    async def _fulfill_calculation(self, context, need, entry, fin_need) -> FulfillmentAttempt:
        try:
            code = CalculationCode(fin_need.calculation_code.value)
        except ValueError:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNSUPPORTED,
                error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
            )
        by_metric = await self._load_observations_by_metric(
            context.company_id,
            context.analysis_as_of,
            _question_sha(context.research_question),
        )
        try:
            inputs = self._resolve_inputs(code, fin_need, by_metric)
        except _FinancialError as exc:
            if exc.kind == _FinancialError.MISSING_OBSERVATION:
                # F1：底层 observation 缺失 → 先尝试自动财务提取（年报 parsed
                # blocks → 指标 → 证据卡 + observation）；成功 → 重查重试一次。
                if self._extraction is not None:
                    await self._try_auto_extract(context)
                    by_metric = await self._load_observations_by_metric(
                        context.company_id,
                        context.analysis_as_of,
                        _question_sha(context.research_question),
                    )
                    try:
                        inputs = self._resolve_inputs(code, fin_need, by_metric)
                    except _FinancialError:
                        return self._attempt(
                            need,
                            entry,
                            FulfillmentStatus.UNRESOLVED,
                            error_code=FulfillmentErrorCode.MISSING_UNDERLYING_OBSERVATION,
                        )
                else:
                    return self._attempt(
                        need,
                        entry,
                        FulfillmentStatus.UNRESOLVED,
                        error_code=FulfillmentErrorCode.MISSING_UNDERLYING_OBSERVATION,
                    )
            else:
                return self._attempt(
                    need,
                    entry,
                    FulfillmentStatus.UNRESOLVED,
                    error_code=FulfillmentErrorCode.OBSERVATION_INSUFFICIENT,
                )

        draft = FinancialCalculationDraft(
            company_id=context.company_id,
            calculation_code=code,
            input_observation_ids=inputs,
        )
        try:
            result = await FinancialCalculationService(self._sessionmaker).create_calculation(draft)
        except FinancialCalculationError:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.CALCULATION_INPUT_ERROR,
            )
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value,
            status=FulfillmentStatus.RESOLVED,
            created_artifact_ids=[result.calculation_id] if not result.replayed else [],
            existing_artifact_ids=[result.calculation_id] if result.replayed else [],
        )

    # ------------------------------------------------------------ inputs

    def _resolve_inputs(self, code: CalculationCode, fin_need, by_metric) -> dict[InputRole, UUID]:
        """按 roles 匹配 Observation → {role: obs_id}；任一缺失 → MISSING。

        选择策略（确定性）：
        - fixed role：`expected_metric_code(role)`；margin/ratio 需 period 匹配
          （need.period 缺省时取最新）；
        - growth current/baseline：`fin_need.metric_code`；CURRENT 取 period 年
          （缺省最新），BASELINE 取 current 前一年且优先同 period_kind。
        - 所有 input 优先同 statement_scope（consolidated），不强制。
        """
        roles = calculation_input_roles(code)
        year = int(fin_need.period) if fin_need.period else None
        if code in _GROWTH_CALCULATION_CODES:
            return self._resolve_growth(fin_need, by_metric, year)

        inputs: dict[InputRole, UUID] = {}
        scope: str | None = None
        for role in roles:
            expected = expected_metric_code(role)
            metric = expected.value
            obs = self._pick(
                by_metric.get(metric, []),
                year=year,
                statement_scope=scope,
            )
            if obs is None:
                raise _FinancialError(_FinancialError.MISSING_OBSERVATION)
            scope = scope or obs.statement_scope
            inputs[role] = obs.metric_observation_id
        return inputs

    def _resolve_growth(self, fin_need, by_metric, year: int | None) -> dict[InputRole, UUID]:
        metric = fin_need.metric_code.value  # growth 必填（contracts 已强制）
        candidates = by_metric.get(metric, [])
        current = self._pick(candidates, year=year)
        if current is None:
            raise _FinancialError(_FinancialError.MISSING_OBSERVATION)
        baseline_year = current.period_end.year - 1
        baseline = self._pick(
            candidates,
            year=baseline_year,
            period_kind=current.period_kind,
            statement_scope=current.statement_scope,
            # 同期间长度优先（2026H1 对 2025H1，而非 2025 全年）
            same_period_end=(current.period_end.month, current.period_end.day),
        )
        if baseline is None:
            raise _FinancialError(_FinancialError.MISSING_OBSERVATION)
        return {
            InputRole.CURRENT: current.metric_observation_id,
            InputRole.BASELINE: baseline.metric_observation_id,
        }

    @staticmethod
    def _pick(
        candidates: list[FinancialMetricObservationModel],
        *,
        year: int | None,
        period_kind: str | None = None,
        statement_scope: str | None = None,
        same_period_end: tuple[int, int] | None = None,
    ) -> FinancialMetricObservationModel | None:
        """确定性挑选：year 必配（不满足 → None）；period_kind / scope 软约束。

        candidates 按 (period_end, id) 升序 → 取池内最新。
        """
        if not candidates:
            return None
        pool = candidates
        if year is not None:
            by_year = [obs for obs in pool if obs.period_end.year == year]
            if not by_year:
                return None
            pool = by_year
        if period_kind is not None:
            same_kind = [obs for obs in pool if obs.period_kind == period_kind]
            if same_kind:
                pool = same_kind
        if statement_scope is not None:
            same_scope = [obs for obs in pool if obs.statement_scope == statement_scope]
            if same_scope:
                pool = same_scope
        if same_period_end is not None:
            month, day = same_period_end
            same_end = [
                obs for obs in pool if (obs.period_end.month, obs.period_end.day) == (month, day)
            ]
            if same_end:
                pool = same_end
        return pool[-1]

    # ------------------------------------------------------------ F1 自动提取

    async def _try_auto_extract(self, context) -> None:
        """从公司 eligible 年报自动提取财务指标（0 LLM；失败静默 → 原语义）。

        - 只处理已 parse 的 annual_report source（no-lookahead 由 preparation
          的 eligibility 语义承担——使用 analysis_as_of 之前的报告）；
        - 每条 source：provider.extract → provenance 校验 → 证据卡 +
          observation 落库（全部幂等）；单条失败不阻塞其它。
        """
        if self._extraction_service is None or self._provider is None:
            return
        sources = await self._eligible_annual_reports(context)
        for source in sources:
            parsed = await self._parsed_source(source.source_id)
            if parsed is None or source.reporting_period_end is None:
                continue
            try:
                request = FinancialExtractionRequest(
                    company_id=context.company_id,
                    parsed_source_id=parsed.parsed_source_id,
                    reporting_period_end=source.reporting_period_end,
                )
                result = await self._extraction_service.extract(request)
            except Exception:  # noqa: BLE001 - 单条 source 提取失败 → 下一个
                continue
            if result.accepted_count == 0:
                continue
            try:
                await self._extraction.ingest(
                    research_question=context.research_question,
                    source_id=source.source_id,
                    extraction=result,
                )
            except Exception:  # noqa: BLE001 - 落库失败不阻塞
                continue

    async def _eligible_annual_reports(self, context) -> list[SourceRecordModel]:
        """公司 eligible 年报（document_type=annual_report；no-lookahead）。"""
        from app.claims.macro_policy import resolve_availability
        from app.evidence.contracts import EvidenceOrigin

        stmt = select(SourceRecordModel).where(
            SourceRecordModel.company_id == context.company_id,
            SourceRecordModel.document_type.in_(
                ("annual_report", "semiannual_report", "quarterly_report")
            ),
        )
        async with self._sessionmaker() as session:
            rows = await session.execute(stmt)
            sources = list(rows.scalars().all())
        eligible = []
        for source in sources:
            availability = resolve_availability(
                origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
                snapshot_fetched_at=None,
                source_published_at=source.published_at,
                source_acquired_at=source.acquired_at,
            )
            if availability is not None and availability.date() <= context.analysis_as_of:
                eligible.append(source)
        return eligible

    async def _parsed_source(self, source_id: UUID) -> ParsedSourceModel | None:
        async with self._sessionmaker() as session:
            return (
                (
                    await session.execute(
                        select(ParsedSourceModel).where(ParsedSourceModel.source_id == source_id)
                    )
                )
                .scalars()
                .first()
            )

    # ------------------------------------------------------------ 数据

    async def _load_observations_by_metric(
        self,
        company_id: UUID,
        analysis_as_of: date | None = None,
        research_question_sha256: str | None = None,
    ) -> dict[str, list[FinancialMetricObservationModel]]:
        stmt = select(FinancialMetricObservationModel).where(
            FinancialMetricObservationModel.company_id == company_id
        )
        async with self._sessionmaker() as session:
            rows = await session.execute(stmt)
            observations = list(rows.scalars().all())
            # P0 isolation：只保留 availability <= analysis_as_of 的观测。
            if analysis_as_of is not None:
                from app.financial.availability import filter_observations_eligible

                observations = await filter_observations_eligible(
                    session, observations, analysis_as_of
                )
        by_metric: dict[str, list[FinancialMetricObservationModel]] = {}
        for obs in observations:
            by_metric.setdefault(obs.metric_code, []).append(obs)
        for metric_list in by_metric.values():
            metric_list.sort(key=lambda obs: (obs.period_end, obs.metric_observation_id))
        return by_metric

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
