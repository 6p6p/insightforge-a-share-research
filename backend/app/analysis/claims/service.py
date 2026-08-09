"""Structured claim analysis service (stage 4B.1): Evidence Pack → LLM → Claim。

流程：
1. 防御性 domain check（4B.1 只支持 business / event / risk）；
2. 短 DB session 加载 EvidenceCard（全部存在 + 同 request.company_id）→
   build_evidence_pack（E1..En 最小投影，**不发送** UUID / locator / raw /
   Chroma）；
3. 调 ClaimAnalysisModel.analyze → ClaimAnalysisDecision（任何模型输出无法
   通过 schema 校验 → ClaimAnalysisMalformedOutput）；
4. resolve_decision_refs：E → evidence_card_id（未知引用 / 跨 relation 冲突 →
   整次失败，0 写，不 fuzzy resolve）；
5. relevant=false → 返回 0-claims 结果（不写任何 Claim）；
6. relevant=true → 构建全部 ClaimDraft（analyst_name = 具体 strategy、
   analyst_version = CLAIM_ANALYST_VERSION、analyst_model_id = model.model_id），
   domain ↔ claim_kind 兼容性校验 → ClaimService.create_claim_batch 原子持久化
   （1..MAX_CLAIMS_PER_BATCH，all-drafts-validate-first，单 transaction）；
7. 返回 ClaimAnalysisResult。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph 分析节点；
不调用 Retrieval / Chroma / RawArtifact / tools / web search。
"""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.contracts import (
    _ALLOWED_KINDS_4B1,
    _SUPPORTED_DOMAINS_4B1,
    CLAIM_ANALYST_VERSION,
    ClaimAnalysisContext,
    ClaimAnalysisDecision,
    ClaimAnalysisModel,
    ClaimAnalysisRequest,
    ClaimAnalysisResult,
    EvidencePack,
)
from app.analysis.claims.errors import (
    ClaimAnalysisDomainKindIncompatible,
    ClaimAnalysisDomainNotReady,
    ClaimAnalysisEvidenceCompanyMismatch,
    ClaimAnalysisMalformedOutput,
)
from app.analysis.claims.evidence_pack import EvidencePackSource, build_evidence_pack
from app.analysis.claims.ref_resolver import ResolvedClaim, resolve_decision_refs
from app.analysis.claims.strategies import strategy_for_domain
from app.claims.contracts import ClaimAnalysisDomain, ClaimDraft
from app.db.models.evidence_card import EvidenceCardModel
from app.services.claim_service import ClaimService


