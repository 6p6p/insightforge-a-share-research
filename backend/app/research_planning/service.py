"""Research planning service (stage 7A.1 spec G/H/I): create + replay + verify.

- **create_plan(task_id)**：从 ResearchTask 派生 planner 输入（research_question /
  analysis_as_of / CompanyIdentitySnapshot）→ input fingerprint → replay
  （同 input → 同一行，0 次额外 LLM 调用）→ 否则调 planner LLM → plan payload
  （Pydantic validate）→ plan fingerprint → create_or_get（并发最终 1 行）；
- **verify_research_plan_integrity(research_plan_id)**：重新加载 ResearchTask /
  Company → 重建 planner input fingerprint → validate stored payload →
  recompute plan fingerprint → 全比对。**不重新调用 LLM**（spec I）；
  tamper → `ResearchPlanIntegrityError`。

fingerprint（spec H）：
- input fingerprint = schema + task_id + company identity snapshot + question +
  analysis_as_of + planner name/version + model_id + prompt/strategy version；
- plan fingerprint = input fingerprint + normalized validated plan payload；
- canonical JSON SHA-256；**不含** created_at / row id / API key。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    MissingResearchQuestion,
    ResearchExecutionRequiresSingleQuestion,
    TaskNotFound,
)
from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.research_plan import ResearchPlanModel
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_planning.contracts import (
    RESEARCH_PLAN_SCHEMA_VERSION,
    RESEARCH_PLAN_STRATEGY_VERSION,
    CompanyIdentitySnapshot,
    ResearchPlannerRequest,
    ResearchPlanPayload,
)
from app.research_planning.errors import (
    ResearchPlanIntegrityError,
    ResearchPlanNotFound,
)
from app.research_planning.planner import (
    PLANNER_NAME,
    PLANNER_VERSION,
    ResearchPlannerModel,
)
from app.research_planning.repository import ResearchPlanRepository
from app.services.company_identity_service import CompanyIdentityService


@dataclass(frozen=True)
class ResearchPlanResult:
    """create_plan 的结果摘要（不含 prompt / 模型内部信息）。"""

    research_plan_id: UUID
    replayed: bool
    plan_schema_version: int
    planner_name: str
    planner_version: int
    model_id: str
    planner_input_fingerprint: str
    plan_fingerprint: str
    plan_payload: dict
    created_at: datetime


def compute_planner_input_fingerprint(
    *,
    plan_schema_version: int,
    task_id: UUID,
    company: CompanyIdentitySnapshot,
    research_question: str,
    analysis_as_of,
    planner_name: str,
    planner_version: int,
    model_id: str,
    strategy_version: int,
) -> str:
    """planner input fingerprint（spec H）：同输入 → replay 同一行。"""
    payload = {
        "plan_schema_version": plan_schema_version,
        "task_id": str(task_id),
        "company": company.model_dump(mode="json"),
        "research_question": research_question,
        "analysis_as_of": analysis_as_of.isoformat(),
        "planner": {"name": planner_name, "version": planner_version},
        "model_id": model_id,
        "strategy_version": strategy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_plan_fingerprint(*, planner_input_fingerprint: str, payload: dict) -> str:
    """plan fingerprint（spec H）：input fingerprint + normalized validated payload。"""
    canonical = json.dumps(
        {
            "planner_input_fingerprint": planner_input_fingerprint,
            "plan": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_plan_payload(raw: dict) -> ResearchPlanPayload:
    """把 stored plan_payload 还原为 validated ResearchPlanPayload（失败 → IntegrityError）。"""
    try:
        return ResearchPlanPayload.model_validate(raw)
    except ValidationError as exc:
        raise ResearchPlanIntegrityError("stored plan payload invalid") from exc


class ResearchPlanningService:
    """Research Planner 应用服务（create / replay / verify）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        planner_model: ResearchPlannerModel,
        company_identity: CompanyIdentityService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._planner_model = planner_model
        self._company_identity = company_identity

    @property
    def planner_model(self) -> ResearchPlannerModel:
        return self._planner_model

    # ------------------------------------------------------------------ create

    async def create_plan(self, task_id: UUID) -> ResearchPlanResult:
        """从 ResearchTask 派生输入 → replay / 生成并持久化 plan。

        0 real DeepSeek 自动测试：planner_model 注入 Fake（不访问真实 LLM）；
        生产只用于受控 smoke。
        """
        async with self._sessionmaker() as session:
            task = await ResearchTaskRepository(session).get_by_id(task_id)
        if task is None:
            raise TaskNotFound()

        questions = list(task.questions or [])
        if len(questions) > 1:
            raise ResearchExecutionRequiresSingleQuestion()
        research_question = questions[0] if questions else None
        if not research_question:
            raise MissingResearchQuestion()
        analysis_as_of = task.research_end_date

        resolution = await self._company_identity.resolve(task.company_query)
        company = await self._load_company_snapshot(resolution.company.company_id)

        request = ResearchPlannerRequest(
            task_id=task_id,
            company=company,
            research_question=research_question,
            analysis_as_of=analysis_as_of,
        )
        input_fingerprint = compute_planner_input_fingerprint(
            plan_schema_version=RESEARCH_PLAN_SCHEMA_VERSION,
            task_id=task_id,
            company=company,
            research_question=research_question,
            analysis_as_of=analysis_as_of,
            planner_name=PLANNER_NAME,
            planner_version=PLANNER_VERSION,
            model_id=self._planner_model.model_id,
            strategy_version=RESEARCH_PLAN_STRATEGY_VERSION,
        )

        # replay：同 input → 同一行（0 次额外 LLM）。
        async with self._sessionmaker() as session:
            existing = await ResearchPlanRepository(session).get_by_input_fingerprint(
                input_fingerprint
            )
            if existing is not None:
                self._assert_replay_matches(existing, input_fingerprint)
                return self._to_result(existing, replayed=True)

        payload = await self._planner_model.generate(request)
        plan_fingerprint = compute_plan_fingerprint(
            planner_input_fingerprint=input_fingerprint,
            payload=payload.normalized_payload(),
        )
        return await self._persist(
            task_id=task_id,
            company_id=resolution.company.company_id,
            payload=payload,
            input_fingerprint=input_fingerprint,
            plan_fingerprint=plan_fingerprint,
        )

    async def _persist(
        self,
        *,
        task_id: UUID,
        company_id: UUID,
        payload: ResearchPlanPayload,
        input_fingerprint: str,
        plan_fingerprint: str,
    ) -> ResearchPlanResult:
        """短事务 create_or_get（并发 → replay 同一行）。"""
        async with self._sessionmaker() as session:
            repo = ResearchPlanRepository(session)
            plan = ResearchPlanModel(
                task_id=task_id,
                company_id=company_id,
                plan_schema_version=RESEARCH_PLAN_SCHEMA_VERSION,
                planner_name=PLANNER_NAME,
                planner_version=PLANNER_VERSION,
                model_id=self._planner_model.model_id,
                planner_input_fingerprint=input_fingerprint,
                plan_payload=payload.normalized_payload(),
                plan_fingerprint=plan_fingerprint,
            )
            row, created = await repo.create_or_get(plan)
            await session.commit()
            return self._to_result(row, replayed=not created)

    async def _load_company_snapshot(self, company_id: UUID) -> CompanyIdentitySnapshot:
        """从真实 Company + aliases 构建语义身份快照（进 input fingerprint）。"""
        async with self._sessionmaker() as session:
            company = await session.get(CompanyModel, company_id)
            if company is None:
                raise ResearchPlanIntegrityError("company not found")
            result = await session.execute(
                select(CompanyAliasModel).where(CompanyAliasModel.company_id == company_id)
            )
            alias_values = sorted({row.alias for row in result.scalars().all()})
        aliases = list(dict.fromkeys([company.short_name, *alias_values]))
        return CompanyIdentitySnapshot(
            security_code=company.security_code,
            official_name=company.official_name,
            exchange=company.exchange,
            board=company.board,
            aliases=aliases,
        )

    def _assert_replay_matches(self, plan, input_fingerprint: str) -> None:
        """replay 命中时防御性确认 fingerprint 一致（数据损坏 → IntegrityError）。"""
        if plan.planner_input_fingerprint != input_fingerprint:
            raise ResearchPlanIntegrityError("research plan replay fingerprint mismatch")
        validate_plan_payload(plan.plan_payload)

    # ------------------------------------------------------------------ verify

    async def verify_research_plan_integrity(self, research_plan_id: UUID):
        """spec I：重新加载 task/company → 重建 input fingerprint → validate payload →
        recompute plan fingerprint → 全比对。**不重新调用 LLM**。"""
        async with self._sessionmaker() as session:
            plan = await ResearchPlanRepository(session).get_by_id(research_plan_id)
            if plan is None:
                raise ResearchPlanNotFound()
            task = await ResearchTaskRepository(session).get_by_id(plan.task_id)
            company = await session.get(CompanyModel, plan.company_id)
            stored_input = plan.planner_input_fingerprint
            stored_payload = plan.plan_payload
            stored_plan_fp = plan.plan_fingerprint
            stored_task_id = plan.task_id
            stored_company_id = plan.company_id
        if task is None:
            raise ResearchPlanIntegrityError("research plan task missing")
        if company is None:
            raise ResearchPlanIntegrityError("research plan company missing")

        questions = list(task.questions or [])
        if len(questions) != 1:
            raise ResearchPlanIntegrityError("research plan task questions changed")
        research_question = questions[0]
        if not research_question:
            raise ResearchPlanIntegrityError("research plan task question missing")
        company_snapshot = await self._load_company_snapshot(stored_company_id)

        recomputed_input = compute_planner_input_fingerprint(
            plan_schema_version=plan.plan_schema_version,
            task_id=stored_task_id,
            company=company_snapshot,
            research_question=research_question,
            analysis_as_of=task.research_end_date,
            planner_name=plan.planner_name,
            planner_version=plan.planner_version,
            model_id=plan.model_id,
            strategy_version=RESEARCH_PLAN_STRATEGY_VERSION,
        )
        if recomputed_input != stored_input:
            raise ResearchPlanIntegrityError(
                "research plan input fingerprint mismatch (task/company tampered)"
            )

        payload = validate_plan_payload(stored_payload)
        recomputed_plan_fp = compute_plan_fingerprint(
            planner_input_fingerprint=stored_input,
            payload=payload.normalized_payload(),
        )
        if recomputed_plan_fp != stored_plan_fp:
            raise ResearchPlanIntegrityError(
                "research plan fingerprint mismatch (payload tampered)"
            )
        return plan

    # ------------------------------------------------------------------ read

    async def get_plan(self, research_plan_id: UUID) -> ResearchPlanResult:
        async with self._sessionmaker() as session:
            plan = await ResearchPlanRepository(session).get_by_id(research_plan_id)
        if plan is None:
            raise ResearchPlanNotFound()
        return self._to_result(plan, replayed=False)

    @staticmethod
    def _to_result(plan, *, replayed: bool) -> ResearchPlanResult:
        return ResearchPlanResult(
            research_plan_id=plan.research_plan_id,
            replayed=replayed,
            plan_schema_version=plan.plan_schema_version,
            planner_name=plan.planner_name,
            planner_version=plan.planner_version,
            model_id=plan.model_id,
            planner_input_fingerprint=plan.planner_input_fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            plan_payload=dict(plan.plan_payload),
            created_at=plan.created_at.astimezone(UTC),
        )
