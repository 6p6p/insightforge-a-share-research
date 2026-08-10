"""Structured relative valuation analysis service (stage 4C.2B.2).

流程（镜像 FinancialAnalysisService 的两步提交 + 0 写失败）：
1. 防御性 request 校验（构造已校验，服务层再兜底）；
2. 短 DB session：加载全部 Comparison 并逐条 replay 校验
   （`verify_comparison_integrity`：缺失 → ComparisonNotFound、company !=
   request → ComparisonCompanyMismatch、重放损坏 → ComparisonCorrupted，不
   repair）；再复用 shared policy `check_comparison_set_consistency` 校验跨
   comparison 一致性（analysis_as_of / metric_as_of / peer set / metric 唯一性 /
   数量上限），失败映射为 InputInvalid；
3. 关闭 DB session（**LLM 调用期间不持有 DB transaction / connection**）；
4. 构造确定性 Comparison Pack（V1..Vn 按 metric_code 排序，position /
   display premium 程序生成）；
5. 调 ValuationAnalysisModel.analyze → ValuationAnalysisDecision（provider 失败
   → ModelUnavailable；输出无法解析 → MalformedOutput）；
6. relevant=false → 0 写结果（reason_code 可选）；
7. V ref resolution（未知 → UnknownRef、跨 relation → RelationConflict、遗漏
   input comparison → ComparisonOmitted——no-cherry-picking 硬边界）；
8. direction / uncertain-importance 策略（复用 shared policy）：
   relative_high 全正 / relative_low 全负 / mixed 正负都有 / uncertain→normal；
9. 确定性 statement 渲染（`render_valuation_claim_statement`，LLM 不生成
   statement）+ ValuationClaimDraft(schema v7) 构造；
10. ValuationClaimService.create_claim 原子登记（含 fingerprint replay）→
    ValuationAnalysisResult（relevant / claim_id / replayed / assessment /
    reason_code）。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph 分析节点；不调用
Retrieval / Chroma / RawArtifact / tools / web search。Analyst 不计算任何数值
（median / premium / percent）、不选择 peers、不生成 target price / fair value /
买卖建议。
"""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.valuation.contracts import (
    MAX_VALUATION_COMPARISONS_PER_REQUEST,
    VALUATION_ANALYST_FOCUS,
    VALUATION_ANALYST_NAME,
    VALUATION_ANALYST_VERSION,
    ValuationAnalysisContext,
    ValuationAnalysisDecision,
    ValuationAnalysisRequest,
    ValuationAnalysisResult,
)
from app.analysis.valuation.errors import (
    ValuationAnalysisClaimDraftError,
    ValuationAnalysisComparisonCompanyMismatch,
    ValuationAnalysisComparisonCorrupted,
    ValuationAnalysisComparisonNotFound,
    ValuationAnalysisDirectionConflict,
    ValuationAnalysisError,
    ValuationAnalysisInputError,
    ValuationAnalysisInputInvalid,
    ValuationAnalysisMalformedOutput,
    ValuationAnalysisMixedEvidenceInsufficient,
    ValuationAnalysisUncertainImportancePolicy,
)
from app.analysis.valuation.model import ValuationAnalysisModel
from app.analysis.valuation.packs import (
    ResolvedValuationDecision,
    ValuationComparisonPack,
    ValuationComparisonPackSource,
    build_valuation_comparison_pack,
    resolve_decision_refs,
)
from app.valuation.claim_contracts import (
    ValuationClaimDraft,
    render_valuation_claim_statement,
)
from app.valuation.claim_errors import ValuationClaimDraftError
from app.valuation.claim_policy import (
    ComparisonProjection,
    ValuationClaimPolicyError,
    ValuationClaimPolicyReason,
    check_assessment_direction_policy,
    check_comparison_set_consistency,
    check_uncertain_importance_policy,
)
from app.valuation.claim_service import ValuationClaimService
from app.valuation.comparison_service import (
    RelativeValuationComparisonService,
    VerifiedComparison,
)
from app.valuation.errors import ValuationError