class ClaimAnalysisService:
    def __init__(self, sessionmaker: async_sessionmaker, model: ClaimAnalysisModel) -> None:
        self._sessionmaker = sessionmaker
        self._model = model

    async def analyze(self, request: ClaimAnalysisRequest) -> ClaimAnalysisResult:
        # 1. 防御性 domain check（请求构造已校验，服务层再兜底）。
        self._check_domain(request.analysis_domain)

        # 2. Evidence Pack：真实 PG 加载 + 确定性 E1..En alias。
        sources = await self._load_evidence_sources(request)
        pack = build_evidence_pack(sources)

        # 3. 调模型（结构化决策）。
        context = ClaimAnalysisContext(
            research_question=request.research_question,
            analysis_domain=request.analysis_domain,
            strategy=strategy_for_domain(request.analysis_domain),
        )
        decision = await self._call_model(context, pack)

        # 4. relevant=false → 0-claims 结果（不写任何 Claim）。
        if not decision.relevant:
            return ClaimAnalysisResult(
                relevant=False,
                claim_ids=[],
                created_count=0,
                replayed_count=0,
                reason_code=decision.reason_code,
            )

        # 5. ref resolution → ClaimDrafts（全部 candidate 先完成 schema + ref
        #    resolution，任一无效 → 整次失败，0 写）。
        resolved = resolve_decision_refs(decision, pack)
        drafts = self._build_drafts(request, context.strategy, resolved)
        self._check_kind_compatibility(drafts)

        # 6. 原子持久化（ClaimService 全量校验 + 单 transaction，无 partial writes）。
        batch = await ClaimService(self._sessionmaker).create_claim_batch(drafts)
        return ClaimAnalysisResult(
            relevant=True,
            claim_ids=list(batch.claim_ids),
            created_count=len(batch.created),
            replayed_count=len(batch.replayed),
            reason_code=None,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_domain(domain: ClaimAnalysisDomain) -> None:
        if domain not in _SUPPORTED_DOMAINS_4B1:
            raise ClaimAnalysisDomainNotReady()

    async def _load_evidence_sources(
        self, request: ClaimAnalysisRequest
    ) -> list[EvidencePackSource]:
        """从真实 PG 加载全部 EvidenceCard；缺失 / 跨公司 → CompanyMismatch。"""
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(EvidenceCardModel).where(
                    EvidenceCardModel.evidence_card_id.in_(request.evidence_card_ids)
                )
            )
            rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(request.evidence_card_ids):
            raise ClaimAnalysisEvidenceCompanyMismatch()
        for card in by_id.values():
            if card.company_id != request.company_id:
                raise ClaimAnalysisEvidenceCompanyMismatch()
        # 保持 request 的 canonical 顺序（build_evidence_pack 内部再按 uuid 排序）。
        return [
            EvidencePackSource.from_model(by_id[card_id]) for card_id in request.evidence_card_ids
        ]

    async def _call_model(
        self,
        context: ClaimAnalysisContext,
        pack: EvidencePack,
    ) -> ClaimAnalysisDecision:
        """调用模型并归一到 ClaimAnalysisDecision（防御性 double-check）。

        模型层负责解析；这里再对返回结果做一次 schema 校验（provider 可能
        返回 raw dict / 已构造对象），ValidationError → ClaimAnalysisMalformedOutput。
        """
        raw = await self._model.analyze(context, pack)
        if isinstance(raw, ClaimAnalysisDecision):
            return raw
        try:
            return ClaimAnalysisDecision.model_validate(raw)
        except ValidationError as exc:
            raise ClaimAnalysisMalformedOutput() from exc

    def _build_drafts(
        self,
        request: ClaimAnalysisRequest,
        strategy: str,
        resolved: list[ResolvedClaim],
    ) -> list[ClaimDraft]:
        """把解析后的 Claim 候选构造为 ClaimDraft（analyst 身份确定性派生）。

        ClaimDraft 构造时已做去重 + canonical 排序（幂等，对已排序输入无副作用）。
        """
        drafts: list[ClaimDraft] = []
        for claim in resolved:
            drafts.append(
                ClaimDraft(
                    company_id=request.company_id,
                    research_question=request.research_question,
                    statement=claim.statement,
                    analysis_domain=request.analysis_domain,
                    claim_kind=claim.claim_kind,
                    confidence=claim.confidence,
                    importance=claim.importance,
                    support_evidence_ids=list(claim.supports),
                    contradict_evidence_ids=list(claim.contradicts),
                    context_evidence_ids=list(claim.context),
                    analyst_name=strategy,
                    analyst_version=CLAIM_ANALYST_VERSION,
                    analyst_model_id=self._model.model_id,
                )
            )
        return drafts

    @staticmethod
    def _check_kind_compatibility(drafts: list[ClaimDraft]) -> None:
        """domain ↔ claim_kind 兼容性防线（4B.1 不输出 relative_valuation）。

        ClaimCandidate schema 已拒绝 relative_valuation；此处对最终 ClaimDraft
        再做一次兜底，保证任何路径都无法写入 4B.1 不支持的 claim_kind。
        """
        for draft in drafts:
            if draft.claim_kind not in _ALLOWED_KINDS_4B1:
                raise ClaimAnalysisDomainKindIncompatible(
                    f"claim_kind {draft.claim_kind.value} incompatible with analysis domain"
                )
