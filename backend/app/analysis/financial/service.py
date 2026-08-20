"""Structured financial analysis service (stage 4B.2C.2): Calculation Pack → LLM → Claim。

流程（10 步）：
1. 防御性 request 校验（构造已校验，服务层再兜底）；
2. 短 DB session：加载全部 Calculation（FinancialCalculationService.
   verify_calculation_integrity 逐条校验：缺失 → CalculationNotFound、company
   != request → CompanyMismatch、重放损坏 → Corrupted）+ 加载 inputs →
   Observations（company 一致）+ 加载 additional Evidence（存在 + company 一致）；
3. 关闭 DB session（**LLM 调用期间不持有 DB transaction / connection**）；
4. 构造 C/E alias（Calculation Pack + Evidence Pack）；
5. 调 FinancialAnalysisModel.analyze → FinancialAnalysisDecision（provider 失败
   → ModelUnavailable；输出无法解析 → MalformedOutput）；
6. 防御性 double-check（模型可能返回 raw dict，再做一次 schema 校验）；
7. numeric-literal guard（任一 Claim statement 含数字/百分比 →
   FinancialAnalysisNumericLiteralForbidden，整次失败 0 写；**不自动删数字 /
   不改写 / 不让第二个 LLM 修正**）；
8. C/E ref resolution（未知 C/E → UnknownRef；跨 relation → RelationConflict；
   全部 candidate 先完成，任一失败 → 整次 0 写）；
9. 构造全部 FinancialClaimDraft（v3；固定 analysis_domain=financial、
   analyst_name=FINANCIAL_ANALYST_NAME、analyst_version=1、
   analyst_model_id=model.model_id）+ claim_kind policy；
10. FinancialClaimService.create_claim_batch（1..3 drafts，单 transaction）→
    FinancialAnalysisResult（relevant / claim_ids ordered / created_count /
    replayed_count / reason_code）。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph 分析节点；
不调用 Retrieval / Chroma / RawArtifact / tools / web search。Financial Analyst
不计算任何财务指标、不修改公式结果、不做宏观因果 / 估值。
"""

from dataclasses import replace
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.contracts import EvidencePack
from app.analysis.claims.evidence_pack import EvidencePackSource
from app.analysis.financial.contracts import (
    _ALLOWED_KINDS_FINANCIAL_ANALYST,
    FINANCIAL_ANALYST_FOCUS,
    FINANCIAL_ANALYST_NAME,
    FINANCIAL_ANALYST_VERSION,
    CalculationPack,
    FinancialAnalysisContext,
    FinancialAnalysisDecision,
    FinancialAnalysisModel,
    FinancialAnalysisReason,
    FinancialAnalysisRequest,
    FinancialAnalysisResult,
)
from app.analysis.financial.errors import (
    FinancialAnalysisCalculationCompanyMismatch,
    FinancialAnalysisCalculationCorrupted,
    FinancialAnalysisCalculationNotFound,
    FinancialAnalysisClaimKindPolicy,
    FinancialAnalysisError,
    FinancialAnalysisEvidenceCompanyMismatch,
    FinancialAnalysisInputError,
    FinancialAnalysisMalformedOutput,
    FinancialAnalysisNumericLiteralForbidden,
)
from app.analysis.financial.packs import (
    CalculationPackSource,
    InputSummarySource,
    ResolvedFinancialClaim,
    assert_statement_has_no_numeric_literals,
    build_calculation_pack,
    build_evidence_pack_allowing_empty,
    resolve_decision_refs,
)
from app.analysis.financial.prompt import NUMERIC_REPAIR_HINT
from app.claims.financial_contracts import (
    FinancialClaimDraft,
    FinancialClaimImportance,
)
from app.core.logging import get_logger
from app.db.models.evidence_card import EvidenceCardModel
from app.financial.calculations.errors import FinancialCalculationError
from app.financial.calculations.service import FinancialCalculationService
from app.repositories.financial_calculation_input_repository import (
    FinancialCalculationInputRepository,
)
from app.repositories.financial_metric_observation_repository import (
    FinancialMetricObservationRepository,
)
from app.services.financial_claim_service import FinancialClaimService


