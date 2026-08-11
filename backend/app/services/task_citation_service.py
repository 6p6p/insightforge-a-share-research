"""Task-scoped citation navigation service (Stage 6B.2 spec J/K/L/M): read-only.

Report → click citation → Evidence → Claim relation → Source / locator，以及
Claim citation → evidence relation list。**只读**：不创建 / 不修改任何
Evidence / Claim / Report / Audit。

**task scope（spec J）**：给定 task_id + evidence_card_id / claim_id，首先从
`TaskArtifactService` canonical lineage 得到 allowed evidence / claim IDs；
不属于当前任务 → `CitationNotFound`（404，**不暴露跨 task 存在性**）。**不能**
仅凭任意 UUID 直接读取全库 Evidence / Claim。

**provenance integrity（spec M）**：Evidence 的 Document / Macro 全链经
`EvidenceProvenanceService.resolve` 逐 hop 校验（Chunk → ParsedSource →
SourceRecord → RawArtifact，或 Observation → Snapshot → Series → Provider +
artifact links → RawArtifact）；任一缺失 / tamper →
`EvidenceProvenanceIntegrityError` → 映射为 `TaskArtifactIntegrityError`（409）。
**不 repair**。

relation 保留 supports / contradicts / context（spec L：不能压成单一 relation）；
同一 Evidence 对多个 canonical Claim 的关系全部返回（spec R-5，不丢）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import CitationNotFound, TaskArtifactIntegrityError
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.evidence.errors import EvidenceProvenanceIntegrityError
from app.evidence.provenance_service import EvidenceProvenanceService
from app.schemas.citation import (
    ClaimCitationEvidenceRelation,
    ClaimCitationResponse,
    EvidenceCitationClaimRelation,
    EvidenceCitationPayload,
    EvidenceCitationResponse,
)
from app.services.task_artifact_service import TaskArtifactService
from app.synthesis.contracts import VerifiedSynthesisClaim


class TaskCitationService:
    """任务级 citation navigation（read-only，0 LLM / 0 network）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        task_artifact_service: TaskArtifactService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._task_artifact_service = task_artifact_service

    async def get_evidence_citation(
        self,
        task_id: UUID,
        evidence_card_id: UUID,
    ) -> EvidenceCitationResponse:
        """Evidence citation：evidence 头部 + canonical Claim relations + verified provenance。

        1. task-scope 判定（spec J）：evidence 不属于当前 task → 404；
        2. 加载 EvidenceCard 行（在 scope 内但行缺失 = 数据被篡改，spec M）；
        3. claim_relations：本 evidence 对 canonical synthesis input Claims 的
           relation（claim_id + statement + relation）；
        4. provenance：Document / Macro verified 链，任一 hop 断裂 → 409。
        """
        scope = await self._task_artifact_service.resolve_evidence_scope(task_id)
        if evidence_card_id not in scope:
            raise CitationNotFound()
        claim_ids = await self._task_artifact_service.resolve_claim_scope(task_id)

        async with self._sessionmaker() as session:
            card = (
                await session.execute(
                    select(EvidenceCardModel).where(
                        EvidenceCardModel.evidence_card_id == evidence_card_id
                    )
                )
            ).scalar_one_or_none()
            if card is None:
                raise TaskArtifactIntegrityError()
            claim_relations = await self._load_claim_relations(session, card, claim_ids)
            try:
                provenance = await EvidenceProvenanceService.resolve(session, card)
            except EvidenceProvenanceIntegrityError:
                raise TaskArtifactIntegrityError() from None

        return EvidenceCitationResponse(
            evidence=EvidenceCitationPayload(
                evidence_card_id=card.evidence_card_id,
                statement=card.evidence_statement,
                quote_text=card.quote_text,
                evidence_type=card.evidence_type,
                origin_type=card.origin_type,
            ),
            claim_relations=claim_relations,
            provenance=provenance,
        )

    async def get_claim_citation(
        self,
        task_id: UUID,
        claim_id: UUID,
    ) -> ClaimCitationResponse:
        """Claim citation：canonical synthesis input claim → evidence relation list。

        只允许 canonical synthesis input claim（spec L）；claim 元数据来自
        verified claim（domain / kind / confidence / importance 取 StrEnum
        `.value`）；evidence_relations 只投影该 claim 的 exact evidence
        `evidence_card_ids`，relation 保留 supports / contradicts / context。
        """
        claim_ids = await self._task_artifact_service.resolve_claim_scope(task_id)
        if claim_id not in claim_ids:
            raise CitationNotFound()
        verified_claims = await self._task_artifact_service.resolve_verified_claims(task_id)
        claim = next((c for c in verified_claims if c.claim_id == claim_id), None)
        if claim is None:
            raise CitationNotFound()

        async with self._sessionmaker() as session:
            evidence_relations = await self._load_evidence_relations(session, claim)

        return ClaimCitationResponse(
            claim_id=claim.claim_id,
            statement=claim.statement,
            domain=claim.analysis_domain.value,
            kind=claim.claim_kind.value,
            confidence=claim.confidence.value,
            importance=claim.importance.value,
            evidence_relations=evidence_relations,
        )

    # ------------------------------------------------------------------ 内部

    async def _load_claim_relations(
        self,
        session,
        card: EvidenceCardModel,
        claim_ids: set[UUID],
    ) -> list[EvidenceCitationClaimRelation]:
        """evidence → canonical Claim relations（claim_id + statement + relation）。

        只投影当前任务 canonical synthesis input Claims（spec J：不混入旧
        synthesis / 其他任务）。同一 evidence 对多个 claim 的 relations 全量
        返回（spec R-5）。
        """
        if not claim_ids:
            return []
        result = await session.execute(
            select(
                ClaimEvidenceLinkModel.claim_id,
                ClaimEvidenceLinkModel.relation,
            ).where(
                ClaimEvidenceLinkModel.evidence_card_id == card.evidence_card_id,
                ClaimEvidenceLinkModel.claim_id.in_(sorted(claim_ids, key=str)),
            )
        )
        links = [(claim_id, relation) for claim_id, relation in result.all()]
        if not links:
            return []
        related_claim_ids = {claim_id for claim_id, _ in links}
        claim_rows = await session.execute(
            select(ClaimModel.claim_id, ClaimModel.statement).where(
                ClaimModel.claim_id.in_(sorted(related_claim_ids, key=str))
            )
        )
        statement_by_claim = {claim_id: statement for claim_id, statement in claim_rows.all()}
        relations = []
        for claim_id, relation in sorted(links, key=lambda item: (str(item[0]), item[1])):
            relations.append(
                EvidenceCitationClaimRelation(
                    claim_id=claim_id,
                    claim_statement=statement_by_claim.get(claim_id, ""),
                    relation=relation,
                )
            )
        return relations

    async def _load_evidence_relations(
        self,
        session,
        claim: VerifiedSynthesisClaim,
    ) -> list[ClaimCitationEvidenceRelation]:
        """claim → evidence relations（evidence_card_id + statement + relation）。

        只投影该 canonical claim 的 exact input evidence（`claim.evidence_card_ids`），
        relation 保留 supports / contradicts / context（spec L 不压平）。
        """
        card_ids = list(claim.evidence_card_ids)
        if not card_ids:
            return []
        result = await session.execute(
            select(
                ClaimEvidenceLinkModel.evidence_card_id,
                ClaimEvidenceLinkModel.relation,
            ).where(
                ClaimEvidenceLinkModel.claim_id == claim.claim_id,
                ClaimEvidenceLinkModel.evidence_card_id.in_(sorted(card_ids, key=str)),
            )
        )
        links = [(card_id, relation) for card_id, relation in result.all()]
        if not links:
            return []
        related_card_ids = {card_id for card_id, _ in links}
        card_rows = await session.execute(
            select(
                EvidenceCardModel.evidence_card_id,
                EvidenceCardModel.evidence_statement,
            ).where(EvidenceCardModel.evidence_card_id.in_(sorted(related_card_ids, key=str)))
        )
        statement_by_card = {card_id: statement for card_id, statement in card_rows.all()}
        relations = []
        for card_id, relation in sorted(links, key=lambda item: (str(item[0]), item[1])):
            relations.append(
                ClaimCitationEvidenceRelation(
                    evidence_card_id=card_id,
                    evidence_statement=statement_by_card.get(card_id, ""),
                    relation=relation,
                )
            )
        return relations
