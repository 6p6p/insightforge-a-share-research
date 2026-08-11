"""Evidence-bound section revision service (stage 5E.2A, spec G-L/M/N).

流程（短 verify session + 纯函数 + 短事务，镜像 DraftSectionService）：
1. 防御性 request 校验（`RevisionRequest` 构造已校验，服务层再兜底）；
2. **递归 verify source DraftSection**（`_verify_source_draft`）：v2 原始 draft
   走 `DraftSectionService.load_section_input`（完整 replay 校验 + 重建 section
   input）；v1 修订输出沿 revision link 递归回其源（终止于 v2 原始 draft），
   **防环**（visiting set）→ `VerifiedSourceDraft`（同一 section scope pack +
   原正文段落）；
3. **verify trigger artifact**（spec G/H）：check_result_id /
   review_action_id / human_decision_id 三选一；audit_rewrite 必须
   action_type=rewrite、human_rewrite 必须 decision=rewrite；派生 target
   sections + section-normalized feedback（spec I）；
4. target section 校验（spec H）：source section ∈ trigger target sections，
   否则 `RevisionTargetSectionInvalid`（0 write）；
5. 纯函数构造 Revision Input Pack + `compute_revision_input_fingerprint`
   （spec K）；
6. **replay check**（短 session，0 LLM）：同指纹已有修订行 → `_verify_revision_row`
   完整重放校验后直接返回；
7. 关闭 session → 调 Revision Writer 模型（structured output，spec F）→
   `WriterDecision`（与 5B 同一契约，spec J）；
8. hard provenance validation：**复用 5B 的 `validate_decision` / `resolve_decision`**
   （同一 section scope 的 Claim scope / Evidence binding / numeric / forbidden /
   inline alias，spec J）→ 规范化 persisted payload；
9. `compute_section_fingerprint`（writer_input_fingerprint=revision input
   fingerprint + payload）——修订正文的不可变指纹；
10. 短 DB transaction：**同一事务**原子 `create_or_get` draft_sections +
    draft_section_revisions（无进程锁，ON CONFLICT DO NOTHING）→ 并发同 revision
    → 最终 1 revised draft + 1 revision link；命中既有行 → replay 校验；
    SQLAlchemyError → rollback + `RevisionPersistenceFailed`；
11. 返回 `RevisionResult`（不含正文段落 / prompt / raw response）。

**公共 read-side**：`verify_revision_integrity(revision_id)`——递归 verify source
DraftSection / verify trigger / rebuild feedback+input / verify revised / 重算
revision fingerprint；任一 tamper → `RevisionIntegrityError`（**不自动 repair**）。

**不创建 Report / Audit / CheckResult**；不接 LangGraph；不调用 Retrieval /
Chroma / tools / web search；Revision Writer 不能添加新 Claim/Evidence、不改变
section scope（spec J）。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.draft_section import DraftSectionModel
from app.db.models.draft_section_revision import DraftSectionRevisionModel
from app.draft_section.contracts import (
    DRAFT_SECTION_SCHEMA_VERSION,
    WRITER_VERSION,
    VerifiedDraftSection,
    WriterDecision,
    compute_section_fingerprint,
)
from app.draft_section.repository import DraftSectionRepository
from app.draft_section.service import (
    DraftSectionService,
    _conflict_fingerprint_data,
    _evidence_claim_relations,
    _gap_fingerprint_data,
)
from app.draft_section.validate import (
    resolve_decision,
    validate_decision,
    verify_payload_contracts,
)
from app.report.check_service import ReportCheckService
from app.review.contracts import ACTION_TYPE_REWRITE, HUMAN_DECISION_REWRITE
from app.review.service import ReviewActionService
from app.revision.contracts import (
    DRAFT_SECTION_REVISION_SCHEMA_VERSION,
    REVISION_WRITER_NAME,
    REVISION_WRITER_VERSION,
    TRIGGER_TYPE_AUDIT_REWRITE,
    TRIGGER_TYPE_DETERMINISTIC_CHECK,
    TRIGGER_TYPE_HUMAN_REWRITE,
    RevisionRequest,
    RevisionResult,
    RevisionTrigger,
    VerifiedRevisedDraft,
    VerifiedRevision,
    VerifiedSourceDraft,
    VerifiedTrigger,
    compute_revision_input_fingerprint,
)
from app.revision.derive import (
    action_target_section_ids,
    check_target_section_ids,
    derive_audit_feedback,
    derive_check_feedback,
    derive_human_feedback,
    derive_trigger_type,
    validate_target_section,
)
from app.revision.errors import (
    RevisionError,
    RevisionInputError,
    RevisionIntegrityError,
    RevisionNotFound,
    RevisionPersistenceFailed,
    RevisionSourceNotFound,
    RevisionTriggerInvalid,
    RevisionWriterMalformedOutput,
    RevisionWriterModelUnavailable,
)
from app.revision.model import RevisionWriterModel
from app.revision.packs import RevisionInputPack, build_revision_input_pack
from app.revision.repository import RevisionRepository


@dataclass(frozen=True)
class _RevisionTriggerState:
    """trigger 派生中间态（内部使用，避免重复三元判断）。"""

    trigger_type: str
    check_result_id: UUID | None = None
    review_action_id: UUID | None = None
    human_decision_id: UUID | None = None


class RevisionService:
    """Evidence-bound Section Rewriter：verified source + trigger → 修订正文 draft。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        model: RevisionWriterModel,
        draft_section_service: DraftSectionService,
        check_service: ReportCheckService,
        review_action_service: ReviewActionService,
    ) -> None:
        """model 必须提供（replay 也用它取 writer_model_id 重算输入指纹）。

        draft_section / check / review_action service 显式注入（上游 verify 链由
        调用方组合）。构造不触发模型调用；只有 `revise_section` 真正缺少既有行时
        才调 `model.rewrite()`。
        """
        self._sessionmaker = sessionmaker
        self._model = model
        self._draft_section_service = draft_section_service
        self._check_service = check_service
        self._review_action_service = review_action_service

    # ------------------------------------------------------------------ create / get

    async def revise_section(self, request: RevisionRequest) -> RevisionResult:
        """修订一个已验证 source DraftSection；同输入 → replay 同一行（0 model calls）。"""
        self._check_request(request)

        # 1. 递归 verify source draft（v2 原始或 v1 修订输出）→ section input + 原文。
        source = await self._verify_source_draft(request.source_draft_section_id, frozenset())

        # 2. verify trigger artifact → target sections + feedback（spec G/H/I）。
        trigger_state = _trigger_state(request.trigger)
        trigger = await self._verify_trigger_request(trigger_state, source.section_id)

        # 3. target section 校验（spec H）。
        validate_target_section(source.section_id, trigger.target_section_ids)

        # 4. 纯函数构造 Revision Input Pack + 输入指纹（spec K）。
        pack = build_revision_input_pack(
            input_pack=source.pack,
            original_paragraphs=source.original_paragraphs,
            revision_feedback=trigger.feedback,
        )
        revision_input_fingerprint = self._compute_input_fingerprint(source, trigger)

        # 5. replay check（0 LLM）：同指纹已有修订行 → 完整重放校验后直接返回。
        existing_draft = await self._find_draft(revision_input_fingerprint)
        if existing_draft is not None:
            existing_link = await self._find_link(revision_input_fingerprint)
            if existing_link is None:
                raise RevisionIntegrityError("revised draft exists without revision link")
            # 完整重放校验（抛错即 tamper）；返回值仅用于 read-side。
            await self._verify_revision_row(existing_link, frozenset())
            return self._result(existing_link, replayed=True)

        # 6. 关闭 session → 调模型（structured output）。
        decision = await self._call_model(pack)

        # 7. hard provenance validation（复用 5B，同一 section scope，spec J）→ payload。
        validate_decision(
            pack=source.pack,
            decision=decision,
            total_claim_count=source.total_claim_count,
        )
        payload = resolve_decision(source.pack, decision)

        # 8. 修订正文不可变指纹（writer_input_fingerprint = revision input 指纹）。
        section_fingerprint = compute_section_fingerprint(
            writer_input_fingerprint=revision_input_fingerprint,
            section_payload=payload,
        )
        expected_draft = self._revised_draft_model(
            source=source,
            revision_input_fingerprint=revision_input_fingerprint,
            payload=payload,
            section_fingerprint=section_fingerprint,
        )
        expected_link = self._revision_link_model(
            source=source,
            request=request,
            trigger_state=trigger_state,
            draft=expected_draft,
            revision_input_fingerprint=revision_input_fingerprint,
        )

        # 9. 同一短事务原子创建 draft + revision link（spec L，并发同 revision →
        #    最终 1 revised draft + 1 revision link）。
        async with self._sessionmaker() as session:
            try:
                draft_row, draft_created = await DraftSectionRepository(session).create_or_get(
                    expected_draft
                )
                link_row, link_created = await RevisionRepository(session).create_or_get(
                    expected_link
                )
                if not draft_created:
                    # 并发输家 / 已存在结果：完整 replay 校验（不写任何行）。
                    self._verify_revised_draft_replay(source, draft_row, revision_input_fingerprint)
                if not link_created:
                    self._verify_link_matches(
                        link_row,
                        draft_row,
                        request,
                        trigger_state,
                        revision_input_fingerprint,
                    )
                await session.commit()
            except RevisionError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise RevisionPersistenceFailed() from exc

        # 10. 结果摘要（不含正文段落 / prompt / raw response）。
        return self._result(link_row, replayed=not (draft_created and link_created))

    async def verify_revision_integrity(self, revision_id: UUID) -> VerifiedRevision:
        """public read-only 校验（spec M）：递归重建并重放验证，**不自动 repair**。

        覆盖：source DraftSection（递归至 v2 原始 draft）/ trigger / feedback+input
        / revised DraftSection / revision fingerprint——任一 tamper →
        `RevisionIntegrityError`。
        """
        async with self._sessionmaker() as session:
            link = await RevisionRepository(session).get_by_id(revision_id)
        if link is None:
            raise RevisionNotFound()
        return await self._verify_revision_row(link, frozenset())

    async def verify_revised_draft_section(self, draft_section_id: UUID) -> VerifiedDraftSection:
        """verify 一个**修订输出**（writer_version=1）draft → 其 VerifiedDraftSection。

        供 ReportService 装配含修订输出的新 Report（spec N）时使用：定位 revision
        link（revised→link）→ `_verify_revision_row` 完整重放（source / trigger /
        revised）→ 把 verified_revised 投影为标准 `VerifiedDraftSection`。任一 tamper
        → `RevisionIntegrityError`（**不自动 repair**）。仅用于修订输出；v2 原始
        draft 由 `DraftSectionService.verify_draft_section_integrity` 负责。
        """
        link = await self._find_link_by_revised_draft(draft_section_id)
        if link is None:
            raise RevisionIntegrityError("revision output draft missing revision link")
        verified = await self._verify_revision_row(link, frozenset())
        revised = verified.verified_revised
        return VerifiedDraftSection(
            draft_section_id=revised.draft_section_id,
            outline_id=revised.outline_id,
            section_id=revised.section_id,
            section_order=revised.section_order,
            section_type=revised.section_type,
            title=revised.title,
            section_schema_version=revised.section_schema_version,
            writer_name=revised.writer_name,
            writer_version=revised.writer_version,
            writer_model_id=revised.writer_model_id,
            writer_input_fingerprint=revised.writer_input_fingerprint,
            section_fingerprint=revised.section_fingerprint,
            paragraph_count=revised.paragraph_count,
        )

    # ------------------------------------------------------------ 递归 verify 核心

    async def _verify_revision_row(
        self,
        link: DraftSectionRevisionModel,
        visiting: frozenset,
    ) -> VerifiedRevision:
        """递归验证一条 revision row：source → trigger → feedback+input → revised。"""
        # 1. source（递归；同一 section scope 的 input pack + 原正文）。
        source = await self._verify_source_draft(link.source_draft_section_id, visiting)

        # 2. trigger（按 row 的 FK / trigger_type 重放）。
        trigger = await self._verify_trigger_row(link, source.section_id)

        # 3. target section 校验（spec H）。
        validate_target_section(source.section_id, trigger.target_section_ids)

        # 4. rebuild feedback + input → 重算 revision input 指纹。
        revision_input_fingerprint = self._compute_input_fingerprint(source, trigger)

        # 5. revision row 身份字段逐一对比。
        if link.revision_schema_version != DRAFT_SECTION_REVISION_SCHEMA_VERSION:
            raise RevisionIntegrityError("revision schema version mismatch")
        if link.trigger_type != trigger.trigger_type:
            raise RevisionIntegrityError("revision trigger_type mismatch")
        if link.revision_round < 1:
            raise RevisionIntegrityError("revision round invalid")
        if link.revision_fingerprint != revision_input_fingerprint:
            raise RevisionIntegrityError("revision fingerprint mismatch")

        # 6. verify revised draft（身份 / payload contracts / section 指纹重放）。
        revised = await self._verify_revised_draft(
            source,
            link.revised_draft_section_id,
            revision_input_fingerprint,
        )

        return VerifiedRevision(
            revision_id=link.revision_id,
            source_draft_section_id=link.source_draft_section_id,
            revised_draft_section_id=link.revised_draft_section_id,
            revision_round=link.revision_round,
            trigger_type=link.trigger_type,
            revision_schema_version=link.revision_schema_version,
            revision_fingerprint=link.revision_fingerprint,
            created_at=link.created_at,
            source=source,
            trigger=trigger,
            verified_revised=revised,
        )

    async def _verify_source_draft(
        self,
        draft_section_id: UUID,
        visiting: frozenset,
    ) -> VerifiedSourceDraft:
        """递归验证 source draft，重建同一 section scope 的 input + 原正文段落。

        - v2 原始 draft（writer_version=2）：`DraftSectionService.load_section_input`
          （完整 replay 校验）→ section input + outline fingerprint；
        - v1 修订输出（writer_version=1）：沿 revision link（revised→link）递归回
          其源（终止于 v2 原始 draft），用递归产物的 section input（同一 scope）+
          本 draft 自己的正文段落；link 缺失 / 环 → `RevisionIntegrityError`。
        """
        row = await self._load_draft_row(draft_section_id)
        if row is None:
            raise RevisionSourceNotFound(f"source draft section {draft_section_id} not found")

        if row.writer_version == WRITER_VERSION:
            loaded = await self._draft_section_service.load_section_input(draft_section_id)
            return VerifiedSourceDraft(
                draft_section_id=row.draft_section_id,
                section_fingerprint=loaded.verified.section_fingerprint,
                outline_id=row.outline_id,
                outline_fingerprint=loaded.outline.outline_fingerprint,
                section_id=row.section_id,
                section_order=row.section_order,
                section_type=row.section_type,
                title=row.title,
                total_claim_count=len(loaded.outline.verified_synthesis_result.input_claim_ids),
                claims=loaded.claims,
                evidence=loaded.evidence,
                conflicts=loaded.conflicts,
                gaps=loaded.gaps,
                pack=loaded.pack,
                original_paragraphs=_paragraph_texts(row.section_payload),
            )

        if row.writer_version == REVISION_WRITER_VERSION:
            link = await self._find_link_by_revised_draft(row.draft_section_id)
            if link is None:
                raise RevisionIntegrityError("revision output draft missing revision link")
            if link.revision_id in visiting:
                raise RevisionIntegrityError("revision chain cycle detected")
            verified_rev = await self._verify_revision_row(link, visiting | {link.revision_id})
            source = verified_rev.source
            return VerifiedSourceDraft(
                draft_section_id=row.draft_section_id,
                section_fingerprint=verified_rev.verified_revised.section_fingerprint,
                outline_id=source.outline_id,
                outline_fingerprint=source.outline_fingerprint,
                section_id=source.section_id,
                section_order=source.section_order,
                section_type=source.section_type,
                title=source.title,
                total_claim_count=source.total_claim_count,
                claims=source.claims,
                evidence=source.evidence,
                conflicts=source.conflicts,
                gaps=source.gaps,
                pack=source.pack,
                original_paragraphs=_paragraph_texts(row.section_payload),
            )

        raise RevisionIntegrityError(
            f"source draft writer_version={row.writer_version} not supported"
        )

    # ------------------------------------------------------------ trigger 派生

    async def _verify_trigger_request(
        self,
        state: _RevisionTriggerState,
        source_section_id: str,
    ) -> VerifiedTrigger:
        """create 路径：由 caller 的 trigger union verify artifact + 派生反馈。"""
        if state.trigger_type == TRIGGER_TYPE_DETERMINISTIC_CHECK:
            verified_check = await self._check_service.verify_check_result_integrity(
                state.check_result_id
            )
            return VerifiedTrigger(
                trigger_type=TRIGGER_TYPE_DETERMINISTIC_CHECK,
                artifact_id=verified_check.check_result_id,
                artifact_fingerprint=verified_check.check_fingerprint,
                target_section_ids=check_target_section_ids(verified_check),
                feedback=derive_check_feedback(verified_check, source_section_id),
            )
        if state.trigger_type == TRIGGER_TYPE_AUDIT_REWRITE:
            verified_action = await self._review_action_service.verify_review_action_integrity(
                state.review_action_id
            )
            if verified_action.action_type != ACTION_TYPE_REWRITE:
                raise RevisionTriggerInvalid("review action action_type 不是 rewrite")
            return VerifiedTrigger(
                trigger_type=TRIGGER_TYPE_AUDIT_REWRITE,
                artifact_id=verified_action.review_action_id,
                artifact_fingerprint=verified_action.action_fingerprint,
                target_section_ids=action_target_section_ids(verified_action.action_payload),
                feedback=derive_audit_feedback(verified_action.verified_audit, source_section_id),
            )
        if state.trigger_type == TRIGGER_TYPE_HUMAN_REWRITE:
            verified_decision = await self._review_action_service.verify_human_decision_integrity(
                state.human_decision_id
            )
            if verified_decision.decision != HUMAN_DECISION_REWRITE:
                raise RevisionTriggerInvalid("human decision 不是 rewrite")
            action = verified_decision.verified_request.verified_action
            return VerifiedTrigger(
                trigger_type=TRIGGER_TYPE_HUMAN_REWRITE,
                artifact_id=verified_decision.human_decision_id,
                artifact_fingerprint=verified_decision.decision_fingerprint,
                target_section_ids=action_target_section_ids(action.action_payload),
                feedback=derive_human_feedback(
                    action.verified_audit, source_section_id, verified_decision.comment
                ),
            )
        raise RevisionInputError("trigger 必须恰好一个非空")

    async def _verify_trigger_row(
        self,
        link: DraftSectionRevisionModel,
        source_section_id: str,
    ) -> VerifiedTrigger:
        """verify 路径：由 revision row 的 trigger_type + FK 重放 trigger artifact。"""
        if link.trigger_type == TRIGGER_TYPE_DETERMINISTIC_CHECK:
            if (
                link.check_result_id is None
                or link.review_action_id is not None
                or link.human_decision_id is not None
            ):
                raise RevisionIntegrityError("revision trigger FK mismatch (deterministic_check)")
            verified_check = await self._check_service.verify_check_result_integrity(
                link.check_result_id
            )
            return VerifiedTrigger(
                trigger_type=TRIGGER_TYPE_DETERMINISTIC_CHECK,
                artifact_id=verified_check.check_result_id,
                artifact_fingerprint=verified_check.check_fingerprint,
                target_section_ids=check_target_section_ids(verified_check),
                feedback=derive_check_feedback(verified_check, source_section_id),
            )
        if link.trigger_type == TRIGGER_TYPE_AUDIT_REWRITE:
            if (
                link.review_action_id is None
                or link.check_result_id is not None
                or link.human_decision_id is not None
            ):
                raise RevisionIntegrityError("revision trigger FK mismatch (audit_rewrite)")
            verified_action = await self._review_action_service.verify_review_action_integrity(
                link.review_action_id
            )
            if verified_action.action_type != ACTION_TYPE_REWRITE:
                raise RevisionIntegrityError("revision action_type 不是 rewrite")
            return VerifiedTrigger(
                trigger_type=TRIGGER_TYPE_AUDIT_REWRITE,
                artifact_id=verified_action.review_action_id,
                artifact_fingerprint=verified_action.action_fingerprint,
                target_section_ids=action_target_section_ids(verified_action.action_payload),
                feedback=derive_audit_feedback(verified_action.verified_audit, source_section_id),
            )
        if link.trigger_type == TRIGGER_TYPE_HUMAN_REWRITE:
            if (
                link.human_decision_id is None
                or link.check_result_id is not None
                or link.review_action_id is not None
            ):
                raise RevisionIntegrityError("revision trigger FK mismatch (human_rewrite)")
            verified_decision = await self._review_action_service.verify_human_decision_integrity(
                link.human_decision_id
            )
            if verified_decision.decision != HUMAN_DECISION_REWRITE:
                raise RevisionIntegrityError("revision human decision 不是 rewrite")
            action = verified_decision.verified_request.verified_action
            return VerifiedTrigger(
                trigger_type=TRIGGER_TYPE_HUMAN_REWRITE,
                artifact_id=verified_decision.human_decision_id,
                artifact_fingerprint=verified_decision.decision_fingerprint,
                target_section_ids=action_target_section_ids(action.action_payload),
                feedback=derive_human_feedback(
                    action.verified_audit, source_section_id, verified_decision.comment
                ),
            )
        raise RevisionIntegrityError("revision trigger_type invalid")

    # ------------------------------------------------------------ 修订正文校验

    async def _verify_revised_draft(
        self,
        source: VerifiedSourceDraft,
        revised_draft_section_id: UUID,
        revision_input_fingerprint: str,
    ) -> VerifiedRevisedDraft:
        """verify revised draft：身份 / payload contracts / section 指纹重放。"""
        row = await self._load_draft_row(revised_draft_section_id)
        if row is None:
            raise RevisionIntegrityError("revised draft missing")
        self._verify_revised_draft_replay(source, row, revision_input_fingerprint)
        return VerifiedRevisedDraft(
            draft_section_id=row.draft_section_id,
            outline_id=row.outline_id,
            section_id=row.section_id,
            section_order=row.section_order,
            section_type=row.section_type,
            title=row.title,
            section_schema_version=row.section_schema_version,
            writer_name=row.writer_name,
            writer_version=row.writer_version,
            writer_model_id=row.writer_model_id,
            writer_input_fingerprint=row.writer_input_fingerprint,
            section_fingerprint=row.section_fingerprint,
            paragraph_count=len(row.section_payload["paragraphs"]),
        )

    # ------------------------------------------------------------ replay 校验

    def _verify_revised_draft_replay(
        self,
        source: VerifiedSourceDraft,
        row: DraftSectionModel,
        revision_input_fingerprint: str,
    ) -> None:
        """replay 完整性校验：既有 revised draft 与本次派生完全一致（spec M）。

        - 身份 / revision writer / 输入指纹字段逐一对比；
        - `verify_payload_contracts`（同一 section scope 的 scope / Section-aware
          contract / binding / numeric / forbidden / inline alias）；
        - 重算 section_fingerprint（revision input 指纹 + 既有 payload）。任一损坏
          → `RevisionIntegrityError`，**不自动 repair**。
        """
        checks = [
            (row.outline_id, source.outline_id, "outline_id"),
            (row.section_id, source.section_id, "section_id"),
            (row.section_order, source.section_order, "section_order"),
            (row.section_type, source.section_type, "section_type"),
            (row.title, source.title, "title"),
            (
                row.section_schema_version,
                DRAFT_SECTION_SCHEMA_VERSION,
                "section_schema_version",
            ),
            (row.writer_name, REVISION_WRITER_NAME, "writer_name"),
            (row.writer_version, REVISION_WRITER_VERSION, "writer_version"),
            (row.writer_model_id, self._model.model_id, "writer_model_id"),
            (
                row.writer_input_fingerprint,
                revision_input_fingerprint,
                "writer_input_fingerprint",
            ),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise RevisionIntegrityError(f"revised draft {field} mismatch")

        payload = row.section_payload
        verify_payload_contracts(
            pack=source.pack,
            payload=payload,
            total_claim_count=source.total_claim_count,
        )
        recomputed = compute_section_fingerprint(
            writer_input_fingerprint=revision_input_fingerprint,
            section_payload=payload,
        )
        if recomputed != row.section_fingerprint:
            raise RevisionIntegrityError("revised draft section_fingerprint mismatch")

    @staticmethod
    def _verify_link_matches(
        link_row: DraftSectionRevisionModel,
        draft_row: DraftSectionModel,
        request: RevisionRequest,
        state: _RevisionTriggerState,
        revision_input_fingerprint: str,
    ) -> None:
        """replay 校验：既有 revision link 与本次派生完全一致（spec L）。"""
        if link_row.revision_fingerprint != revision_input_fingerprint:
            raise RevisionIntegrityError("revision link fingerprint mismatch")
        if link_row.source_draft_section_id != request.source_draft_section_id:
            raise RevisionIntegrityError("revision link source mismatch")
        if link_row.revised_draft_section_id != draft_row.draft_section_id:
            raise RevisionIntegrityError("revision link revised draft mismatch")
        if link_row.trigger_type != state.trigger_type:
            raise RevisionIntegrityError("revision link trigger_type mismatch")
        if link_row.revision_round != request.revision_round:
            raise RevisionIntegrityError("revision link round mismatch")
        if link_row.revision_schema_version != DRAFT_SECTION_REVISION_SCHEMA_VERSION:
            raise RevisionIntegrityError("revision link schema version mismatch")
        expected_fk = {
            TRIGGER_TYPE_DETERMINISTIC_CHECK: link_row.check_result_id,
            TRIGGER_TYPE_AUDIT_REWRITE: link_row.review_action_id,
            TRIGGER_TYPE_HUMAN_REWRITE: link_row.human_decision_id,
        }[state.trigger_type]
        if expected_fk is None or expected_fk != _trigger_artifact_id(state):
            raise RevisionIntegrityError("revision link trigger artifact mismatch")

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: RevisionRequest) -> None:
        # 构造时已校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if isinstance(request.source_draft_section_id, bool) or not isinstance(
            request.source_draft_section_id, UUID
        ):
            raise RevisionInputError("source_draft_section_id 必须是 UUID")
        if not isinstance(request.trigger, RevisionTrigger):
            raise RevisionInputError("trigger 必须是 RevisionTrigger")
        if (
            isinstance(request.revision_round, bool)
            or not isinstance(request.revision_round, int)
            or request.revision_round < 1
        ):
            raise RevisionInputError("revision_round 必须 >= 1")

    def _compute_input_fingerprint(
        self,
        source: VerifiedSourceDraft,
        trigger: VerifiedTrigger,
    ) -> str:
        """revision input fingerprint（spec K）：source + trigger + feedback + writer。"""
        return compute_revision_input_fingerprint(
            revision_schema_version=DRAFT_SECTION_REVISION_SCHEMA_VERSION,
            source_draft_section_id=source.draft_section_id,
            source_section_fingerprint=source.section_fingerprint,
            outline_fingerprint=source.outline_fingerprint,
            section_id=source.section_id,
            section_order=source.section_order,
            section_type=source.section_type,
            title=source.title,
            claim_fingerprints=[claim.claim_fingerprint for claim in source.claims],
            evidence_fingerprints=[item.evidence_fingerprint for item in source.evidence],
            evidence_claim_relations=_evidence_claim_relations(source.evidence),
            conflicts=_conflict_fingerprint_data(source.conflicts),
            gaps=_gap_fingerprint_data(source.gaps),
            trigger_type=trigger.trigger_type,
            trigger_artifact_id=trigger.artifact_id,
            trigger_artifact_fingerprint=trigger.artifact_fingerprint,
            feedback=[item.to_fingerprint_dict() for item in trigger.feedback],
            writer_name=REVISION_WRITER_NAME,
            writer_version=REVISION_WRITER_VERSION,
            writer_model_id=self._model.model_id,
        )

    async def _call_model(self, pack: RevisionInputPack) -> WriterDecision:
        """调用模型并归一到 WriterDecision（防御性 double-check）。"""
        if self._model is None:
            raise RevisionWriterModelUnavailable()
        raw = await self._model.rewrite(pack)
        if isinstance(raw, WriterDecision):
            return raw
        try:
            return WriterDecision.model_validate(raw)
        except ValidationError as exc:
            raise RevisionWriterMalformedOutput() from exc

    def _revised_draft_model(
        self,
        *,
        source: VerifiedSourceDraft,
        revision_input_fingerprint: str,
        payload: dict,
        section_fingerprint: str,
    ) -> DraftSectionModel:
        """把验证过的修订输出构造成不可变 draft 行（writer=revision rewriter）。"""
        return DraftSectionModel(
            draft_section_id=uuid.uuid4(),
            outline_id=source.outline_id,
            section_id=source.section_id,
            section_order=source.section_order,
            section_type=source.section_type,
            title=source.title,
            section_schema_version=DRAFT_SECTION_SCHEMA_VERSION,
            writer_name=REVISION_WRITER_NAME,
            writer_version=REVISION_WRITER_VERSION,
            writer_model_id=self._model.model_id,
            writer_input_fingerprint=revision_input_fingerprint,
            section_payload=payload,
            section_fingerprint=section_fingerprint,
        )

    def _revision_link_model(
        self,
        *,
        source: VerifiedSourceDraft,
        request: RevisionRequest,
        trigger_state: _RevisionTriggerState,
        draft: DraftSectionModel,
        revision_input_fingerprint: str,
    ) -> DraftSectionRevisionModel:
        """把验证过的修订构造成不可变 revision link 行（exactly one trigger FK）。"""
        return DraftSectionRevisionModel(
            revision_id=uuid.uuid4(),
            source_draft_section_id=source.draft_section_id,
            revised_draft_section_id=draft.draft_section_id,
            revision_round=request.revision_round,
            trigger_type=trigger_state.trigger_type,
            review_action_id=trigger_state.review_action_id,
            check_result_id=trigger_state.check_result_id,
            human_decision_id=trigger_state.human_decision_id,
            revision_schema_version=DRAFT_SECTION_REVISION_SCHEMA_VERSION,
            revision_fingerprint=revision_input_fingerprint,
        )

    async def _load_draft_row(self, draft_section_id: UUID) -> DraftSectionModel | None:
        async with self._sessionmaker() as session:
            return await DraftSectionRepository(session).get_by_id(draft_section_id)

    async def _find_draft(self, fingerprint: str) -> DraftSectionModel | None:
        async with self._sessionmaker() as session:
            repo = DraftSectionRepository(session)
            return await repo.get_by_writer_input_fingerprint(fingerprint)

    async def _find_link(self, fingerprint: str) -> DraftSectionRevisionModel | None:
        async with self._sessionmaker() as session:
            return await RevisionRepository(session).get_by_revision_fingerprint(fingerprint)

    async def _find_link_by_revised_draft(
        self, draft_section_id: UUID
    ) -> DraftSectionRevisionModel | None:
        async with self._sessionmaker() as session:
            repo = RevisionRepository(session)
            return await repo.get_by_revised_draft_section_id(draft_section_id)

    def _result(self, link: DraftSectionRevisionModel, *, replayed: bool) -> RevisionResult:
        return RevisionResult(
            revision_id=link.revision_id,
            source_draft_section_id=link.source_draft_section_id,
            revised_draft_section_id=link.revised_draft_section_id,
            revision_round=link.revision_round,
            trigger_type=link.trigger_type,
            revision_schema_version=link.revision_schema_version,
            revision_fingerprint=link.revision_fingerprint,
            replayed=replayed,
        )


def _trigger_state(trigger: RevisionTrigger) -> _RevisionTriggerState:
    """trigger union → 内部状态（derive_trigger_type 已强约束三选一）。"""
    return _RevisionTriggerState(
        trigger_type=derive_trigger_type(trigger),
        check_result_id=trigger.check_result_id,
        review_action_id=trigger.review_action_id,
        human_decision_id=trigger.human_decision_id,
    )


def _trigger_artifact_id(state: _RevisionTriggerState) -> UUID:
    """trigger 内部状态 → 对应 artifact id（replay FK 比对用）。"""
    return {
        TRIGGER_TYPE_DETERMINISTIC_CHECK: state.check_result_id,
        TRIGGER_TYPE_AUDIT_REWRITE: state.review_action_id,
        TRIGGER_TYPE_HUMAN_REWRITE: state.human_decision_id,
    }[state.trigger_type]


def _paragraph_texts(section_payload: dict) -> tuple[str, ...]:
    """从 persisted section payload 提取段落正文文本（修订的"原文"）。"""
    paragraphs = section_payload.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        raise RevisionIntegrityError("revision source draft payload has no paragraphs")
    return tuple(p["text"] for p in paragraphs)