def _policy_to_analysis_error(exc: ValuationClaimPolicyError) -> ValuationAnalysisError:
    """把 shared policy 失败映射为 ValuationAnalysisService 的稳定错误域。

    - DIRECTION_CONFLICT → ValuationAnalysisDirectionConflict；
    - MIXED_EVIDENCE_INSUFFICIENT → ValuationAnalysisMixedEvidenceInsufficient；
    - UNCERTAIN_IMPORTANCE_POLICY → ValuationAnalysisUncertainImportancePolicy；
    - 其余（ANALYSIS_DATE_MISMATCH / METRIC_DATE_MISMATCH / PEER_SET_MISMATCH /
      DUPLICATE_METRIC / TOO_MANY_COMPARISONS）→ ValuationAnalysisInputInvalid。
    """
    if exc.reason == ValuationClaimPolicyReason.DIRECTION_CONFLICT:
        return ValuationAnalysisDirectionConflict()
    if exc.reason == ValuationClaimPolicyReason.MIXED_EVIDENCE_INSUFFICIENT:
        return ValuationAnalysisMixedEvidenceInsufficient()
    if exc.reason == ValuationClaimPolicyReason.UNCERTAIN_IMPORTANCE_POLICY:
        return ValuationAnalysisUncertainImportancePolicy()
    return ValuationAnalysisInputInvalid()


