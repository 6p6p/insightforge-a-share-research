"""Backflow manual closure data access (P0).

`backflow_human_review_requests` / `backflow_human_review_decisions`：

- 一个 orchestration 至多一个 closure request（UNIQUE(orchestration_id)），
  create_or_get（ON CONFLICT(o orchestration_id) DO NOTHING）；
- 一个 request 至多一个 immutable decision（UNIQUE(backflow_human_request_id)），
  replay 语义（同 decision/comment → 输家回查；不同由服务层判定
  BackflowReviewAlreadyResolved）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.research_backflow import (
    BackflowHumanReviewDecisionModel,
    BackflowHumanReviewRequestModel,
)


class BackflowReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, backflow_human_request_id: UUID):
        result = await self._session.execute(
            select(BackflowHumanReviewRequestModel).where(
                BackflowHumanReviewRequestModel.backflow_human_request_id
                == backflow_human_request_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_orchestration(self, orchestration_id: UUID):
        result = await self._session.execute(
            select(BackflowHumanReviewRequestModel).where(
                BackflowHumanReviewRequestModel.orchestration_id == orchestration_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, expected: BackflowHumanReviewRequestModel):
        excluded = {"created_at", "backflow_human_request_id"}
        values = {
            column.key: getattr(expected, column.key)
            for column in BackflowHumanReviewRequestModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(BackflowHumanReviewRequestModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    BackflowHumanReviewRequestModel.orchestration_id,
                ]
            )
            .returning(BackflowHumanReviewRequestModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_orchestration(expected.orchestration_id)
        return existing, False

    async def get_decision_by_request(self, backflow_human_request_id: UUID):
        result = await self._session.execute(
            select(BackflowHumanReviewDecisionModel).where(
                BackflowHumanReviewDecisionModel.backflow_human_request_id
                == backflow_human_request_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get_decision(self, expected: BackflowHumanReviewDecisionModel):
        excluded = {"decided_at", "backflow_human_decision_id"}
        values = {
            column.key: getattr(expected, column.key)
            for column in BackflowHumanReviewDecisionModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(BackflowHumanReviewDecisionModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    BackflowHumanReviewDecisionModel.backflow_human_request_id,
                ]
            )
            .returning(BackflowHumanReviewDecisionModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_decision_by_request(expected.backflow_human_request_id)
        return existing, False
