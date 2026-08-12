"""Research planning service (stage 7A.1 spec G/H/I + Final Gate A): create + replay + verify.

- **create_plan(task_id)**：从 ResearchTask 派生 planner 输入（research_question /
  analysis_as_of / **creation-time PlannerInputSnapshot**，spec A）→ input
  fingerprint（v2，只来自 stored snapshot）→ replay（同 input → 同一行，0 次额外
  LLM 调用）→ 否则调 planner LLM → plan payload（Pydantic validate）→ plan
  fingerprint → create_or_get（并发最终 1 行）→ 同时持久化 snapshot；
- **verify_research_plan_integrity(research_plan_id)**：
  - v2 行：validate stored `planner_input_payload` → 与 row 的 task/company 交叉
    核对 → **只重放 stored snapshot** 重建 input fingerprint → validate payload →
    重建 plan fingerprint。当前 Company/Task 只验证 FK identity 存在 + task 仍属于
    同一 company identity（master-data 演化不误判 tamper）；
  - v1 legacy 行：显式 legacy policy（不重读当前 alias / 不重算历史 input）。
  **不重新调用 LLM**（spec I）；tamper → `ResearchPlanIntegrityError`。

fingerprint（spec H / A）：
- v2 input fingerprint = plan_schema_version + stored PlannerInputSnapshot
  （task_id / company_id / 公司语义身份 / aliases / question / as_of /
  snapshot schema_version）+ planner name/version + model_id + strategy version；
- plan fingerprint = input fingerprint + normalized validated plan payload；
- canonical JSON SHA-256；**不含** created_at / row id / API key / prompt / 模型响应。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    CompanyIdentityAmbiguous,
    CompanyIdentityNotFound,
    MissingResearchQuestion,
    ResearchExecutionRequiresSingleQuestion,
    TaskNotFound,
)
from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.research_plan import ResearchPlanModel
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_planning.contracts import (
    PLANNER_INPUT_SNAPSHOT_SCHEMA_VERSION,
    RESEARCH_PLAN_SCHEMA_VERSION,
    RESEARCH_PLAN_STRATEGY_VERSION,
    CompanyIdentitySnapshot,
    ResearchPlannerInputSnapshot,
    ResearchPlannerRequest,
    ResearchPlanPayload,
)
from app.research_planning.errors import (
    ResearchPlanIntegrityError,
    ResearchPlanLegacyExecutionUnsupported,
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


@dataclass(frozen=True)
class VerifiedPlanExecutionContext:
    """verified v2 Plan 的 **frozen 执行上下文**（spec 7A.2A B）。

    全部执行语义（research_question / analysis_as_of / company_id / task_id /
    company identity）来自 `research_plans.planner_input_payload` 的
    creation-time `ResearchPlannerInputSnapshot`，**不读当前 ResearchTask 字段**——
    Task 在 Plan 创建后被修改不影响既有 Plan 的执行语义（frozen Plan 原则）。
    """

    research_plan_id: UUID
    task_id: UUID
    company_id: UUID
    research_question: str
    analysis_as_of: date
    company: CompanyIdentitySnapshot
    payload: ResearchPlanPayload


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
    """planner input fingerprint（spec H）：同输入 → replay 同一行。

    **v1 legacy**：输入来自当前 CompanyIdentitySnapshot（create 当时）。v1 verify
    不再用当前 alias 重新计算历史 input（spec A5）——该函数只保留给 v1 语义与测试。
    """
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


def compute_planner_input_fingerprint_v2(
    *,
    plan_schema_version: int,
    snapshot: ResearchPlannerInputSnapshot,
    planner_name: str,
    planner_version: int,
    model_id: str,
    strategy_version: int,
) -> str:
    """planner input fingerprint v2（spec A）：只来自 **creation-time snapshot**。

    fingerprint = plan_schema_version + stored snapshot（含 task_id / company_id /
    公司语义身份 / aliases / question / as_of / snapshot schema_version）+ planner
    身份 + strategy_version。verify 从 stored `planner_input_payload` 重建，**不读
    当前 Company / aliases**——master-data 正常演化不改变 fingerprint。
    """
    payload = {
        "plan_schema_version": plan_schema_version,
        "planner_input_snapshot": snapshot.model_dump(mode="json"),
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
        snapshot = await self._build_input_snapshot(
            task_id=task_id,
            company_id=resolution.company.company_id,
            research_question=research_question,
            analysis_as_of=analysis_as_of,
        )

        request = ResearchPlannerRequest(
            task_id=task_id,
            company=snapshot.company_identity(),
            research_question=research_question,
            analysis_as_of=analysis_as_of,
        )
        input_fingerprint = compute_planner_input_fingerprint_v2(
            plan_schema_version=RESEARCH_PLAN_SCHEMA_VERSION,
            snapshot=snapshot,
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
            snapshot=snapshot,
            payload=payload,
            input_fingerprint=input_fingerprint,
            plan_fingerprint=plan_fingerprint,
        )

    async def _persist(
        self,
        *,
        task_id: UUID,
        company_id: UUID,
        snapshot: ResearchPlannerInputSnapshot,
        payload: ResearchPlanPayload,
        input_fingerprint: str,
        plan_fingerprint: str,
    ) -> ResearchPlanResult:
        """短事务 create_or_get（并发 → replay 同一行）。v2 行同时持久化
        creation-time PlannerInputSnapshot（spec A）。"""
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
                planner_input_payload=snapshot.model_dump(mode="json"),
                planner_input_schema_version=PLANNER_INPUT_SNAPSHOT_SCHEMA_VERSION,
            )
            row, created = await repo.create_or_get(plan)
            await session.commit()
            return self._to_result(row, replayed=not created)

    async def _build_input_snapshot(
        self,
        *,
        task_id: UUID,
        company_id: UUID,
        research_question: str,
        analysis_as_of,
    ) -> ResearchPlannerInputSnapshot:
        """从真实 Company + aliases 构建 **creation-time** PlannerInputSnapshot。

        aliases = CompanyAliasModel 的稳定排序字符串列表（不含 short_name；
        short_name 单独存为可选字段）。
        """
        async with self._sessionmaker() as session:
            company = await session.get(CompanyModel, company_id)
            if company is None:
                raise ResearchPlanIntegrityError("company not found")
            result = await session.execute(
                select(CompanyAliasModel).where(CompanyAliasModel.company_id == company_id)
            )
            alias_values = sorted({row.alias for row in result.scalars().all()})
        return ResearchPlannerInputSnapshot(
            task_id=task_id,
            company_id=company_id,
            security_code=company.security_code,
            official_name=company.official_name,
            short_name=company.short_name,
            exchange=company.exchange,
            board=company.board,
            aliases=alias_values,
            research_question=research_question,
            analysis_as_of=analysis_as_of,
        )

    def _assert_replay_matches(self, plan, input_fingerprint: str) -> None:
        """replay 命中时防御性确认 fingerprint 一致（数据损坏 → IntegrityError）。"""
        if plan.planner_input_fingerprint != input_fingerprint:
            raise ResearchPlanIntegrityError("research plan replay fingerprint mismatch")
        validate_plan_payload(plan.plan_payload)

    # ------------------------------------------------------------------ verify

    async def verify_research_plan_integrity(self, research_plan_id: UUID):
        """spec I/A：重放 stored plan + stored input snapshot 做完整性校验。

        **不重新调用 LLM**，**不读当前 Company / aliases 重建历史 input**。

        - **v2 行**（plan_schema_version >= 2 或携带 input snapshot）：validate
          stored `planner_input_payload` → 与 row 的 task_id / company_id 交叉核对
          → 只用 stored snapshot + planner 身份重建 input fingerprint → validate
          payload → 重建 plan fingerprint。当前 Company / Task 只验证 FK identity
          存在 + task 仍属于同一 company identity（master-data 演化不误判 tamper）；
        - **v1 legacy 行**（显式 legacy policy，spec A5）：verify row structure +
          validate payload + 用 stored legacy input fp 重建 plan fingerprint + FK
          identity 检查；**不用当前 alias 重新计算历史 input**。
        """
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
            stored_input_payload = plan.planner_input_payload

        is_v2 = plan.plan_schema_version >= 2 or stored_input_payload is not None
        if is_v2:
            return await self._verify_v2(
                plan=plan,
                task=task,
                company=company,
                stored_input=stored_input,
                stored_payload=stored_payload,
                stored_plan_fp=stored_plan_fp,
                stored_task_id=stored_task_id,
                stored_company_id=stored_company_id,
                stored_input_payload=stored_input_payload,
            )
        return await self._verify_v1_legacy(
            plan=plan,
            task=task,
            company=company,
            stored_input=stored_input,
            stored_payload=stored_payload,
            stored_plan_fp=stored_plan_fp,
        )

    async def get_verified_execution_context(
        self, research_plan_id: UUID
    ) -> VerifiedPlanExecutionContext:
        """verify plan → 从 stored `planner_input_payload` 返回 **frozen 执行上下文**。

        - v2 行：verify 后验证 stored snapshot，派生 research_question /
          analysis_as_of / company identity / validated payload——执行语义与
          Task 当前状态解耦（spec 7A.2A B）；
        - v1 legacy 行：无 snapshot → `ResearchPlanLegacyExecutionUnsupported`
          （可 verify 历史，不允许自动执行；**不拿当前 Task 猜历史 question/cutoff**）。
        """
        plan = await self.verify_research_plan_integrity(research_plan_id)
        if plan.planner_input_payload is None:
            raise ResearchPlanLegacyExecutionUnsupported()
        snapshot = self._validate_input_snapshot(plan.planner_input_payload)
        payload = validate_plan_payload(plan.plan_payload)
        return VerifiedPlanExecutionContext(
            research_plan_id=plan.research_plan_id,
            task_id=plan.task_id,
            company_id=plan.company_id,
            research_question=snapshot.research_question,
            analysis_as_of=snapshot.analysis_as_of,
            company=snapshot.company_identity(),
            payload=payload,
        )

    async def _verify_v2(
        self,
        *,
        plan,
        task,
        company,
        stored_input: str,
        stored_payload: dict,
        stored_plan_fp: str,
        stored_task_id: UUID,
        stored_company_id: UUID,
        stored_input_payload: dict | None,
    ):
        """v2 路径：只重放 stored PlannerInputSnapshot 重建 input fingerprint。"""
        if task is None:
            raise ResearchPlanIntegrityError("research plan task missing")
        if company is None:
            raise ResearchPlanIntegrityError("research plan company missing")
        await self._assert_task_company_unchanged(task, stored_company_id)

        if stored_input_payload is None:
            raise ResearchPlanIntegrityError("research plan v2 input snapshot missing")
        snapshot = self._validate_input_snapshot(stored_input_payload)
        if snapshot.task_id != stored_task_id or snapshot.company_id != stored_company_id:
            raise ResearchPlanIntegrityError(
                "research plan input snapshot identity mismatch (task/company tampered)"
            )

        recomputed_input = compute_planner_input_fingerprint_v2(
            plan_schema_version=plan.plan_schema_version,
            snapshot=snapshot,
            planner_name=plan.planner_name,
            planner_version=plan.planner_version,
            model_id=plan.model_id,
            strategy_version=RESEARCH_PLAN_STRATEGY_VERSION,
        )
        if recomputed_input != stored_input:
            raise ResearchPlanIntegrityError(
                "research plan input fingerprint mismatch (input snapshot tampered)"
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

    async def _verify_v1_legacy(
        self,
        *,
        plan,
        task,
        company,
        stored_input: str,
        stored_payload: dict,
        stored_plan_fp: str,
    ):
        """v1 legacy policy（spec A5）：不重读当前 alias / 不重算历史 input。

        只验证 row structure（FK identity 存在）+ payload 结构（object，DB CHECK
        保证）+ 用 stored legacy input fp 与 **raw stored payload** 重建 plan
        fingerprint（legit v1 行的 stored JSON 就是当时的 normalized 形态，fingerprint
        已把 payload 字节全部 commit；**不**用当前 v2 schema 强校验 legacy 行——
        schema 演化后 v1 行不能被 v2 校验器拒绝）。
        """
        if task is None:
            raise ResearchPlanIntegrityError("research plan task missing")
        if company is None:
            raise ResearchPlanIntegrityError("research plan company missing")
        if not isinstance(stored_payload, dict):
            raise ResearchPlanIntegrityError("stored plan payload not object")

        recomputed_plan_fp = compute_plan_fingerprint(
            planner_input_fingerprint=stored_input,
            payload=stored_payload,
        )
        if recomputed_plan_fp != stored_plan_fp:
            raise ResearchPlanIntegrityError(
                "research plan fingerprint mismatch (payload tampered)"
            )
        return plan

    async def _assert_task_company_unchanged(self, task, stored_company_id: UUID) -> None:
        """task 仍解析到同一 company identity（master-data 演化的 FK identity 检查）。"""
        try:
            resolution = await self._company_identity.resolve(task.company_query)
        except (CompanyIdentityNotFound, CompanyIdentityAmbiguous) as exc:
            raise ResearchPlanIntegrityError(
                "research plan task company resolution changed"
            ) from exc
        if resolution.company.company_id != stored_company_id:
            raise ResearchPlanIntegrityError("research plan task company identity changed")

    @staticmethod
    def _validate_input_snapshot(raw: dict) -> ResearchPlannerInputSnapshot:
        try:
            return ResearchPlannerInputSnapshot.model_validate(raw)
        except ValidationError as exc:
            raise ResearchPlanIntegrityError("stored planner input snapshot invalid") from exc

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
