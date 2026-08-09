"""Claim service (stage 4A): structural provenance + persistence + replay.

create_claim(draft) 把"分析结论的语义输入"确定性登记为一条 Claim 及其
ClaimEvidenceLink 关系。**不调用 LLM、不接 Analyst Agent、不做语义判断**：
ClaimService 只负责结构 / provenance / 来源政策 / relation / fingerprint /
persistence / replay；statement 是否真的被 Evidence 支持由 LLM Analyst /
later Auditor 判断。

流程：
1. 短 DB session 从真实 PG 加载全部 EvidenceCard（supports ∪ contradicts ∪
   context）：任一缺失或 evidence.company_id != draft.company_id →
   ClaimEvidenceCompanyMismatch（不自动修复）。
2. 纯函数规则（不持有 DB 连接）：
   - 支持政策：≥1 supports Evidence，否则 ClaimEvidenceInsufficient；
   - critical 政策：importance=critical 时 ≥1 supports Evidence 满足
     critical_claim_eligible_snapshot=true，否则 ClaimCriticalEvidenceInsufficient
     （不因 confidence=high 放宽来源政策；不因多个 Tier-3 Evidence 自动推断）；
   - macro 传导规则：analysis_domain=macro 时 ≥1 macro_observation support
     **且** ≥1 document_chunk Evidence（supports 或 context，体现公司暴露 /
     公司经营事实），否则 MacroClaimTransmissionEvidenceInsufficient。只验证
     证据结构具备传导链材料，不判断实际因果。
3. 纯函数派生：research_question_sha256 + claim_fingerprint（canonical JSON +
   SHA-256，含按 relation 分组的 ordered evidence_card_ids；不含 claim_id /
   created_at）。
4. 短 DB transaction：create_or_get（ON CONFLICT(claim_fingerprint)，无进程锁）
   → 首次 created=True 时 bulk insert links → commit；已有 fingerprint →
   replay 时**重新加载 Claim / ClaimEvidenceLinks / EvidenceCards 并逐项核实**
   （statement / enums / company / question hash / analyst identity / link
   数量 / relations / Evidence IDs / critical rule / macro rule / fingerprint），
   任一损坏 → ClaimIntegrityError，**不自动 repair**。修改观点 = 新 Claim
   （语义 / evidence relations / confidence / analyst version 任一变化 →
   新指纹 → 新行，旧行保留）。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph 分析节点。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.claims.contracts import (
    CLAIM_SCHEMA_VERSION,
    ClaimAnalysisDomain,
    ClaimDraft,
    ClaimEvidenceRelation,
    ClaimImportance,
    compute_claim_fingerprint,
    compute_research_question_sha256,
)
from app.claims.errors import (
    ClaimCriticalEvidenceInsufficient,
    ClaimEvidenceCompanyMismatch,
    ClaimEvidenceInsufficient,
    ClaimIntegrityError,
    ClaimPersistenceFailed,
    MacroClaimTransmissionEvidenceInsufficient,
)
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_repository import ClaimRepository

_RELATIONS = ("supports", "contradicts", "context")
_ORIGIN_DOCUMENT_CHUNK = "document_chunk"
_ORIGIN_MACRO_OBSERVATION = "macro_observation"


@dataclass(frozen=True)
class ClaimResult:
    """一次 create_claim 的结果摘要（不含任何正文文本 / evidence）。"""

    claim_id: UUID
    claim_fingerprint: str
    replayed: bool


class ClaimService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_claim(self, draft: ClaimDraft) -> ClaimResult:
        # 1. 短 DB session：加载全部 EvidenceCard（真实 PG，不信任调用方 provenance）。
        evidence = await self._load_evidence(draft)
        # 2. 纯函数规则（支持 / critical / macro 传导）。
        self._apply_policy_rules(draft, evidence)
        # 3. 纯函数派生。
        fingerprint = compute_claim_fingerprint(
            claim_schema_version=CLAIM_SCHEMA_VERSION,
            company_id=draft.company_id,
            research_question=draft.research_question,
            statement=draft.statement,
            analysis_domain=draft.analysis_domain.value,
            claim_kind=draft.claim_kind.value,
            confidence=draft.confidence.value,
            importance=draft.importance.value,
            analyst_name=draft.analyst_name,
            analyst_version=draft.analyst_version,
            analyst_model_id=draft.analyst_model_id,
            supports=draft.support_evidence_ids,
            contradicts=draft.contradict_evidence_ids,
            context=draft.context_evidence_ids,
        )
        question_sha256 = compute_research_question_sha256(draft.research_question)

        # 4. 短 DB transaction：create_or_get + links + replay 校验。
        async with self._sessionmaker() as session:
            try:
                repo = ClaimRepository(session)
                existing = await repo.get_by_fingerprint(fingerprint)
                if existing is not None:
                    await self._verify_replay(
                        session, existing, draft, fingerprint, question_sha256
                    )
                    return self._to_result(existing, replayed=True)

                claim = ClaimModel(
                    claim_id=uuid.uuid4(),
                    **self._derived_kwargs(draft, fingerprint, question_sha256),
                )
                claim, created = await repo.create_or_get(claim)
                if not created:
                    # 并发输家：复用既有 Claim（replay 校验后返回）。
                    await self._verify_replay(session, claim, draft, fingerprint, question_sha256)
                    return self._to_result(claim, replayed=True)

                link_repo = ClaimEvidenceLinkRepository(session)
                await link_repo.bulk_insert(self._build_links(claim.claim_id, draft))
                await session.commit()
                return self._to_result(claim, replayed=False)
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ClaimPersistenceFailed() from exc

    # ------------------------------------------------------------------ 内部

    async def _load_evidence(self, draft: ClaimDraft) -> dict[UUID, EvidenceCardModel]:
        """从真实 PG 加载全部 EvidenceCard；缺失 / 跨公司 → CompanyMismatch。"""
        all_ids = (
            set(draft.support_evidence_ids)
            | set(draft.contradict_evidence_ids)
            | set(draft.context_evidence_ids)
        )
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(all_ids))
            )
            rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(all_ids):
            raise ClaimEvidenceCompanyMismatch()
        for card in by_id.values():
            if card.company_id != draft.company_id:
                raise ClaimEvidenceCompanyMismatch()
        return by_id

    @staticmethod
    def _apply_policy_rules(draft: ClaimDraft, evidence: dict[UUID, EvidenceCardModel]) -> None:
        """来源政策 / critical / macro 传导结构规则（纯函数，不做语义判断）。"""
        supports = [evidence[card_id] for card_id in draft.support_evidence_ids]
        if not supports:
            raise ClaimEvidenceInsufficient()

        if draft.importance == ClaimImportance.CRITICAL:
            if not any(card.critical_claim_eligible_snapshot for card in supports):
                raise ClaimCriticalEvidenceInsufficient()

        if draft.analysis_domain == ClaimAnalysisDomain.MACRO:
            macro_supports = [
                card for card in supports if card.origin_type == _ORIGIN_MACRO_OBSERVATION
            ]
            doc_transmission = [
                card for card in supports if card.origin_type == _ORIGIN_DOCUMENT_CHUNK
            ] + [
                evidence[card_id]
                for card_id in draft.context_evidence_ids
                if evidence[card_id].origin_type == _ORIGIN_DOCUMENT_CHUNK
            ]
            if not macro_supports or not doc_transmission:
                raise MacroClaimTransmissionEvidenceInsufficient()

    @staticmethod
    def _derived_kwargs(
        draft: ClaimDraft,
        fingerprint: str,
        question_sha256: str,
    ) -> dict:
        return {
            "company_id": draft.company_id,
            "research_question": draft.research_question,
            "research_question_sha256": question_sha256,
            "statement": draft.statement,
            "analysis_domain": draft.analysis_domain.value,
            "claim_kind": draft.claim_kind.value,
            "confidence": draft.confidence.value,
            "importance": draft.importance.value,
            "analyst_name": draft.analyst_name,
            "analyst_version": draft.analyst_version,
            "analyst_model_id": draft.analyst_model_id,
            "claim_schema_version": CLAIM_SCHEMA_VERSION,
            "claim_fingerprint": fingerprint,
        }

    @staticmethod
    def _build_links(claim_id: UUID, draft: ClaimDraft) -> list[ClaimEvidenceLinkModel]:
        links: list[ClaimEvidenceLinkModel] = []
        relation_ids = (
            (ClaimEvidenceRelation.SUPPORTS, draft.support_evidence_ids),
            (ClaimEvidenceRelation.CONTRADICTS, draft.contradict_evidence_ids),
            (ClaimEvidenceRelation.CONTEXT, draft.context_evidence_ids),
        )
        for relation, ids in relation_ids:
            for card_id in ids:
                links.append(
                    ClaimEvidenceLinkModel(
                        claim_id=claim_id,
                        evidence_card_id=card_id,
                        relation=relation.value,
                    )
                )
        return links

    async def _verify_replay(
        self,
        session,
        existing: ClaimModel,
        draft: ClaimDraft,
        fingerprint: str,
        question_sha256: str,
    ) -> None:
        """已有 fingerprint 的 Claim replay 完整性校验（重新加载 DB 状态核实）。

        校验项：link 数量 / relations / Evidence IDs、Evidence 公司一致性、
        critical 支持规则、macro 传导规则、statement / enums / company /
        question hash / analyst identity / claim_schema_version / fingerprint。
        发现损坏只抛 ClaimIntegrityError，**不自动 repair**（修改观点 = 新 Claim）。
        """
        # 重新加载 ClaimEvidenceLinks。
        links = await ClaimEvidenceLinkRepository(session).list_by_claim(existing.claim_id)
        actual: dict[str, list[UUID]] = {relation: [] for relation in _RELATIONS}
        for link in links:
            if link.relation not in actual:
                raise ClaimIntegrityError("claim replay integrity check failed on relation")
            actual[link.relation].append(link.evidence_card_id)
        for relation in actual:
            actual[relation].sort(key=str)

        expected = {
            "supports": sorted(draft.support_evidence_ids, key=str),
            "contradicts": sorted(draft.contradict_evidence_ids, key=str),
            "context": sorted(draft.context_evidence_ids, key=str),
        }
        for relation in _RELATIONS:
            if actual[relation] != expected[relation]:
                raise ClaimIntegrityError(
                    f"claim replay integrity check failed on links[{relation}]"
                )

        # 重新加载 EvidenceCards（link 指向的真实证据），核验 company + 政策规则。
        all_ids = actual["supports"] + actual["contradicts"] + actual["context"]
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(all_ids))
        )
        cards = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in cards}
        if len(by_id) != len(set(all_ids)):
            raise ClaimIntegrityError("claim replay integrity check failed on missing evidence")
        for card in by_id.values():
            if card.company_id != existing.company_id:
                raise ClaimIntegrityError("claim replay integrity check failed on evidence company")
        # critical 支持规则 / macro 传导规则从当前真实证据重新核验；replay
        # 阶段任何来源政策不满足都视为既有 Claim 数据损坏 → ClaimIntegrityError
        # （不自动 repair；初始创建时的政策错误仍抛各自专属错误）。
        try:
            self._apply_policy_rules(draft, by_id)
        except (
            ClaimEvidenceInsufficient,
            ClaimCriticalEvidenceInsufficient,
            MacroClaimTransmissionEvidenceInsufficient,
        ) as exc:
            raise ClaimIntegrityError(
                "claim replay integrity check failed on policy rules"
            ) from exc

        # Claim 字段与派生值逐项比对。
        pairs = (
            ("company_id", existing.company_id, draft.company_id),
            ("research_question", existing.research_question, draft.research_question),
            ("research_question_sha256", existing.research_question_sha256, question_sha256),
            ("statement", existing.statement, draft.statement),
            ("analysis_domain", existing.analysis_domain, draft.analysis_domain.value),
            ("claim_kind", existing.claim_kind, draft.claim_kind.value),
            ("confidence", existing.confidence, draft.confidence.value),
            ("importance", existing.importance, draft.importance.value),
            ("analyst_name", existing.analyst_name, draft.analyst_name),
            ("analyst_version", existing.analyst_version, draft.analyst_version),
            ("analyst_model_id", existing.analyst_model_id, draft.analyst_model_id),
            ("claim_schema_version", existing.claim_schema_version, CLAIM_SCHEMA_VERSION),
            ("claim_fingerprint", existing.claim_fingerprint, fingerprint),
        )
        for name, stored, expected_value in pairs:
            if stored != expected_value:
                raise ClaimIntegrityError(f"claim replay integrity check failed on {name}")

    @staticmethod
    def _to_result(claim: ClaimModel, *, replayed: bool) -> ClaimResult:
        return ClaimResult(
            claim_id=claim.claim_id,
            claim_fingerprint=claim.claim_fingerprint,
            replayed=replayed,
        )
