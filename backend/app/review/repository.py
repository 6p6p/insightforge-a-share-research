"""Data access for report review routing + human confirmation (stage 5E.1).

三张表各自按业务唯一键做 **create_or_get**（ON CONFLICT DO NOTHING 无目标索引，
镜像 `ReportAuditRepository`）：
- `report_review_actions` 按 `(audit_id)` UNIQUE——同一 immutable Audit 只能有 1
  行（fingerprint 也 UNIQUE，并发同 audit 的派生必然同指纹 → 1 行）；
- `human_review_requests` 按 `(review_action_id)` UNIQUE——一个 human_review
  action 至多 1 行；
- `human_review_decisions` 按 `(human_request_id)` UNIQUE——一个 request 至多 1 个
  immutable decision（decision_fingerprint 也 UNIQUE）。

**无 Python 进程锁**；不允许 update API。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.review_action import (
    HumanReviewDecisionModel,
    HumanReviewRequestModel,
    ReportReviewActionModel,
)


class ReviewActionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, review_action_id: UUID) -> ReportReviewActionModel | None:
        result = await self._session.execute(
            select(ReportReviewActionModel).where(
                ReportReviewActionModel.review_action_id == review_action_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_audit_id(self, audit_id: UUID) -> ReportReviewActionModel | None:
        result = await self._session.execute(
            select(ReportReviewActionModel).where(ReportReviewActionModel.audit_id == audit_id)
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, action: ReportReviewActionModel
    ) -> tuple[ReportReviewActionModel, bool]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING（无目标索引）。

        并发下同一 audit 只能有 1 行：输家回查既有行（created=False）并复用
        （replay 语义）。audit immutable + derive deterministic → 并发派生的
        fingerprint 必然相同；**无 Python 进程锁**。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(action, column.key)
            for column in ReportReviewActionModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ReportReviewActionModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ReportReviewActionModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_audit_id(action.audit_id)
        if existing is None:
            raise RuntimeError("review action conflict without existing row")
        return existing, False


class HumanReviewRequestRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, human_request_id: UUID) -> HumanReviewRequestModel | None:
        result = await self._session.execute(
            select(HumanReviewRequestModel).where(
                HumanReviewRequestModel.human_request_id == human_request_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_review_action_id(
        self, review_action_id: UUID
    ) -> HumanReviewRequestModel | None:
        result = await self._session.execute(
            select(HumanReviewRequestModel).where(
                HumanReviewRequestModel.review_action_id == review_action_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, request: HumanReviewRequestModel
    ) -> tuple[HumanReviewRequestModel, bool]:
        """同 review_action 并发只能有 1 行（`(review_action_id)` UNIQUE）。"""
        excluded = {"created_at"}
        values = {
            column.key: getattr(request, column.key)
            for column in HumanReviewRequestModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(HumanReviewRequestModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(HumanReviewRequestModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_review_action_id(request.review_action_id)
        if existing is None:
            raise RuntimeError("human review request conflict without existing row")
        return existing, False


class HumanReviewDecisionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, human_decision_id: UUID) -> HumanReviewDecisionModel | None:
        result = await self._session.execute(
            select(HumanReviewDecisionModel).where(
                HumanReviewDecisionModel.human_decision_id == human_decision_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_human_request_id(
        self, human_request_id: UUID
    ) -> HumanReviewDecisionModel | None:
        result = await self._session.execute(
            select(HumanReviewDecisionModel).where(
                HumanReviewDecisionModel.human_request_id == human_request_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, decision: HumanReviewDecisionModel
    ) -> tuple[HumanReviewDecisionModel, bool]:
        """同 request 并发只能有 1 行（`(human_request_id)` UNIQUE）。

        输家回查既有行由 service 判断 replay（同 decision/comment）或
        `HumanReviewAlreadyResolved`（不同）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(decision, column.key)
            for column in HumanReviewDecisionModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(HumanReviewDecisionModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(HumanReviewDecisionModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_human_request_id(decision.human_request_id)
        if existing is None:
            raise RuntimeError("human review decision conflict without existing row")
        return existing, False
