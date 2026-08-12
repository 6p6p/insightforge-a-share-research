"""Data access for research backflow contract (stage 5E.2B).

三表各自按业务唯一键做 **create_or_get**（ON CONFLICT DO NOTHING 无目标索引，
镜像 `ReviewActionRepository`）：
- `research_backflow_requests` 按 `(source_stage5_run_id)` UNIQUE——一个 Stage 5
  run 至多 1 行（request_fingerprint 也 UNIQUE，并发同 run 的派生必然同指纹 →
  1 行）；
- `research_backflow_fulfillments` 按 `(research_request_id)` UNIQUE——一个请求
  至多 1 个 fulfillment（fulfillment_fingerprint 也 UNIQUE）；
- `research_backflow_plans` 按 `(research_backflow_request_id)` UNIQUE——一个
  request 至多 1 个补充计划（plan_fingerprint 也 UNIQUE）。

**无 Python 进程锁**；不允许 update API。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.research_backflow import (
    ResearchBackflowFulfillmentModel,
    ResearchBackflowPlanModel,
    ResearchBackflowRequestModel,
)
from app.db.models.workflow_run import WorkflowRunModel


class ResearchBackflowRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_request_by_id(
        self, research_request_id: UUID
    ) -> ResearchBackflowRequestModel | None:
        result = await self._session.execute(
            select(ResearchBackflowRequestModel).where(
                ResearchBackflowRequestModel.research_request_id == research_request_id
            )
        )
        return result.scalar_one_or_none()

    async def get_request_by_run_id(self, run_id: UUID) -> ResearchBackflowRequestModel | None:
        result = await self._session.execute(
            select(ResearchBackflowRequestModel).where(
                ResearchBackflowRequestModel.source_stage5_run_id == run_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get_request(
        self, request: ResearchBackflowRequestModel
    ) -> tuple[ResearchBackflowRequestModel, bool]:
        """同 source run 并发只能有 1 行（`(source_stage5_run_id)` UNIQUE）。

        输家回查既有行（created=False）并复用（replay 语义）。run immutable + derive
        deterministic → 并发派生的 fingerprint 必然相同；**无 Python 进程锁**。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(request, column.key)
            for column in ResearchBackflowRequestModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ResearchBackflowRequestModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ResearchBackflowRequestModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_request_by_run_id(request.source_stage5_run_id)
        if existing is None:
            raise RuntimeError("research backflow request conflict without existing row")
        return existing, False

    async def get_plan_by_request_id(
        self, research_backflow_request_id: UUID
    ) -> ResearchBackflowPlanModel | None:
        result = await self._session.execute(
            select(ResearchBackflowPlanModel).where(
                ResearchBackflowPlanModel.research_backflow_request_id
                == research_backflow_request_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get_plan(
        self, plan: ResearchBackflowPlanModel
    ) -> tuple[ResearchBackflowPlanModel, bool]:
        """同 request 并发只能有 1 行（`(research_backflow_request_id)` UNIQUE）。

        输家回查既有行（created=False）并复用（replay 语义）。request immutable +
        derive deterministic → 并发派生的 fingerprint 必然相同；**无 Python 进程锁**。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(plan, column.key)
            for column in ResearchBackflowPlanModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ResearchBackflowPlanModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ResearchBackflowPlanModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_plan_by_request_id(plan.research_backflow_request_id)
        if existing is None:
            raise RuntimeError("research backflow plan conflict without existing row")
        return existing, False

    async def get_fulfillment_by_id(
        self, fulfillment_id: UUID
    ) -> ResearchBackflowFulfillmentModel | None:
        result = await self._session.execute(
            select(ResearchBackflowFulfillmentModel).where(
                ResearchBackflowFulfillmentModel.fulfillment_id == fulfillment_id
            )
        )
        return result.scalar_one_or_none()

    async def get_fulfillment_by_request_id(
        self, research_request_id: UUID
    ) -> ResearchBackflowFulfillmentModel | None:
        result = await self._session.execute(
            select(ResearchBackflowFulfillmentModel).where(
                ResearchBackflowFulfillmentModel.research_request_id == research_request_id
            )
        )
        return result.scalar_one_or_none()

    async def list_fulfillments_by_new_synthesis_result_for_task(
        self,
        new_synthesis_result_id: UUID,
        task_id: UUID,
    ) -> list[ResearchBackflowFulfillmentModel]:
        """**task-scoped** 按新综合 result 反查 fulfillment（finalize 后的
        continuation run 用，Gate0-C）。

        Stage 6B.1 spec K：finalize run 的 checkpoint 无 `research_request_id`
        （该 channel 只在 research 路由时写入），但 canonical synthesis 就是
        fulfillment 的 `new_synthesis_result_id` → 由此反查 request+fulfillment。

        `new_synthesis_result_id` 全表**不唯一**，不能做全局命中：必须经
        fulfillment → request（`source_stage5_run_id`）→ `workflow_runs.task_id`
        回到当前任务域再匹配。0 行 → 无 backflow；>1 行 → 调用方报完整性失败
        （投影口径不唯一，绝不静默选一行）。
        """
        result = await self._session.execute(
            select(ResearchBackflowFulfillmentModel)
            .join(
                ResearchBackflowRequestModel,
                ResearchBackflowRequestModel.research_request_id
                == ResearchBackflowFulfillmentModel.research_request_id,
            )
            .join(
                WorkflowRunModel,
                WorkflowRunModel.run_id == ResearchBackflowRequestModel.source_stage5_run_id,
            )
            .where(
                ResearchBackflowFulfillmentModel.new_synthesis_result_id == new_synthesis_result_id,
                WorkflowRunModel.task_id == task_id,
            )
        )
        return list(result.scalars().all())

    async def create_or_get_fulfillment(
        self, fulfillment: ResearchBackflowFulfillmentModel
    ) -> tuple[ResearchBackflowFulfillmentModel, bool]:
        """同 request 并发只能有 1 行（`(research_request_id)` UNIQUE）。

        输家回查既有行由 service 判断 replay（同 result）或
        `ResearchBackflowAlreadyFulfilled`（不同 result）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(fulfillment, column.key)
            for column in ResearchBackflowFulfillmentModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ResearchBackflowFulfillmentModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ResearchBackflowFulfillmentModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_fulfillment_by_request_id(fulfillment.research_request_id)
        if existing is None:
            raise RuntimeError("research backflow fulfillment conflict without existing row")
        return existing, False