class ValuationAnalysisService:
    def __init__(self, sessionmaker: async_sessionmaker, model: ValuationAnalysisModel) -> None:
        self._sessionmaker = sessionmaker
        self._model = model

    async def analyze(self, request: ValuationAnalysisRequest) -> ValuationAnalysisResult:
        # 1. 防御性 request 校验（构造已校验，服务层再兜底）。
        self._check_request(request)

        # 2. 短 DB session：加载并校验全部 Comparison（任一 missing / company
        #    mismatch / corruption / 一致性失败 → 稳定错误，不调用 LLM）。
        verified = await self._load_comparisons(request)

        # 3. DB session 已关闭；构造确定性 Comparison Pack（V1..Vn）。
        pack = build_valuation_comparison_pack(self._build_pack_sources(request, verified))

        # 4-5. 调模型（结构化决策；LLM 调用期间不持有 DB transaction）。
        context = ValuationAnalysisContext(
            research_question=request.research_question,
            analysis_as_of=request.analysis_as_of,
            strategy=VALUATION_ANALYST_FOCUS,
        )
        decision = await self._call_model(context, pack)

        # 6. relevant=false → 0 写结果（不写任何 Claim）。
        if not decision.relevant:
            return ValuationAnalysisResult(
                relevant=False,
                claim_id=None,
                replayed=False,
                assessment=None,
                reason_code=decision.reason_code,
            )

        # 7. V ref resolution（未知 / 跨 relation / 遗漏 input → 整次 0 写失败）。
        resolved = resolve_decision_refs(decision, pack)

        # 8. direction / uncertain-importance 策略（复用 shared policy，
        #    禁止复制两套规则；contradict / context 允许任意 sign）。
        self._check_policy(resolved, verified)

        # 9. 确定性 statement 渲染 + ValuationClaimDraft(v7) 构造。
        draft = self._build_draft(request, resolved)

        # 10. 原子登记（create_claim：fingerprint replay / 0 partial write）。
        result = await ValuationClaimService(self._sessionmaker).create_claim(draft)
        return ValuationAnalysisResult(
            relevant=True,
            claim_id=result.claim_id,
            replayed=result.replayed,
            assessment=resolved.assessment,
            reason_code=None,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: ValuationAnalysisRequest) -> None:
        # 构造时已做校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if (
            not request.research_question.strip()
            or not request.comparison_ids
            or len(request.comparison_ids) > MAX_VALUATION_COMPARISONS_PER_REQUEST
        ):
            raise ValuationAnalysisInputError("invalid valuation analysis request")

    async def _load_comparisons(
        self, request: ValuationAnalysisRequest
    ) -> dict[UUID, VerifiedComparison]:
        """短 DB session 加载并校验全部 Comparison（不调用 LLM 时先验证上游）。

        每个 Comparison 走 RelativeValuationComparisonService.
        verify_comparison_integrity（missing → None、重放损坏 → ValuationError
        包装为 ComparisonCorrupted）；company != request → CompanyMismatch；再复用
        shared policy 校验跨 comparison 一致性。
        """
        async with self._sessionmaker() as session:
            comparison_svc = RelativeValuationComparisonService(self._sessionmaker)
            verified: dict[UUID, VerifiedComparison] = {}
            projections: list[ComparisonProjection] = []
            for comparison_id in request.comparison_ids:
                try:
                    v = await comparison_svc.verify_comparison_integrity(session, comparison_id)
                except ValuationError as exc:
                    raise ValuationAnalysisComparisonCorrupted() from exc
                if v is None:
                    raise ValuationAnalysisComparisonNotFound()
                if v.target_company_id != request.company_id:
                    raise ValuationAnalysisComparisonCompanyMismatch()
                projections.append(
                    ComparisonProjection(
                        metric_code=v.metric_code,
                        metric_as_of=v.metric_as_of,
                        analysis_as_of=v.analysis_as_of,
                        peer_companies=frozenset(v.peer_companies),
                    )
                )
                verified[comparison_id] = v

            # 跨 comparison 一致性（复用 shared policy，禁止复制两套规则）。
            try:
                check_comparison_set_consistency(
                    expected_analysis_as_of=request.analysis_as_of,
                    comparisons=projections,
                    max_comparisons=MAX_VALUATION_COMPARISONS_PER_REQUEST,
                )
            except ValuationClaimPolicyError as exc:
                raise _policy_to_analysis_error(exc) from exc
            return verified

    def _build_pack_sources(
        self, request: ValuationAnalysisRequest, verified: dict[UUID, VerifiedComparison]
    ) -> list[ValuationComparisonPackSource]:
        """把已验证的 Comparison 投影为 Pack 来源（按 request canonical 顺序）。"""
        sources: list[ValuationComparisonPackSource] = []
        for comparison_id in request.comparison_ids:
            v = verified[comparison_id]
            sources.append(
                ValuationComparisonPackSource(
                    comparison_id=comparison_id,
                    metric_code=v.metric_code,
                    target_value=v.target_value,
                    peer_median=v.peer_median,
                    peer_min=v.peer_min,
                    peer_max=v.peer_max,
                    premium_discount_to_median=v.premium_discount_to_median,
                    peer_count=v.peer_count,
                    metric_as_of=v.metric_as_of,
                    analysis_as_of=v.analysis_as_of,
                    comparison_method=v.comparison_method,
                    formula_version=v.formula_version,
                )
            )
        return sources

    async def _call_model(
        self,
        context: ValuationAnalysisContext,
        pack: ValuationComparisonPack,
    ) -> ValuationAnalysisDecision:
        """调用模型并归一到 ValuationAnalysisDecision（防御性 double-check）。

        模型层负责解析；这里再对返回结果做一次 schema 校验（provider 可能
        返回 raw dict / 已构造对象），ValidationError → MalformedOutput。
        """
        raw = await self._model.analyze(context, pack)
        if isinstance(raw, ValuationAnalysisDecision):
            return raw
        try:
            return ValuationAnalysisDecision.model_validate(raw)
        except ValidationError as exc:
            raise ValuationAnalysisMalformedOutput() from exc

    @staticmethod
    def _check_policy(
        resolved: ResolvedValuationDecision,
        verified: dict[UUID, VerifiedComparison],
    ) -> None:
        """direction / uncertain-importance 策略（复用 shared policy，0 写失败）。

        support premiums 来自已通过 replay 校验的 persisted 派生值（Decimal）；
        contradict / context 允许任意 sign（反证 / 背景）。
        """
        support_premiums = [
            verified[comparison_id].premium_discount_to_median
            for comparison_id in resolved.support_comparison_ids
        ]
        try:
            check_assessment_direction_policy(
                assessment=resolved.assessment,
                support_premiums=support_premiums,
            )
            check_uncertain_importance_policy(
                assessment=resolved.assessment,
                importance=resolved.importance,
            )
        except ValuationClaimPolicyError as exc:
            raise _policy_to_analysis_error(exc) from exc

    def _build_draft(
        self,
        request: ValuationAnalysisRequest,
        resolved: ResolvedValuationDecision,
    ) -> ValuationClaimDraft:
        """确定性 statement 渲染 + ValuationClaimDraft(v7) 构造（LLM 不生成 statement）。

        statement 由 `render_valuation_claim_statement(assessment)` 从冻结映射
        生成，不含任何数字 / 百分比 / company / peer 名称插值；additional
        Evidence 一律为空（v1 Analyst 只做纯相对估值判断）。
        """
        try:
            statement = render_valuation_claim_statement(resolved.assessment)
        except ValuationClaimDraftError as exc:
            raise ValuationAnalysisClaimDraftError() from exc
        try:
            return ValuationClaimDraft(
                company_id=request.company_id,
                research_question=request.research_question,
                analysis_as_of=request.analysis_as_of,
                statement=statement,
                assessment=resolved.assessment,
                confidence=resolved.confidence,
                importance=resolved.importance,
                support_comparison_ids=list(resolved.support_comparison_ids),
                contradict_comparison_ids=list(resolved.contradict_comparison_ids),
                context_comparison_ids=list(resolved.context_comparison_ids),
                additional_support_evidence_ids=[],
                additional_contradict_evidence_ids=[],
                additional_context_evidence_ids=[],
                analyst_name=VALUATION_ANALYST_NAME,
                analyst_version=VALUATION_ANALYST_VERSION,
                analyst_model_id=self._model.model_id,
            )
        except ValuationClaimDraftError as exc:
            raise ValuationAnalysisClaimDraftError() from exc
