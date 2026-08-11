"""Report review routing + human confirmation service (stage 5E.1, spec E/N/O).

流程（短 verify session → 纯函数派生 → 短事务，镜像 ReportAuditService）：
1. `create_or_get_action(audit_id)`：第一步必须
   `ReportAuditService.verify_audit_integrity(audit_id)` → `VerifiedReportAudit`
   （**只消费 VerifiedReportAudit**，spec E）→ `derive_action_type` /
   `derive_action_payload`（spec F/G，caller 不提供字段）→ `compute_action_fingerprint`
   → 短事务 create_or_get（ON CONFLICT(audit_id) DO NOTHING，无进程锁）；输家
   回查既有行，fingerprint 与派生不同 → `ReviewActionIntegrityError`；
2. `create_or_get_human_request(review_action_id)`：先 `verify_review_action_integrity`
   → 必须 action_type=human_review（spec J，否则 `ReviewRequestNotHumanReview`）→
   `derive_human_request_payload`（只存 IDs + issue summaries）→
   `compute_request_fingerprint` → create_or_get（ON CONFLICT(review_action_id)）；
3. `resolve_human_request(human_request_id, decision, comment=None)`：先校验
   decision 枚举 + normalize comment（spec K）→ `verify_human_request_integrity`
   → `compute_decision_fingerprint` → create_or_get（ON CONFLICT(human_request_id)）；
   输家回查：同 decision/comment → replay，不同 → `HumanReviewAlreadyResolved`
   （**不覆盖历史**，spec K/L）。

**公共 read-side（spec N，逐层重放，不 repair）**：
- `verify_review_action_integrity(id)`：重 verify Audit → 重派生 action_type /
  payload → 重算 fingerprint → 与 persisted 对比；
- `verify_human_request_integrity(id)`：重 verify action → 重派生 request payload
  → 重算 fingerprint → 对比；
- `verify_human_decision_integrity(id)`：重 verify request → 核对 immutable
  fields（decision / comment）→ 重算 fingerprint → 对比。

**不创建 Audit / Report 之外的行**；不接 LangGraph；不 rewrite / 不 research /
不调 Retrieval / Chroma / tools / web（spec A/B）。人工 decision **不修改** Audit
route / issues / Report（spec L，新的 immutable artifact）。
"""

import json
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.audit.service import ReportAuditService
from app.db.models.review_action import (
    HumanReviewDecisionModel,
    HumanReviewRequestModel,
    ReportReviewActionModel,
)
from app.review.contracts import (
    ACTION_TYPE_HUMAN_REVIEW,
    HUMAN_DECISIONS,
    HUMAN_REVIEW_DECISION_SCHEMA_VERSION,
    HUMAN_REVIEW_REQUEST_SCHEMA_VERSION,
    REVIEW_ACTION_SCHEMA_VERSION,
    HumanReviewDecisionResult,
    HumanReviewRequestResult,
    ReviewActionResult,
    VerifiedHumanReviewDecision,
    VerifiedHumanReviewRequest,
    VerifiedReviewAction,
    compute_action_fingerprint,
    compute_decision_fingerprint,
    compute_request_fingerprint,
)
from app.review.derive import (
    derive_action_payload,
    derive_action_type,
    derive_human_request_payload,
    normalize_comment,
)
from app.review.errors import (
    HumanReviewAlreadyResolved,
    HumanReviewDecisionIntegrityError,
    HumanReviewDecisionNotFound,
    HumanReviewRequestIntegrityError,
    HumanReviewRequestNotFound,
    ReviewActionIntegrityError,
    ReviewActionNotFound,
    ReviewError,
    ReviewInputError,
    ReviewPersistenceFailed,
    ReviewRequestNotHumanReview,
)
from app.review.repository import (
    HumanReviewDecisionRepository,
    HumanReviewRequestRepository,
    ReviewActionRepository,
)


