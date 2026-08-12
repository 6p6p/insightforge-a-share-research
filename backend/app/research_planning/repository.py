"""Research planning data access (stage 7A.1): research_plans + research_plan_routes.

事务由调用方（ResearchPlanningService / ResearchPreparationService）协调；
本模块只做短查询与 create_or_get（ON CONFLICT replay 语义，无 Python 进程锁）。
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.research_plan import (
    ResearchPlanModel,
    ResearchPlanRouteModel,
)


class ResearchPlanRepository:
    """research_plans 访问（research_plan_id PK / input fingerprint 唯一）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, research_plan_id: UUID) -> ResearchPlanModel | None:
        result = await self._session.execute(
            select(ResearchPlanModel).where(ResearchPlanModel.research_plan_id == research_plan_id)
        )
        return result.scalar_one_or_none()

    async def get_by_input_fingerprint(self, fingerprint: str) -> ResearchPlanModel | None:
        result = await self._session.execute(
            select(ResearchPlanModel).where(
                ResearchPlanModel.planner_input_fingerprint == fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def get_by_plan_fingerprint(self, fingerprint: str) -> ResearchPlanModel | None:
        result = await self._session.execute(
            select(ResearchPlanModel).where(ResearchPlanModel.plan_fingerprint == fingerprint)
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, plan: ResearchPlanModel) -> tuple[ResearchPlanModel, bool]:
        """INSERT ... ON CONFLICT(planner_input_fingerprint) DO NOTHING RETURNING。

        同 input（相同 input fingerprint）→ replay 同一行；并发最终只 1 行。
        """
        # research_plan_id 的 default=uuid.uuid4 是 Python-side：模型构造时不赋值（None），
        # 逐列取值会显式传 None 绕过默认。排除 PK，让 Core INSERT 应用列默认。
        excluded = {"created_at", "research_plan_id"}
        values = {
            column.key: getattr(plan, column.key)
            for column in ResearchPlanModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ResearchPlanModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ResearchPlanModel.planner_input_fingerprint])
            .returning(ResearchPlanModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_input_fingerprint(plan.planner_input_fingerprint)
        if existing is None:
            raise RuntimeError("research plan conflict without existing row")
        return existing, False

    async def count(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(ResearchPlanModel))
        return int(result.scalar_one())


class ResearchPlanRouteRepository:
    """research_plan_routes 访问（plan_id + router_version 唯一）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, route_plan_id: UUID) -> ResearchPlanRouteModel | None:
        result = await self._session.execute(
            select(ResearchPlanRouteModel).where(
                ResearchPlanRouteModel.route_plan_id == route_plan_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_plan_and_router_version(
        self, research_plan_id: UUID, router_version: int
    ) -> ResearchPlanRouteModel | None:
        result = await self._session.execute(
            select(ResearchPlanRouteModel).where(
                ResearchPlanRouteModel.research_plan_id == research_plan_id,
                ResearchPlanRouteModel.router_version == router_version,
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, route: ResearchPlanRouteModel
    ) -> tuple[ResearchPlanRouteModel, bool]:
        """INSERT ... ON CONFLICT(research_plan_id, router_version) DO NOTHING RETURNING。"""
        # 同 create_or_get：route_plan_id 的 Python-side default 不会逐列取值时触发，排除 PK。
        excluded = {"created_at", "route_plan_id"}
        values = {
            column.key: getattr(route, column.key)
            for column in ResearchPlanRouteModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ResearchPlanRouteModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    ResearchPlanRouteModel.research_plan_id,
                    ResearchPlanRouteModel.router_version,
                ]
            )
            .returning(ResearchPlanRouteModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_plan_and_router_version(
            route.research_plan_id, route.router_version
        )
        if existing is None:
            raise RuntimeError("research plan route conflict without existing row")
        return existing, False

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(ResearchPlanRouteModel)
        )
        return int(result.scalar_one())
