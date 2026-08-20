"""Claim synthesis input service (stage 4D.1A): validation + provenance + persistence + replay.

`create_or_get_synthesis(draft)` 把调用方显式选出的 2..50 条 Claim + company +
research_question + analysis_as_of 登记为一个不可变 SynthesisRun。**不调用 LLM、
不接 LangGraph 合成节点、不做语义筛选**——只负责输入集边界与 provenance 校验：
输入选择是显式的，本阶段只证明"这些已验证的 Claim 在什么 question / cutoff 下
进入综合"。

流程（spec R，两步提交镜像 ClaimService / ValuationAnalysisService）：
1. 短 DB session：逐 claim 加载并经 ClaimIntegrityGateway 完整性校验（domain
   fingerprint 重算），再校验 research-question 隔离（spec L）与 company 隔离
   （spec M），随后 temporal no-lookahead（spec O：evidence availability <=
   synthesis cutoff，复用 resolve_availability；无法解析 → 拒绝）。任一失败 →
   稳定错误，0 写。
2. 关闭 DB session（期间不持有 connection / transaction）。
3. 纯函数派生：research_question_sha256 + synthesis_fingerprint（canonical JSON
   + SHA-256，claims 按 claim_id 排序——input 顺序不影响指纹）。
4. 短 DB transaction：create_or_get run（ON CONFLICT(synthesis_fingerprint)，无
   进程锁）→ 首次 created=True 时 bulk insert 全部 input links 原子 commit；
   fingerprint 命中 → replay 时**重新加载 run / links / claims / domain
   provenance / evidence** 并逐项核实（run 字段 / exact claim set / company /
   question / temporal / fingerprint），任一损坏 → SynthesisIntegrityError，
   **不自动 repair**。任何 SQLAlchemyError → rollback + SynthesisPersistenceFailed。
5. 返回 SynthesisRunResult（synthesis_id / claim_ids / fingerprint / replayed /
   SynthesisInputSummary）。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph；不复制
Evidence / Calculation / Transmission / Comparison 的 ID 到 synthesis 表。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.contracts import compute_research_question_sha256
from app.claims.macro_policy import resolve_availability
from app.db.models.claim_synthesis_input_link import ClaimSynthesisInputLinkModel
from app.db.models.claim_synthesis_run import ClaimSynthesisRunModel
from app.db.models.evidence_card import EvidenceCardModel
from app.financial.calculations.service import FinancialCalculationService
from app.repositories.claim_repository import ClaimRepository
from app.repositories.claim_synthesis_input_link_repository import (
    ClaimSynthesisInputLinkRepository,
)
from app.repositories.claim_synthesis_run_repository import ClaimSynthesisRunRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.claim_service import ClaimService
from app.services.macro_claim_service import MacroClaimService
from app.synthesis.contracts import (
    CLAIM_SYNTHESIS_SCHEMA_VERSION,
    SynthesisInputDraft,
    SynthesisInputSummary,
    VerifiedSynthesisClaim,
    VerifiedSynthesisRun,
    build_synthesis_input_summary,
    compute_synthesis_fingerprint,
)
from app.synthesis.errors import (
    SynthesisClaimIntegrityError,
    SynthesisCompanyMismatch,
    SynthesisFutureEvidence,
    SynthesisIntegrityError,
    SynthesisPersistenceFailed,
    SynthesisResearchQuestionMismatch,
    SynthesisRunNotFound,
    SynthesisTemporalEvidenceInsufficient,
)
from app.synthesis.integrity import ClaimIntegrityGateway
from app.valuation.comparison_service import RelativeValuationComparisonService

_ORIGIN_DOCUMENT_CHUNK = "document_chunk"
_ORIGIN_MACRO_OBSERVATION = "macro_observation"


@dataclass(frozen=True)
class SynthesisRunResult:
    """一次 create_or_get_synthesis 的结果摘要（不含任何正文文本 / evidence）。"""

    synthesis_id: UUID
    claim_ids: tuple[UUID, ...]
    fingerprint: str
    replayed: bool
    summary: SynthesisInputSummary


class SynthesisService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker
        self._gateway = ClaimIntegrityGateway(
            claim_service=ClaimService(sessionmaker),
            macro_claim_service=MacroClaimService(sessionmaker),
            financial_calculation_service=FinancialCalculationService(sessionmaker),
            valuation_comparison_service=RelativeValuationComparisonService(sessionmaker),
        )

    async def create_or_get_synthesis(self, draft: SynthesisInputDraft) -> SynthesisRunResult:
        """把显式 Claim 输入集原子登记为一个不可变 SynthesisRun（replay 语义）。

        两步提交：短 session 加载 + 校验 → 关闭 → 纯函数派生 → 短 transaction
        原子写。任一校验失败 → 0 写；fingerprint 命中 → 完整重放校验后复用。
        """
        # 1. 短 DB session：加载 + gateway + 隔离 + temporal 校验（0 写）。
        async with self._sessionmaker() as session:
            verified = await self._load_and_validate(
                session,
                company_id=draft.company_id,
                research_question=draft.research_question,
                analysis_as_of=draft.analysis_as_of,
                claim_ids=draft.claim_ids,
            )

        # 2-3. 关闭 session；纯函数派生（不持有 DB connection）。
        question_sha256 = compute_research_question_sha256(draft.research_question)
        fingerprint = compute_synthesis_fingerprint(
            synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
            company_id=draft.company_id,
            research_question=draft.research_question,
            research_question_sha256=question_sha256,
            analysis_as_of=draft.analysis_as_of,
            claims=verified,
        )
        summary = build_synthesis_input_summary(verified)

        # 4. 短 DB transaction：create_or_get run + links（原子）。
        async with self._sessionmaker() as session:
            try:
                run = ClaimSynthesisRunModel(
                    synthesis_id=uuid.uuid4(),
                    company_id=draft.company_id,
                    research_question=draft.research_question,
                    research_question_sha256=question_sha256,
                    analysis_as_of=draft.analysis_as_of,
                    synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
                    synthesis_fingerprint=fingerprint,
                )
                run, was_created = await ClaimSynthesisRunRepository(session).create_or_get(run)
                if was_created:
                    await ClaimSynthesisInputLinkRepository(session).bulk_insert(
                        [
                            ClaimSynthesisInputLinkModel(
                                synthesis_id=run.synthesis_id,
                                claim_id=claim_id,
                            )
                            for claim_id in draft.claim_ids
                        ]
                    )
                    await session.commit()
                else:
                    # 并发输家 / 已存在 run：完整重放校验（不写任何行）。
                    await self._verify_replay(session, run, draft)
                    await session.commit()
            except SynthesisIntegrityError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise SynthesisPersistenceFailed() from exc

        return SynthesisRunResult(
            synthesis_id=run.synthesis_id,
            claim_ids=tuple(draft.claim_ids),
            fingerprint=fingerprint,
            replayed=not was_created,
            summary=summary,
        )

    async def verify_synthesis_integrity(
        self,
        session: AsyncSession,
        synthesis_id: UUID,
    ) -> VerifiedSynthesisRun:
        """公共 read-only：验证已登记 SynthesisRun 的完整 read-side integrity。

        与 create/replay **完全同一 policy**（spec Gate 0）：
        1. 重新加载 SynthesisRun（缺失 → SynthesisRunNotFound）；
        2. 重新加载 input links → exact claim set（link 增删 → fingerprint
           变化 → 拒绝）；
        3. 逐 claim 经 ClaimIntegrityGateway 完整性校验（domain dispatch）；
        4. 以 run 自身字段为预期重跑 company isolation / research-question
           isolation / temporal no-lookahead / domain analysis cutoff；
        5. 重新计算 synthesis_fingerprint 并与 persisted 比较。

        任一损坏 → 稳定错误（SynthesisIntegrityError 或同族子类），**不自动
        repair**。返回 VerifiedSynthesisRun（claim_ids canonical），消费方只
        消费该投影，不复制 replay 规则。
        """
        run = await ClaimSynthesisRunRepository(session).get_by_id(synthesis_id)
        if run is None:
            raise SynthesisRunNotFound()
        links = await ClaimSynthesisInputLinkRepository(session).list_by_synthesis(synthesis_id)
        if not links:
            raise SynthesisIntegrityError("synthesis run has no input links")
        claim_ids = sorted((link.claim_id for link in links), key=str)
        verified = await self._load_and_validate(
            session,
            company_id=run.company_id,
            research_question=run.research_question,
            analysis_as_of=run.analysis_as_of,
            claim_ids=claim_ids,
        )
        question_sha256 = compute_research_question_sha256(run.research_question)
        fingerprint = compute_synthesis_fingerprint(
            synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
            company_id=run.company_id,
            research_question=run.research_question,
            research_question_sha256=question_sha256,
            analysis_as_of=run.analysis_as_of,
            claims=verified,
        )
        if fingerprint != run.synthesis_fingerprint:
            raise SynthesisIntegrityError("synthesis run fingerprint mismatch")
        return VerifiedSynthesisRun(
            synthesis_id=run.synthesis_id,
            company_id=run.company_id,
            research_question=run.research_question,
            research_question_sha256=question_sha256,
            analysis_as_of=run.analysis_as_of,
            synthesis_fingerprint=run.synthesis_fingerprint,
            verified_claims=verified,
        )

    # ------------------------------------------------------------------ 内部

    async def _load_and_validate(
        self,
        session: AsyncSession,
        *,
        company_id: UUID,
        research_question: str,
        analysis_as_of: date,
        claim_ids: list[UUID],
    ) -> list[VerifiedSynthesisClaim]:
        """短 session 加载 + 完整校验（gateway / question / company / temporal）。

        逐 claim 校验其 domain provenance 完整性，再校验 research-question 与
        company 隔离（spec L/M），随后 temporal no-lookahead（spec O）。
        create/replay 与 read-side verify（verify_synthesis_integrity）共用同一
        policy：预期值由调用方显式给出（draft 字段或 run 自身字段）。
        """
        gateway = self._gateway
        question_sha256 = compute_research_question_sha256(research_question)
        claim_repo = ClaimRepository(session)
        verified: list[VerifiedSynthesisClaim] = []
        for claim_id in claim_ids:
            claim_model = await claim_repo.get_by_id(claim_id)
            if claim_model is None:
                raise SynthesisClaimIntegrityError("input claim missing")
            if claim_model.invalidated_at is not None:
                # P0.5：已被 FutureEvidence 恢复排除的污染 claim，不进综合上下文。
                continue
            claim = await gateway.verify_claim(session, claim_id)
            if claim.research_question_sha256 != question_sha256:
                raise SynthesisResearchQuestionMismatch()
            if claim.company_id != company_id:
                raise SynthesisCompanyMismatch()
            verified.append(claim)
        verified.sort(key=lambda claim: str(claim.claim_id))
        await self._check_temporal(session, verified, analysis_as_of)
        return verified

    async def _check_temporal(
        self,
        session: AsyncSession,
        verified: list[VerifiedSynthesisClaim],
        cutoff: date,
    ) -> None:
        """no-lookahead：evidence availability <= synthesis cutoff（spec O）。

        - 逐条加载 claim_evidence_links 的 Evidence，解析真实 availability
          （复用 resolve_availability，document → published_at 否则 acquired_at；
          macro → snapshot.fetched_at），要求 availability.date() <= cutoff，
          future → SynthesisFutureEvidence；
        - 无法解析（snapshot / source 缺失、provenance 缺失）→
          SynthesisTemporalEvidenceInsufficient（不伪造缺失日期）；
        - domain_analysis_as_of（macro chain / valuation profile）也必须 <=
          cutoff（域分析截止晚于综合截止 = 分析基于综合之后的信息）。
        """
        card_ids = {card_id for claim in verified for card_id in claim.evidence_card_ids}
        cards = await self._load_evidence_cards(session, card_ids)
        # 含 document_chunk / user_supplied / financial_extraction 等所有
        # 绑定 SourceRecord 的 origin（F1：financial_extraction 卡 source 缺失
        # 会导致 availability 无法解析 → 误判 TemporalEvidenceInsufficient）。
        source_ids = {card.source_id for card in cards.values() if card.source_id is not None}
        snapshot_ids = {
            card.macro_snapshot_id
            for card in cards.values()
            if card.origin_type == _ORIGIN_MACRO_OBSERVATION and card.macro_snapshot_id is not None
        }
        sources = await self._load_rows(session, SourceRecordRepository, source_ids)
        snapshots = await self._load_rows(session, MacroSnapshotRepository, snapshot_ids)
        for claim in verified:
            if claim.domain_analysis_as_of is not None and claim.domain_analysis_as_of > cutoff:
                raise SynthesisFutureEvidence()
            for card_id in claim.evidence_card_ids:
                card = cards[card_id]
                if card.origin_type == _ORIGIN_MACRO_OBSERVATION:
                    snapshot = snapshots.get(card.macro_snapshot_id)
                    availability = resolve_availability(
                        origin_type=card.origin_type,
                        snapshot_fetched_at=snapshot.fetched_at if snapshot else None,
                        source_published_at=None,
                        source_acquired_at=None,
                    )
                else:
                    source = sources.get(card.source_id)
                    availability = resolve_availability(
                        origin_type=card.origin_type,
                        snapshot_fetched_at=None,
                        source_published_at=source.published_at if source else None,
                        source_acquired_at=source.acquired_at if source else None,
                    )
                if availability is None:
                    raise SynthesisTemporalEvidenceInsufficient()
                if availability.date() > cutoff:
                    raise SynthesisFutureEvidence()

    async def find_future_evidence_claim_ids(
        self,
        session: AsyncSession,
        claim_ids: list[UUID],
        analysis_as_of: date,
    ) -> list[UUID]:
        """P0.5：找出 evidence availability / domain as_of > cutoff 的污染 claim。

        与 `_check_temporal` 同一 policy，但**不 raise**：返回污染 claim_id 列表
        （供 FutureEvidence 有界恢复 invalidate；已 invalidated 的不计入）。
        """
        gateway = self._gateway
        claim_repo = ClaimRepository(session)
        verified: list[VerifiedSynthesisClaim] = []
        for claim_id in claim_ids:
            model = await claim_repo.get_by_id(claim_id)
            if model is None or model.invalidated_at is not None:
                continue
            claim = await gateway.verify_claim(session, claim_id)
            verified.append(claim)
        if not verified:
            return []
        card_ids = {card_id for claim in verified for card_id in claim.evidence_card_ids}
        cards = await self._load_evidence_cards(session, card_ids)
        source_ids = {card.source_id for card in cards.values() if card.source_id is not None}
        snapshot_ids = {
            card.macro_snapshot_id
            for card in cards.values()
            if card.origin_type == _ORIGIN_MACRO_OBSERVATION and card.macro_snapshot_id is not None
        }
        sources = await self._load_rows(session, SourceRecordRepository, source_ids)
        snapshots = await self._load_rows(session, MacroSnapshotRepository, snapshot_ids)
        offending: list[UUID] = []
        for claim in verified:
            if (
                claim.domain_analysis_as_of is not None
                and claim.domain_analysis_as_of > analysis_as_of
            ):
                offending.append(claim.claim_id)
                continue
            for card_id in claim.evidence_card_ids:
                card = cards[card_id]
                if card.origin_type == _ORIGIN_MACRO_OBSERVATION:
                    snapshot = snapshots.get(card.macro_snapshot_id)
                    availability = resolve_availability(
                        origin_type=card.origin_type,
                        snapshot_fetched_at=snapshot.fetched_at if snapshot else None,
                        source_published_at=None,
                        source_acquired_at=None,
                    )
                else:
                    source = sources.get(card.source_id)
                    availability = resolve_availability(
                        origin_type=card.origin_type,
                        snapshot_fetched_at=None,
                        source_published_at=source.published_at if source else None,
                        source_acquired_at=source.acquired_at if source else None,
                    )
                if availability is None or availability.date() > analysis_as_of:
                    offending.append(claim.claim_id)
                    break
        return offending

    async def _load_evidence_cards(
        self, session: AsyncSession, card_ids: set[UUID]
    ) -> dict[UUID, EvidenceCardModel]:
        """批量加载 EvidenceCard；任一缺失 → claim provenance 损坏（IntegrityError）。"""
        if not card_ids:
            return {}
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
        )
        cards = {card.evidence_card_id: card for card in result.scalars().all()}
        if len(cards) != len(card_ids):
            raise SynthesisClaimIntegrityError("input claim evidence missing")
        return cards

    @staticmethod
    async def _load_rows(
        session: AsyncSession,
        repo_cls,
        row_ids: set[UUID],
    ) -> dict[UUID, object]:
        """批量加载 source / snapshot 行；缺失行 → {}（availability 无法解析）。"""
        by_id: dict[UUID, object] = {}
        for row_id in row_ids:
            row = await repo_cls(session).get_by_id(row_id)
            if row is not None:
                by_id[row_id] = row
        return by_id

    async def _verify_replay(
        self,
        session: AsyncSession,
        run: ClaimSynthesisRunModel,
        draft: SynthesisInputDraft,
    ) -> None:
        """replay 完整性校验（spec S）：重新执行全部校验并逐项核实。

        - 重新加载 run 的 links → exact claim set 必须 == draft.claim_ids；
        - 重新执行 gateway + company / question / temporal policy；
        - 重新算 synthesis_fingerprint 对比 run.synthesis_fingerprint；
        - 核实 run 字段（company / research_question / sha256 / cutoff /
          schema_version / fingerprint）。
        任一损坏 → SynthesisIntegrityError，**不自动 repair**。
        """
        question_sha256 = compute_research_question_sha256(draft.research_question)
        if run.company_id != draft.company_id:
            raise SynthesisIntegrityError("synthesis run company mismatch")
        if run.research_question != draft.research_question:
            raise SynthesisIntegrityError("synthesis run research question mismatch")
        if run.research_question_sha256 != question_sha256:
            raise SynthesisIntegrityError("synthesis run research question hash mismatch")
        if run.analysis_as_of != draft.analysis_as_of:
            raise SynthesisIntegrityError("synthesis run analysis cutoff mismatch")
        if run.synthesis_schema_version != CLAIM_SYNTHESIS_SCHEMA_VERSION:
            raise SynthesisIntegrityError("synthesis run schema version mismatch")

        links = await ClaimSynthesisInputLinkRepository(session).list_by_synthesis(run.synthesis_id)
        linked_claim_ids = sorted((link.claim_id for link in links), key=str)
        if linked_claim_ids != draft.claim_ids:
            raise SynthesisIntegrityError("synthesis run claim set mismatch")

        verified = await self._load_and_validate(
            session,
            company_id=draft.company_id,
            research_question=draft.research_question,
            analysis_as_of=draft.analysis_as_of,
            claim_ids=draft.claim_ids,
        )
        fingerprint = compute_synthesis_fingerprint(
            synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
            company_id=draft.company_id,
            research_question=draft.research_question,
            research_question_sha256=question_sha256,
            analysis_as_of=draft.analysis_as_of,
            claims=verified,
        )
        if fingerprint != run.synthesis_fingerprint:
            raise SynthesisIntegrityError("synthesis run fingerprint mismatch")