class ReviewActionService:
    """Review Routing + Human Confirmation：VerifiedReportAudit → 确定性控制层 artifact。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        audit_service: ReportAuditService,
    ) -> None:
        """audit_service 显式注入（verify_audit_integrity 是唯一 Audit 消费入口）。

        构造不触发模型调用；本阶段 0 LLM / 0 rewrite / 0 research / 0 LangGraph。
        """
        self._sessionmaker = sessionmaker
        self._audit_service = audit_service

    # ------------------------------------------------------------------ create / get

    async def create_or_get_action(self, audit_id: UUID) -> ReviewActionResult:
        """VerifiedReportAudit → deterministic ReviewActionPlan（create or replay）。"""
        # 1. 必须通过公共 verify（spec E：只消费 VerifiedReportAudit）。
        verified = await self._audit_service.verify_audit_integrity(audit_id)

        # 2. 纯函数派生 action_type / payload / fingerprint（spec F/G/M）。
        action_type = derive_action_type(verified)
        payload = derive_action_payload(verified, action_type)
        action_fingerprint = self._action_fingerprint(verified, action_type, payload)

        # 3. 短事务 create_or_get（ON CONFLICT(audit_id)，无进程锁）。
        expected = ReportReviewActionModel(
            review_action_id=uuid.uuid4(),
            audit_id=verified.audit_id,
            report_id=verified.report_id,
            action_schema_version=REVIEW_ACTION_SCHEMA_VERSION,
            action_type=action_type,
            action_payload=payload,
            action_fingerprint=action_fingerprint,
        )
        async with self._sessionmaker() as session:
            try:
                row, was_created = await ReviewActionRepository(session).create_or_get(expected)
                if not was_created:
                    # 并发输家 / 已存在结果：同 audit 只能 1 个 action，指纹必须与
                    # 本次派生一致（不同只来自 tamper）。
                    self._assert_action_matches(row, verified, action_type, payload)
                await session.commit()
            except ReviewError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReviewPersistenceFailed() from exc

        return ReviewActionResult(
            review_action_id=row.review_action_id,
            audit_id=row.audit_id,
            report_id=row.report_id,
            action_schema_version=row.action_schema_version,
            action_type=row.action_type,
            action_payload=dict(row.action_payload),
            action_fingerprint=row.action_fingerprint,
            replayed=not was_created,
        )

    async def create_or_get_human_request(self, review_action_id: UUID) -> HumanReviewRequestResult:
        """human_review action → human review request（create or replay）。"""
        # 1. 先 verify action（spec N 逐层重放）。
        verified_action = await self.verify_review_action_integrity(review_action_id)
        if verified_action.action_type != ACTION_TYPE_HUMAN_REVIEW:
            raise ReviewRequestNotHumanReview()

        # 2. 纯函数派生 request payload / fingerprint（spec J/M）。
        request_payload = derive_human_request_payload(
            verified_action.verified_audit,
            verified_action.action_type,
            verified_action.action_payload,
        )
        request_fingerprint = compute_request_fingerprint(
            request_schema_version=HUMAN_REVIEW_REQUEST_SCHEMA_VERSION,
            review_action_id=verified_action.review_action_id,
            action_fingerprint=verified_action.action_fingerprint,
            request_payload=request_payload,
        )

        # 3. 短事务 create_or_get（ON CONFLICT(review_action_id)，无进程锁）。
        expected = HumanReviewRequestModel(
            human_request_id=uuid.uuid4(),
            review_action_id=verified_action.review_action_id,
            request_schema_version=HUMAN_REVIEW_REQUEST_SCHEMA_VERSION,
            request_payload=request_payload,
            request_fingerprint=request_fingerprint,
        )
        async with self._sessionmaker() as session:
            try:
                row, was_created = await HumanReviewRequestRepository(session).create_or_get(
                    expected
                )
                if not was_created:
                    # 并发输家：同 action 只能 1 个 request，指纹必须与本次派生一致。
                    if row.request_fingerprint != request_fingerprint:
                        raise HumanReviewRequestIntegrityError(
                            "human review request fingerprint mismatch"
                        )
                await session.commit()
            except ReviewError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReviewPersistenceFailed() from exc

        return HumanReviewRequestResult(
            human_request_id=row.human_request_id,
            review_action_id=row.review_action_id,
            request_schema_version=row.request_schema_version,
            request_payload=dict(row.request_payload),
            request_fingerprint=row.request_fingerprint,
            replayed=not was_created,
        )

    async def resolve_human_request(
        self,
        human_request_id: UUID,
        decision: str,
        comment: str | None = None,
    ) -> HumanReviewDecisionResult:
        """人工裁决（approve/rewrite/research/cancel）：create or replay。

        同完全相同 decision/comment → replay；不同 decision/comment →
        `HumanReviewAlreadyResolved`（**不覆盖历史**，spec K）。人工决定不修改
        Audit route / issues / Report（spec L）。
        """
        # 1. 先校验输入（decision 枚举 + comment normalize，spec K）。
        if not isinstance(decision, str) or decision not in HUMAN_DECISIONS:
            raise ReviewInputError("decision 必须是 approve/rewrite/research/cancel")
        normalized_comment = normalize_comment(comment)

        # 2. verify request（spec N 逐层重放）。
        verified_request = await self.verify_human_request_integrity(human_request_id)

        # 3. 纯函数派生 decision fingerprint（spec M）。
        decision_fingerprint = compute_decision_fingerprint(
            decision_schema_version=HUMAN_REVIEW_DECISION_SCHEMA_VERSION,
            human_request_id=verified_request.human_request_id,
            request_fingerprint=verified_request.request_fingerprint,
            decision=decision,
            comment=normalized_comment,
        )

        # 4. 短事务 create_or_get（ON CONFLICT(human_request_id)，无进程锁）。
        expected = HumanReviewDecisionModel(
            human_decision_id=uuid.uuid4(),
            human_request_id=verified_request.human_request_id,
            decision_schema_version=HUMAN_REVIEW_DECISION_SCHEMA_VERSION,
            decision=decision,
            comment=normalized_comment,
            decided_at=datetime.now(UTC),
            decision_fingerprint=decision_fingerprint,
        )
        async with self._sessionmaker() as session:
            try:
                row, was_created = await HumanReviewDecisionRepository(session).create_or_get(
                    expected
                )
                if not was_created:
                    # 并发输家：同 request 只能 1 个 decision；同 decision/comment →
                    # replay，不同 → AlreadyResolved。
                    if row.decision != decision or row.comment != normalized_comment:
                        raise HumanReviewAlreadyResolved()
                await session.commit()
            except ReviewError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReviewPersistenceFailed() from exc

        return HumanReviewDecisionResult(
            human_decision_id=row.human_decision_id,
            human_request_id=row.human_request_id,
            decision_schema_version=row.decision_schema_version,
            decision=row.decision,
            comment=row.comment,
            decided_at=row.decided_at,
            decision_fingerprint=row.decision_fingerprint,
            replayed=not was_created,
        )

    # ------------------------------------------------------------------ verify integrity

    async def verify_review_action_integrity(self, review_action_id: UUID) -> VerifiedReviewAction:
        """public read-only 校验（spec N）：重 verify Audit → 重派生 action →
        重算 fingerprint → 与 persisted 对比。任一损坏 →
        `ReviewActionIntegrityError`（**不自动 repair**）。"""
        async with self._sessionmaker() as session:
            row = await ReviewActionRepository(session).get_by_id(review_action_id)
        if row is None:
            raise ReviewActionNotFound()

        verified = await self._audit_service.verify_audit_integrity(row.audit_id)
        action_type = derive_action_type(verified)
        payload = derive_action_payload(verified, action_type)
        self._assert_action_matches(row, verified, action_type, payload)

        return VerifiedReviewAction(
            review_action_id=row.review_action_id,
            audit_id=row.audit_id,
            report_id=row.report_id,
            action_schema_version=row.action_schema_version,
            action_type=row.action_type,
            action_payload=dict(row.action_payload),
            action_fingerprint=row.action_fingerprint,
            created_at=row.created_at,
            verified_audit=verified,
        )

    async def verify_human_request_integrity(
        self, human_request_id: UUID
    ) -> VerifiedHumanReviewRequest:
        """public read-only 校验（spec N）：重 verify action → 重派生 request
        payload → 重算 fingerprint → 与 persisted 对比。"""
        async with self._sessionmaker() as session:
            row = await HumanReviewRequestRepository(session).get_by_id(human_request_id)
        if row is None:
            raise HumanReviewRequestNotFound()

        verified_action = await self.verify_review_action_integrity(row.review_action_id)
        if verified_action.action_type != ACTION_TYPE_HUMAN_REVIEW:
            raise HumanReviewRequestIntegrityError(
                "human review request action_type is not human_review"
            )
        recomputed_payload = derive_human_request_payload(
            verified_action.verified_audit,
            verified_action.action_type,
            verified_action.action_payload,
        )
        recomputed_fingerprint = compute_request_fingerprint(
            request_schema_version=HUMAN_REVIEW_REQUEST_SCHEMA_VERSION,
            review_action_id=verified_action.review_action_id,
            action_fingerprint=verified_action.action_fingerprint,
            request_payload=recomputed_payload,
        )
        if not self._same_payload(recomputed_payload, row.request_payload):
            raise HumanReviewRequestIntegrityError("human review request payload mismatch")
        if row.request_schema_version != HUMAN_REVIEW_REQUEST_SCHEMA_VERSION:
            raise HumanReviewRequestIntegrityError("human review request schema version mismatch")
        if recomputed_fingerprint != row.request_fingerprint:
            raise HumanReviewRequestIntegrityError("human review request fingerprint mismatch")

        return VerifiedHumanReviewRequest(
            human_request_id=row.human_request_id,
            review_action_id=row.review_action_id,
            request_schema_version=row.request_schema_version,
            request_payload=dict(row.request_payload),
            request_fingerprint=row.request_fingerprint,
            created_at=row.created_at,
            verified_action=verified_action,
        )

    async def verify_human_decision_integrity(
        self, human_decision_id: UUID
    ) -> VerifiedHumanReviewDecision:
        """public read-only 校验（spec N）：重 verify request → 核对 immutable
        fields（decision / comment）→ 重算 fingerprint → 与 persisted 对比。"""
        async with self._sessionmaker() as session:
            row = await HumanReviewDecisionRepository(session).get_by_id(human_decision_id)
        if row is None:
            raise HumanReviewDecisionNotFound()

        verified_request = await self.verify_human_request_integrity(row.human_request_id)
        if row.decision_schema_version != HUMAN_REVIEW_DECISION_SCHEMA_VERSION:
            raise HumanReviewDecisionIntegrityError("human review decision schema version mismatch")
        if row.decision not in HUMAN_DECISIONS:
            raise HumanReviewDecisionIntegrityError("human review decision invalid")
        # 校验 immutable fields（decision / comment 必须与 fingerprint 输入一致）。
        if row.comment != normalize_comment(row.comment):
            raise HumanReviewDecisionIntegrityError("human review decision comment not normalized")
        recomputed_fingerprint = compute_decision_fingerprint(
            decision_schema_version=HUMAN_REVIEW_DECISION_SCHEMA_VERSION,
            human_request_id=verified_request.human_request_id,
            request_fingerprint=verified_request.request_fingerprint,
            decision=row.decision,
            comment=row.comment,
        )
        if recomputed_fingerprint != row.decision_fingerprint:
            raise HumanReviewDecisionIntegrityError("human review decision fingerprint mismatch")

        return VerifiedHumanReviewDecision(
            human_decision_id=row.human_decision_id,
            human_request_id=row.human_request_id,
            decision_schema_version=row.decision_schema_version,
            decision=row.decision,
            comment=row.comment,
            decided_at=row.decided_at,
            decision_fingerprint=row.decision_fingerprint,
            created_at=row.created_at,
            verified_request=verified_request,
        )

    # ------------------------------------------------------------------ 内部

    def _assert_action_matches(
        self,
        row: ReportReviewActionModel,
        verified,
        action_type: str,
        payload: dict,
    ) -> None:
        """persisted action 与派生必须完全一致（replay 语义；同 audit 只能 1 个）。"""
        if row.action_schema_version != REVIEW_ACTION_SCHEMA_VERSION:
            raise ReviewActionIntegrityError("review action schema version mismatch")
        if row.action_type != action_type:
            raise ReviewActionIntegrityError("review action type mismatch")
        if not self._same_payload(payload, row.action_payload):
            raise ReviewActionIntegrityError("review action payload mismatch")
        recomputed = self._action_fingerprint(verified, action_type, payload)
        if recomputed != row.action_fingerprint:
            raise ReviewActionIntegrityError("review action fingerprint mismatch")

    def _action_fingerprint(self, verified, action_type: str, payload: dict) -> str:
        return compute_action_fingerprint(
            action_schema_version=REVIEW_ACTION_SCHEMA_VERSION,
            audit_id=verified.audit_id,
            audit_fingerprint=verified.audit_fingerprint,
            report_id=verified.report_id,
            report_fingerprint=verified.verified_report.report_fingerprint,
            action_type=action_type,
            action_payload=payload,
        )

    @staticmethod
    def _same_payload(left: dict, right: dict) -> bool:
        """JSONB 规范化比较（忽略 None/空列表等边界差异）。"""
        return json.dumps(
            left, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ) == json.dumps(right, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
