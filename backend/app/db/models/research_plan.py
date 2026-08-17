"""SQLAlchemy models for research planning (stage 7A.1): research_plans + routes.

`research_plans` 持久化一次 **immutable** ResearchPlan（schema v1）：Planner 的
语义研究计划。Planner output 一旦持久化即 immutable——tamper 由
`verify_research_plan_integrity` 发现（recompute fingerprint 比对），不 repair。

- 唯一性：**不 UNIQUE(task_id)**（spec G）——planner version / 输入变化后允许
  为同一 task 产生新 immutable Plan；replay 由 `planner_input_fingerprint` UNIQUE
  保证（同 input → 同一行）；
- `plan_fingerprint` = input fingerprint + normalized validated payload（spec H）；
- task_id / company_id FK RESTRICT：上游存在期间本行不会被级联删除；
- plan_payload JSONB 保存 validated ResearchPlanPayload（对象）。

`research_plan_routes` 持久化一次 RoutePlan（spec K）：当时 Router 对每个 need
的 deterministic route decision——**保证 registry 变化后仍可审计当时 route
decision**（不为了少一张表牺牲 provenance）。UNIQUE(research_plan_id,
router_version)：同 plan 同 router version 至多一行（同 plan 重放 → 同一行）。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ResearchPlanModel(Base):
    __tablename__ = "research_plans"
    __table_args__ = (
        CheckConstraint(
            f"planner_input_fingerprint {_SHA256_CHECK}",
            name="ck_research_plans_input_fingerprint",
        ),
        CheckConstraint(
            f"plan_fingerprint {_SHA256_CHECK}",
            name="ck_research_plans_plan_fingerprint",
        ),
        CheckConstraint(
            "plan_schema_version >= 1",
            name="ck_research_plans_plan_schema_version",
        ),
        CheckConstraint(
            "planner_version >= 1",
            name="ck_research_plans_planner_version",
        ),
        CheckConstraint(
            "btrim(planner_name) <> ''",
            name="ck_research_plans_planner_name_not_blank",
        ),
        CheckConstraint(
            "btrim(model_id) <> ''",
            name="ck_research_plans_model_id_not_blank",
        ),
        CheckConstraint(
            "jsonb_typeof(plan_payload) = 'object'",
            name="ck_research_plans_plan_payload_object",
        ),
        # v2 行（plan_schema_version >= 2）必须携带 creation-time input snapshot
        # （spec A）：payload + schema_version 双 NOT NULL。v1 行允许 NULL，不回填。
        CheckConstraint(
            "NOT (plan_schema_version >= 2) OR "
            "(planner_input_payload IS NOT NULL AND planner_input_schema_version >= 1)",
            name="ck_research_plans_v2_input_snapshot",
        ),
        CheckConstraint(
            "(planner_input_payload IS NULL) OR (jsonb_typeof(planner_input_payload) = 'object')",
            name="ck_research_plans_planner_input_payload_object",
        ),
        UniqueConstraint(
            "planner_input_fingerprint",
            name="uq_research_plans_planner_input_fingerprint",
        ),
        UniqueConstraint("plan_fingerprint", name="uq_research_plans_plan_fingerprint"),
        Index("ix_research_plans_task_id", "task_id"),
        Index("ix_research_plans_company_id", "company_id"),
        Index("ix_research_plans_created_at", "created_at"),
    )

    research_plan_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_name: Mapped[str] = mapped_column(String(64), nullable=False)
    planner_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    planner_input_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # creation-time PlannerInputSnapshot（v2 行必填，v1 行 NULL——不回填 legacy）。
    planner_input_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    planner_input_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Planner reliability telemetry (P1: deterministic fallback / repair counters).
    # Internal-only; never exposed to users.
    planner_fallback_used: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    planner_repair_attempts: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )


class ResearchPlanRouteModel(Base):
    __tablename__ = "research_plan_routes"
    __table_args__ = (
        CheckConstraint(
            f"route_fingerprint {_SHA256_CHECK}",
            name="ck_research_plan_routes_route_fingerprint",
        ),
        CheckConstraint(
            "route_schema_version >= 1",
            name="ck_research_plan_routes_route_schema_version",
        ),
        CheckConstraint(
            "router_version >= 1",
            name="ck_research_plan_routes_router_version",
        ),
        CheckConstraint(
            "btrim(router_name) <> ''",
            name="ck_research_plan_routes_router_name_not_blank",
        ),
        CheckConstraint(
            "jsonb_typeof(route_payload) = 'object'",
            name="ck_research_plan_routes_route_payload_object",
        ),
        UniqueConstraint("route_fingerprint", name="uq_research_plan_routes_route_fingerprint"),
        UniqueConstraint(
            "research_plan_id",
            "router_version",
            name="uq_research_plan_routes_plan_router_version",
        ),
        Index("ix_research_plan_routes_research_plan_id", "research_plan_id"),
        Index("ix_research_plan_routes_created_at", "created_at"),
    )

    route_plan_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    research_plan_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_plans.research_plan_id", ondelete="RESTRICT"),
        nullable=False,
    )
    route_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    router_name: Mapped[str] = mapped_column(String(64), nullable=False)
    router_version: Mapped[int] = mapped_column(Integer, nullable=False)
    route_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    route_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
