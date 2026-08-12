"""Research fulfillment service (stage 7A.2A spec G/H/I): 自动补证据。

`fulfill_research_needs(research_plan_id)` 流程（spec G/H）：
1. **verify Plan + frozen execution context**（`get_verified_execution_context`：
   verify 重放 stored snapshot + 从 `planner_input_payload` 派生执行语义，0 LLM）；
2. **verify Route**（`verify_research_plan_route_integrity`，重放 stored route
   payload，0 LLM）；
3. **`prepare_research()`**（只读现有 artifacts）→ 得到 `missing_needs`；
4. 对每个 missing need 按 `need_kind` 分发到 executor（document/event →
   DocumentNeedExecutor；financial → FinancialNeedExecutor；macro →
   MacroNeedExecutor；valuation → ValuationNeedExecutor）。**只消费
   missing_needs**；已 resolved 的 need 不重复执行；
5. 重跑 `prepare_research()` → `preparation_after`；
6. 组装 `ResearchFulfillmentResult`（schema v1，仅 application output）。

硬边界（spec J/K/L/N/O/P/Q）：
- 确定性测试中 **0 real DeepSeek / 0 Retrieval / 0 Chroma query / 0 Web
  fetch**：executor 的 Retrieval / extractor 依赖由测试注入 Fake；
- 幂等（spec Q）：底层 create_or_get（EvidenceCard / Calculation / macro
  Evidence）按 fingerprint replay → 第 2 次调用 0 新增写；
- 不持久化 raw exception / prompt / API response / reasoning_content；
  调用方不获取 Evidence/Source IDs/query/provider URL（只能看 attempt 摘要）；
- scope = 自动补证据，**不**做全网无限搜索 / 复杂浏览器 agent / 自动 peer
  选择 / Top-level Graph / live provider fetch。
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.research_fulfillment.contracts import (
    FulfillmentAttempt,
    FulfillmentErrorCode,
    FulfillmentPreparation,
    FulfillmentStatus,
    MissingNeedSummary,
    ResearchFulfillmentResult,
)
from app.research_planning.contracts import ResearchDocumentNeedType, ResearchPlanPayload
from app.research_planning.preparation import (
    MissingResearchNeed,
    ResearchPreparationResult,
    ResearchPreparationService,
)
from app.research_planning.router import (
    ResearchSourceRouter,
    SourceRouteEntry,
    validate_route_payload,
)
from app.research_planning.service import ResearchPlanningService


@dataclass(frozen=True)
class FulfillmentContext:
    """executor 共享的 plan/task 上下文（不包含任何 raw / prompt / API 内容）。"""

    research_plan_id: UUID
    route_plan_id: UUID
    company_id: UUID
    task_id: UUID
    research_question: str
    analysis_as_of: date
    payload: ResearchPlanPayload


class ResearchNeedExecutor(Protocol):
    """executor 契约：一条 missing need → 一条 FulfillmentAttempt。

    executor **不抛**确定性错误：补证据失败 → attempt.status=unresolved /
    error_code。意外异常也翻译为 attempt（不泄漏 raw exception 文本）。
    """

    async def fulfill(
        self,
        *,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
    ) -> FulfillmentAttempt: ...


def _prep_summary(result: ResearchPreparationResult) -> FulfillmentPreparation:
    """一次 prepare_research 的摘要快照（只投影 missing needs + readiness）。"""
    return FulfillmentPreparation(
        missing_need_codes=[item.need_code for item in result.missing_needs],
        missing_needs=[
            MissingNeedSummary(
                need_code=item.need_code,
                need_kind=item.need_kind,
                reason_code=item.reason_code.value,
                detail=item.detail,
            )
            for item in result.missing_needs
        ],
        ready_for_analysis=result.ready_for_analysis,
    )


class ResearchFulfillmentService:
    """`fulfill_research_needs` 应用服务：verify + prepare + dispatch + 结果。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        plan_service: ResearchPlanningService,
        router: ResearchSourceRouter,
        preparation: ResearchPreparationService,
        document_executor: ResearchNeedExecutor,
        financial_executor: ResearchNeedExecutor,
        macro_executor: ResearchNeedExecutor,
        valuation_executor: ResearchNeedExecutor,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._plan_service = plan_service
        self._router = router
        self._preparation = preparation
        self._executors: dict[str, ResearchNeedExecutor] = {
            "document": document_executor,
            "event": document_executor,
            "financial": financial_executor,
            "macro": macro_executor,
            "valuation": valuation_executor,
        }

    async def fulfill_research_needs(self, research_plan_id: UUID) -> ResearchFulfillmentResult:
        """verify Plan + verify Route + prepare → 只消费 missing_needs → 重跑。

        全部执行语义（company_id / research_question / analysis_as_of / payload）
        来自 Verified Plan Execution Context（frozen `planner_input_payload`），
        **不读当前 ResearchTask 字段**（spec 7A.2A B）——Task 在 Plan 创建后被修改
        不影响既有 Plan 的执行。Task 存在性/ownership 由 verify 保证。
        """
        ctx = await self._plan_service.get_verified_execution_context(research_plan_id)
        route = await self._router.verify_research_plan_route_integrity(research_plan_id)
        payload = ctx.payload
        route_payload = validate_route_payload(route.route_payload)
        context = FulfillmentContext(
            research_plan_id=ctx.research_plan_id,
            route_plan_id=route.route_plan_id,
            company_id=ctx.company_id,
            task_id=ctx.task_id,
            research_question=ctx.research_question,
            analysis_as_of=ctx.analysis_as_of,
            payload=payload,
        )

        before = await self._preparation.prepare_research(research_plan_id)
        entry_by_code = {entry.need_code: entry for entry in route_payload.entries}

        attempts: list[FulfillmentAttempt] = []
        for missing in before.missing_needs:
            if missing.need_kind == "module":
                # 模块级 missing 是底层 need 派生状态，不独立执行（补足底层后
                # 重跑 prepare 自动重新评估）。
                continue
            executor = self._executor_for(missing, payload)
            entry = entry_by_code.get(missing.need_code)
            if executor is None:
                attempts.append(
                    FulfillmentAttempt(
                        need_code=missing.need_code,
                        need_type=missing.need_kind,
                        route_type=entry.route_type.value if entry is not None else "",
                        status=FulfillmentStatus.UNSUPPORTED,
                        error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
                    )
                )
                continue
            attempts.append(await executor.fulfill(context=context, need=missing, entry=entry))

        after = await self._preparation.prepare_research(research_plan_id)
        return ResearchFulfillmentResult(
            research_plan_id=research_plan_id,
            route_plan_id=route.route_plan_id,
            attempts=attempts,
            preparation_before=_prep_summary(before),
            preparation_after=_prep_summary(after),
            ready_for_analysis=after.ready_for_analysis,
            stage4_request=(
                after.stage4_request.model_dump(mode="json")
                if after.stage4_request is not None
                else None
            ),
        )

    def _executor_for(
        self,
        missing: MissingResearchNeed,
        payload: ResearchPlanPayload,
    ) -> ResearchNeedExecutor | None:
        """need → executor。默认按 need_kind 分发；macro_dataset 的 document
        need（本系统宏观数据形态）路由到 macro executor。"""
        if missing.need_kind == "document":
            doc_need = next(
                (item for item in payload.document_needs if item.need_code == missing.need_code),
                None,
            )
            if doc_need is not None and (
                doc_need.source_type == ResearchDocumentNeedType.MACRO_DATASET
            ):
                return self._executors.get("macro")
        return self._executors.get(missing.need_kind)