def _question_sha(research_question: str) -> str:
    """研究问题 -> sha256（与 evidence card 同一函数）。"""
    from app.claims.contracts import compute_research_question_sha256

    return compute_research_question_sha256(research_question)


def _card_question_match(card: EvidenceCardModel, question_sha: str) -> bool:
    """task-level 隔离：evidence card 必须属于当前研究问题。"""
    return card.research_question_sha256 == question_sha


class FinancialAnalysisService:
    def __init__(self, sessionmaker: async_sessionmaker, model: FinancialAnalysisModel) -> None:
        self._sessionmaker = sessionmaker
        self._model = model
        self._logger = get_logger("app.financial_analysis")

    async def analyze(self, request: FinancialAnalysisRequest) -> FinancialAnalysisResult:
        # 1. 防御性 request 校验（构造已校验，服务层再兜底）。
        self._check_request(request)

        # 2. 短 DB session：加载并校验全部 Calculation + additional Evidence
        #    （任一 missing / company mismatch / corruption → 稳定错误，不调用 LLM）。
        calculation_sources = await self._load_calculation_sources(request)
        evidence_sources = await self._load_evidence_sources(request)

        # P0 eligibility：时态隔离可能令请求的全部 calculation 不可用
        # （观测均为未来/跨任务）→ 无任何可用的计算输入。此时**不调用 LLM**，
        # 确定性降级为 0-claims 非相关结果（诚实 no-data，不崩溃、不引入
        # 无来源数字）；缺失/损坏/跨公司的 calc 仍在上层 raise（数据完整性）。
        if request.calculation_ids and not calculation_sources:
            self._logger.warning(
                "financial_analysis_temporal_degrade",
                reason="no_eligible_calculation_after_temporal_isolation",
            )
            return FinancialAnalysisResult(
                relevant=False,
                claim_ids=[],
                created_count=0,
                replayed_count=0,
                reason_code=FinancialAnalysisReason.INSUFFICIENT_CALCULATIONS,
            )


        # 3. DB session 已关闭（上面的 context manager 退出）；构造 C/E alias。
        calculation_pack = build_calculation_pack(calculation_sources)
        evidence_pack = build_evidence_pack_allowing_empty(evidence_sources)

        # 4-7. 调模型（结构化决策；LLM 调用期间不持有 DB transaction）。
        # Part 1 Hardening：numeric-literal 违规 → **自动修复**（带 correction
        # hint 重新生成，最多 3 次）→ 仍失败 → **降级为 0-claims 定性结果**
        # （不阻断 Stage4；不引入无来源数字；warning 记录）。模型瞬时错误
        # （ModelUnavailable）仍走 5 次有界重试。
        context = FinancialAnalysisContext(
            research_question=request.research_question,
            strategy=FINANCIAL_ANALYST_FOCUS,
        )
        decision = None
        correction_hint: str | None = None
        numeric_repairs = 0
        for attempt in range(5):
            try:
                decision = await self._call_model(
                    context,
                    calculation_pack,
                    evidence_pack,
                    correction_hint=correction_hint,
                )
                # 6. relevant=false → 0-claims 结果（不写任何 Claim）。
                if not decision.relevant:
                    return FinancialAnalysisResult(
                        relevant=False,
                        claim_ids=[],
                        created_count=0,
                        replayed_count=0,
                        reason_code=decision.reason_code,
                    )
                # 7. numeric-literal guard（任一 Claim 含数字/百分比 → 进入
                #    repair flow，带 hint 重新生成；guard 本身不删数字/不改写）。
                for candidate in decision.claims:
                    assert_statement_has_no_numeric_literals(candidate.statement)
                break
            except FinancialAnalysisNumericLiteralForbidden as exc:
                numeric_repairs += 1
                if numeric_repairs <= 3:
                    correction_hint = NUMERIC_REPAIR_HINT
                    self._logger.warning(
                        "financial_analysis_numeric_repair",
                        attempt=attempt,
                        repair_round=numeric_repairs,
                        reason=str(exc)[:200],
                    )
                    decision = None
                    continue
                # repair 3 次仍失败 → 降级：0-claims 定性结果（无数字可进报告），
                # 不阻断 Stage4（synthesis 继续）。
                self._logger.warning(
                    "financial_analysis_numeric_downgraded",
                    reason_code=FinancialAnalysisReason.NUMERIC_REFERENCE_DOWNGRADED.value,
                )
                return FinancialAnalysisResult(
                    relevant=False,
                    claim_ids=[],
                    created_count=0,
                    replayed_count=0,
                    reason_code=FinancialAnalysisReason.NUMERIC_REFERENCE_DOWNGRADED,
                )
            except FinancialAnalysisError as exc:
                if attempt < 4:
                    self._logger.warning(
                        "financial_analysis_model_retry",
                        attempt=attempt,
                        error_type=type(exc).__name__,
                    )
                    decision = None
                    continue
                raise

        # 8. C/E ref resolution（全部 candidate 先完成，任一失败 → 整次 0 写）。
        resolved = resolve_decision_refs(decision, calculation_pack, evidence_pack)

        # 8.5 确定性 importance 降级（Final Autonomous Research）：critical
        #     claim 的 supports Calculation 的 source Evidence 与 additional
        #    supports 全部 non-eligible（如 Tier-3 自动获取来源）→ 降级 normal
        #    （诚实反映来源 tier，不因来源级别让研究失败；绝不提升 authority）。
        if any(claim.importance == FinancialClaimImportance.CRITICAL for claim in resolved):
            calc_ids = {calc_id for claim in resolved for calc_id in claim.supports_calculations}
            eligible_by_calc = await self._load_calculation_eligibility(request, calc_ids)
            resolved = self._downgrade_importance(resolved, eligible_by_calc, evidence_sources)

        # 9. 构造全部 FinancialClaimDraft(v3) + claim_kind policy。
        drafts = self._build_drafts(request, resolved)
        self._check_kind_policy(drafts)

        # 10. 原子持久化（create_claim_batch：全部 draft 先 validate，单 transaction）。
        batch = await FinancialClaimService(self._sessionmaker).create_claim_batch(drafts)
        return FinancialAnalysisResult(
            relevant=True,
            claim_ids=list(batch.claim_ids),
            created_count=len(batch.created),
            replayed_count=len(batch.replayed),
            reason_code=None,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: FinancialAnalysisRequest) -> None:
        # 构造时已做校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if not request.research_question.strip() or not request.calculation_ids:
            raise FinancialAnalysisInputError("invalid financial analysis request")

    async def _load_calculation_sources(
        self, request: FinancialAnalysisRequest
    ) -> list[CalculationPackSource]:
        """短 DB session 加载并校验全部 Calculation（不调用 LLM 时先验证上游）。

        每个 Calculation 走 FinancialCalculationService.verify_calculation_integrity
        （missing → None、重放损坏 → FinancialCalculationError）；company !=
        request → CompanyMismatch；再加载 inputs → Observations（company 一致）。
        """
        async with self._sessionmaker() as session:
            calc_svc = FinancialCalculationService(self._sessionmaker)
            input_repo = FinancialCalculationInputRepository(session)
            obs_repo = FinancialMetricObservationRepository(session)
            sources: list[CalculationPackSource] = []
            for calc_id in request.calculation_ids:
                try:
                    calc = await calc_svc.verify_calculation_integrity(session, calc_id)
                except FinancialCalculationError as exc:
                    raise FinancialAnalysisCalculationCorrupted() from exc
                if calc is None:
                    raise FinancialAnalysisCalculationNotFound()
                if calc.company_id != request.company_id:
                    raise FinancialAnalysisCalculationCompanyMismatch()
                rows = await input_repo.get_by_calculation_id(calc_id)
                obs_by_role: list[tuple[str, object]] = []
                for row in rows:
                    obs = await obs_repo.get_by_id(row.metric_observation_id)
                    if obs is None or obs.company_id != request.company_id:
                        raise FinancialAnalysisCalculationCorrupted(
                            "financial analysis calculation input observation corrupted"
                        )
                    obs_by_role.append((row.input_role, obs))
                # P0 isolation：analysis_as_of 已声明时，只允许 availability
                # <= as_of 且属于当前研究问题的观测进入上下文；任一 input 为
                # 未来/跨任务观测 → 该 calculation 对本任务不可用（跳过）。
                if request.analysis_as_of is not None:
                    from app.financial.availability import filter_observations_for_task

                    question_sha = _question_sha(request.research_question)
                    eligible_obs = await filter_observations_for_task(
                        session,
                        [o for _, o in obs_by_role],
                        request.analysis_as_of,
                        question_sha,
                    )
                    if len(eligible_obs) != len(obs_by_role):
                        continue
                inputs = [InputSummarySource.from_model(role, obs) for role, obs in obs_by_role]
                sources.append(CalculationPackSource.from_model(calc, inputs))
            return sources

    async def _load_evidence_sources(
        self, request: FinancialAnalysisRequest
    ) -> list[EvidencePackSource]:
        """加载 additional Evidence（document_chunk / macro_observation 皆可）；
        缺失 / 跨公司 → CompanyMismatch。空 → []。

        P0 isolation：analysis_as_of 已声明时排除 availability > as_of 的卡；
        task-level 隔离：evidence card 必须属于当前研究问题。
        """
        if not request.additional_evidence_ids:
            return []
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(EvidenceCardModel).where(
                    EvidenceCardModel.evidence_card_id.in_(request.additional_evidence_ids)
                )
            )
            rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(request.additional_evidence_ids):
            raise FinancialAnalysisEvidenceCompanyMismatch()
        for card in by_id.values():
            if card.company_id != request.company_id:
                raise FinancialAnalysisEvidenceCompanyMismatch()
        ordered: list[EvidenceCardModel] = [by_id[cid] for cid in request.additional_evidence_ids]
        if request.analysis_as_of is not None:
            from app.claims.macro_policy import resolve_availability as _resolve_avail
            from app.db.models.source_record import SourceRecordModel

            async with self._sessionmaker() as session:
                src_rows = (
                    (
                        await session.execute(
                            select(SourceRecordModel).where(
                                SourceRecordModel.source_id.in_(
                                    {c.source_id for c in ordered if c.source_id is not None}
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            src_by_card = {src.source_id: src for src in src_rows}
            eligible: list[EvidenceCardModel] = []
            for c in ordered:
                src = src_by_card.get(c.source_id)
                avail = _resolve_avail(
                    origin_type=c.origin_type,
                    snapshot_fetched_at=None,
                    source_published_at=src.published_at if src else None,
                    source_acquired_at=src.acquired_at if src else None,
                )
                if avail is not None and avail.date() <= request.analysis_as_of:
                    eligible.append(c)
            return [EvidencePackSource.from_model(c) for c in eligible]
        return [
            EvidencePackSource.from_model(by_id[card_id])
            for card_id in request.additional_evidence_ids
        ]

    async def _call_model(
        self,
        context: FinancialAnalysisContext,
        calculation_pack: CalculationPack,
        evidence_pack: EvidencePack,
        correction_hint: str | None = None,
    ) -> FinancialAnalysisDecision:
        """调用模型并归一到 FinancialAnalysisDecision（防御性 double-check）。

        模型层负责解析；这里再对返回结果做一次 schema 校验（provider 可能
        返回 raw dict / 已构造对象），ValidationError → MalformedOutput。
        correction_hint（Part 1 repair flow）：上一轮违规的修复指令。
        """
        raw = await self._model.analyze(
            context,
            calculation_pack,
            evidence_pack,
            correction_hint=correction_hint,
        )
        if isinstance(raw, FinancialAnalysisDecision):
            return raw
        try:
            return FinancialAnalysisDecision.model_validate(raw)
        except ValidationError as exc:
            raise FinancialAnalysisMalformedOutput() from exc

    def _build_drafts(
        self,
        request: FinancialAnalysisRequest,
        resolved: list[ResolvedFinancialClaim],
    ) -> list[FinancialClaimDraft]:
        """把解析后的 Claim 候选构造为 FinancialClaimDraft（v3；analyst 身份固定）。

        FinancialClaimDraft 构造时已做去重 + canonical 排序（幂等）。
        """
        drafts: list[FinancialClaimDraft] = []
        for claim in resolved:
            drafts.append(
                FinancialClaimDraft(
                    company_id=request.company_id,
                    research_question=request.research_question,
                    statement=claim.statement,
                    confidence=claim.confidence,
                    importance=claim.importance,
                    claim_kind=claim.claim_kind,
                    support_calculation_ids=list(claim.supports_calculations),
                    contradict_calculation_ids=list(claim.contradicts_calculations),
                    context_calculation_ids=list(claim.context_calculations),
                    additional_support_evidence_ids=list(claim.additional_supports),
                    additional_contradict_evidence_ids=list(claim.additional_contradicts),
                    additional_context_evidence_ids=list(claim.additional_context),
                    analyst_name=FINANCIAL_ANALYST_NAME,
                    analyst_version=FINANCIAL_ANALYST_VERSION,
                    analyst_model_id=self._model.model_id,
                )
            )
        return drafts

    async def _load_calculation_eligibility(
        self, request: FinancialAnalysisRequest, calc_ids: set
    ) -> dict[UUID, bool]:
        """calc_id -> 其 source Evidence 是否含 critical_claim_eligible。

        加载 calc inputs → Observations → source evidence cards 的 eligible
        快照（与 FinancialClaimService 的 critical policy 同一语义）。
        """
        if not calc_ids:
            return {}
        from app.repositories.financial_calculation_input_repository import (
            FinancialCalculationInputRepository,
        )
        from app.repositories.financial_metric_observation_repository import (
            FinancialMetricObservationRepository,
        )

        async with self._sessionmaker() as session:
            input_repo = FinancialCalculationInputRepository(session)
            obs_repo = FinancialMetricObservationRepository(session)
            evidence_by_calc: dict[UUID, set[UUID]] = {}
            all_cards: set[UUID] = set()
            for calc_id in calc_ids:
                rows = await input_repo.get_by_calculation_id(calc_id)
                per_calc: set[UUID] = set()
                for row in rows:
                    obs = await obs_repo.get_by_id(row.metric_observation_id)
                    if obs is not None and obs.company_id == request.company_id:
                        per_calc.add(obs.source_evidence_card_id)
                evidence_by_calc[calc_id] = per_calc
                all_cards |= per_calc
            eligible: set[UUID] = set()
            if all_cards:
                result = await session.execute(
                    select(EvidenceCardModel.evidence_card_id).where(
                        EvidenceCardModel.evidence_card_id.in_(all_cards),
                        EvidenceCardModel.critical_claim_eligible_snapshot.is_(True),
                    )
                )
                eligible = {row for row in result.scalars().all()}
        return {calc_id: bool(evidence_by_calc[calc_id] & eligible) for calc_id in calc_ids}

    def _downgrade_importance(
        self,
        resolved: list[ResolvedFinancialClaim],
        eligible_by_calc: dict[UUID, bool],
        evidence_sources: list[EvidencePackSource],
    ) -> list[ResolvedFinancialClaim]:
        """critical 无 eligible 证据 → 确定性降级 normal（不提升、不失败）。"""
        eligible_additional = {
            source.evidence_card_id for source in evidence_sources if source.critical_claim_eligible
        }
        downgraded: list[ResolvedFinancialClaim] = []
        for claim in resolved:
            if claim.importance == FinancialClaimImportance.CRITICAL:
                calc_eligible = any(
                    eligible_by_calc.get(calc_id, False) for calc_id in claim.supports_calculations
                )
                add_eligible = bool(eligible_additional & set(claim.additional_supports))
                if not calc_eligible and not add_eligible:
                    claim = replace(claim, importance=FinancialClaimImportance.NORMAL)
            downgraded.append(claim)
        return downgraded

    @staticmethod
    def _check_kind_policy(drafts: list[FinancialClaimDraft]) -> None:
        """claim_kind 防线：Financial Analyst 只允许 inference / risk。

        FinancialClaimCandidate schema 已拒绝 fact / relative_valuation；此处对
        最终 FinancialClaimDraft 再做一次兜底（即使绕过 Pydantic，fact 也会 →
        FinancialAnalysisClaimKindPolicy）。FinancialClaimDraft 本身仍支持 fact
        （更低层 domain contract，供确定性 producer 使用），本防线只作用于
        Financial Analysis 路径。
        """
        for draft in drafts:
            if draft.claim_kind not in _ALLOWED_KINDS_FINANCIAL_ANALYST:
                raise FinancialAnalysisClaimKindPolicy(
                    f"claim_kind {draft.claim_kind.value} incompatible with financial analysis"
                )
