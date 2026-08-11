"""Research backflow service (stage 5E.2B): 可验证 research handoff + 消费新综合。

Stage5 **不执行** Stage2/3/4 research。本 service 只负责确定性交接与控制：
1. `create_or_get_request(source_stage5_run_id)`（spec F/G/H/I/J）：从 Stage 5 run
   的 **真实 final state**（LangGraph checkpoint）恢复 review_action / report /
   human_decision → verify 上游 artifact（research action 无 decision，或
   human_review + research decision）→ 从 Report→Outline→Synthesis chain 恢复
   身份 / cutoff（caller 不能提供）→ derive 结构化 request_payload → request
   fingerprint → create_or_get 原子持久化（同 run → replay，不 update）；
2. `fulfill_request(research_request_id, new_synthesis_result_id)`（spec K/L/M/N）：
   verify request → verify 新 SynthesisResult → continuation identity（company /
   question hash / cutoff 全等）→ no-progress 政策（新 result ≠ source result 且
   新 run fingerprint ≠ source run fingerprint）→ fulfillment fingerprint →
   create_or_get（同 request+result → replay；不同 result → AlreadyFulfilled）；
3. read-side `verify_research_request_integrity` / `verify_research_fulfillment_integrity`
   （spec P，重放校验，**不自动 repair**）；
4. `build_stage5_continuation_request(fulfillment_id)`（spec O）：从 source run
   恢复 task_id，构造 `Stage5WorkflowRequest`（**不**自动创建 WorkflowRun）。

**import 边界**：本模块 **不得** module-level import `app.stage5.graph` /
`app.stage5.dependencies`（依赖环：stage5.dependencies → research_backflow.service
→ stage5.graph → stage5.nodes）。只在 `_recover_final_state` 内局部 import
`app.stage5.graph`；`app.stage5.contracts` 是 leaf，可 module-level import。
"""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.synthesis.service import SynthesisAnalysisService
from app.db.models.research_backflow import (
    ResearchBackflowFulfillmentModel,
    ResearchBackflowRequestModel,
)
from app.db.models.workflow_run import WorkflowRunModel
from app.report.service import ReportService
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_backflow.contracts import (
    RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION,
    RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION,
    ResearchBackflowFulfillmentResult,
    ResearchBackflowRequestResult,
    VerifiedResearchBackflowFulfillment,
    VerifiedResearchBackflowRequest,
    canonical_payload_equals,
    compute_research_backflow_fulfillment_fingerprint,
    compute_research_backflow_request_fingerprint,
)
from app.research_backflow.derive import derive_research_request_payload
from app.research_backflow.errors import (
    ResearchBackflowAlreadyFulfilled,
    ResearchBackflowContinuationMismatch,
    ResearchBackflowError,
    ResearchBackflowFulfillmentNotFound,
    ResearchBackflowIllegalTrigger,
    ResearchBackflowIntegrityError,
    ResearchBackflowInvalidRun,
    ResearchBackflowInvalidState,
    ResearchBackflowNoProgress,
    ResearchBackflowNotResearchTerminal,
    ResearchBackflowPersistenceFailed,
    ResearchBackflowRequestNotFound,
    ResearchBackflowStage5ContextMissing,
)
from app.research_backflow.repository import ResearchBackflowRepository
from app.review.contracts import (
    ACTION_TYPE_HUMAN_REVIEW,
    ACTION_TYPE_RESEARCH,
    HUMAN_DECISION_RESEARCH,
    VerifiedHumanReviewDecision,
    VerifiedReviewAction,
)
from app.review.service import ReviewActionService
from app.stage5.contracts import (
    STAGE5_GRAPH_NAME,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
    Stage5WorkflowRequest,
)

if TYPE_CHECKING:
    from app.stage5.dependencies import Stage5WorkflowDependencies
    from app.workflows.checkpoint import LangGraphCheckpointManager


class ResearchBackflowService:
    """Backflow 协调 / 验证层：只能由 Stage5 runner 注入 Stage5 checkpoint + deps。

    构造不触发任何模型调用（0 LLM / 0 rewrite / 0 research / 0 LangGraph）。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        review_action_service: ReviewActionService,
        report_service: ReportService,
    ) -> None:
        """review_action_service / report_service 显式注入（verify 是上游唯一消费入口）。

        `SynthesisAnalysisService(sessionmaker)` 在内部构造——本 service 只消费
        `verify_result_integrity`（read-side，不需要 model）。
        """
        self._sessionmaker = sessionmaker
        self._review_action_service = review_action_service
        self._report_service = report_service
        self._synthesis_analysis = SynthesisAnalysisService(sessionmaker)
        self._checkpoint_manager: LangGraphCheckpointManager | None = None
        self._stage5_dependencies: Stage5WorkflowDependencies | None = None

    # ------------------------------------------------------------------ DI

    def bind_stage5(
        self,
        checkpoint_manager: "LangGraphCheckpointManager",
        stage5_dependencies: "Stage5WorkflowDependencies",
    ) -> None:
        """绑定 Stage5 checkpoint + deps（只能由 Stage5 runner 调用）。

        未绑定时调用 `create_or_get_request` → `ResearchBackflowStage5ContextMissing`
        （无法恢复 run final state，不静默降级）。
        """
        self._checkpoint_manager = checkpoint_manager
        self._stage5_dependencies = stage5_dependencies

    # ------------------------------------------------------------------ 写路径

    async def create_or_get_request(
        self, source_stage5_run_id: UUID
    ) -> ResearchBackflowRequestResult:
        """从 Stage 5 run 的**真实 final state** 创建 research 交接请求（spec F-J）。

        checkpoint 恢复的 terminal 必须是 `research_required`；review_action /
        report_id 缺失 → `ResearchBackflowInvalidState`。同 run 并发 → replay
        同一行（**不 update**，`request_fingerprint` UNIQUE）。
        """
        await self._load_stage5_run(source_stage5_run_id)
        final_state = await self._recover_final_state(source_stage5_run_id)
        if final_state.get("terminal") != STAGE5_TERMINAL_RESEARCH_REQUIRED:
            raise ResearchBackflowNotResearchTerminal(
                str(final_state.get("terminal") or "<missing>")
            )
        review_action_id = self._coerce_state_uuid(final_state, "review_action_id")
        report_id = self._coerce_state_uuid(final_state, "report_id")
        human_decision_id = self._coerce_state_uuid(final_state, "human_decision_id")
        if review_action_id is None or report_id is None:
            raise ResearchBackflowInvalidState(
                "stage5 final state must carry review_action_id and report_id"
            )

        expected, _, _, _ = await self._derive_request(
            source_stage5_run_id,
            review_action_id,
            report_id,
            human_decision_id,
        )

        async with self._sessionmaker() as session:
            try:
                repo = ResearchBackflowRepository(session)
                row, was_created = await repo.create_or_get_request(expected)
                if not was_created:
                    await self._verify_request_replay(row, expected)
                await session.commit()
            except ResearchBackflowError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ResearchBackflowPersistenceFailed() from exc

        return ResearchBackflowRequestResult(
            research_request_id=row.research_request_id,
            source_stage5_run_id=row.source_stage5_run_id,
            review_action_id=row.review_action_id,
            human_decision_id=row.human_decision_id,
            source_report_id=row.source_report_id,
            company_id=row.company_id,
            research_question_sha256=row.research_question_sha256,
            analysis_as_of=row.analysis_as_of,
            request_schema_version=row.request_schema_version,
            request_payload=dict(row.request_payload),
            request_fingerprint=row.request_fingerprint,
            replayed=not was_created,
        )

    async def fulfill_request(
        self,
        research_request_id: UUID,
        new_synthesis_result_id: UUID,
    ) -> ResearchBackflowFulfillmentResult:
        """消费 upstream 返回的新 SynthesisResult（spec K/L/M/N）。

        verify request → verify 新 result → continuation identity（L）→
        no-progress（M）→ fulfillment fingerprint（N）→ create_or_get。已 fulfilled
        且 result 不同 → `ResearchBackflowAlreadyFulfilled`（不覆盖历史）。
        """
        verified_request = await self.verify_research_request_integrity(research_request_id)
        verified_new = await self._synthesis_analysis.verify_result_integrity(
            new_synthesis_result_id
        )
        self._check_continuation_identity(verified_request, verified_new)
        self._check_no_progress(verified_request, new_synthesis_result_id, verified_new)

        fingerprint = compute_research_backflow_fulfillment_fingerprint(
            fulfillment_schema_version=RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION,
            research_request_id=verified_request.research_request_id,
            request_fingerprint=verified_request.request_fingerprint,
            new_synthesis_result_id=verified_new.synthesis_result_id,
            new_synthesis_result_fingerprint=verified_new.result_fingerprint,
            new_synthesis_run_id=verified_new.synthesis_id,
            new_synthesis_run_fingerprint=verified_new.synthesis_fingerprint,
        )
        expected = ResearchBackflowFulfillmentModel(
            fulfillment_id=uuid4(),
            research_request_id=verified_request.research_request_id,
            new_synthesis_result_id=verified_new.synthesis_result_id,
            fulfillment_schema_version=RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION,
            fulfillment_fingerprint=fingerprint,
        )

        async with self._sessionmaker() as session:
            try:
                repo = ResearchBackflowRepository(session)
                row, was_created = await repo.create_or_get_fulfillment(expected)
                if not was_created:
                    if row.new_synthesis_result_id != verified_new.synthesis_result_id:
                        raise ResearchBackflowAlreadyFulfilled()
                    if row.fulfillment_fingerprint != fingerprint:
                        raise ResearchBackflowIntegrityError(
                            "research fulfillment fingerprint replay mismatch"
                        )
                await session.commit()
            except ResearchBackflowError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ResearchBackflowPersistenceFailed() from exc

        return ResearchBackflowFulfillmentResult(
            fulfillment_id=row.fulfillment_id,
            research_request_id=row.research_request_id,
            new_synthesis_result_id=row.new_synthesis_result_id,
            fulfillment_schema_version=row.fulfillment_schema_version,
            fulfillment_fingerprint=row.fulfillment_fingerprint,
            replayed=not was_created,
        )

    # ------------------------------------------------------------------ read-side（spec P）

    async def verify_research_request_integrity(
        self, research_request_id: UUID
    ) -> VerifiedResearchBackflowRequest:
        """完整重建 research request（重放校验，**不自动 repair**）。

        **不读 checkpoint**——从 persisted 行的 FK id 重验：source run graph_name
        → verify action（± decision）→ legal trigger → verify report → 重派生
        payload + fingerprint → 与 persisted 对比。
        """
        async with self._sessionmaker() as session:
            row = await ResearchBackflowRepository(session).get_request_by_id(research_request_id)
        if row is None:
            raise ResearchBackflowRequestNotFound()
        await self._load_stage5_run(row.source_stage5_run_id)

        expected, verified_action, verified_decision, verified_report = await self._derive_request(
            row.source_stage5_run_id,
            row.review_action_id,
            row.source_report_id,
            row.human_decision_id,
        )
        await self._verify_request_replay(row, expected)
        source_synthesis = verified_report.verified_outline.verified_synthesis_result

        return VerifiedResearchBackflowRequest(
            research_request_id=row.research_request_id,
            source_stage5_run_id=row.source_stage5_run_id,
            review_action_id=row.review_action_id,
            human_decision_id=row.human_decision_id,
            source_report_id=row.source_report_id,
            company_id=row.company_id,
            research_question_sha256=row.research_question_sha256,
            analysis_as_of=row.analysis_as_of,
            request_schema_version=row.request_schema_version,
            request_payload=dict(row.request_payload),
            request_fingerprint=row.request_fingerprint,
            created_at=row.created_at,
            verified_action=verified_action,
            verified_decision=verified_decision,
            verified_report=verified_report,
            verified_source_synthesis=source_synthesis,
        )

    async def verify_research_fulfillment_integrity(
        self, fulfillment_id: UUID
    ) -> VerifiedResearchBackflowFulfillment:
        """完整重建 fulfillment（重放校验，**不自动 repair**）。

        重验 request + 新 synthesis → 重放 continuation identity / no-progress →
        重算 fingerprint 与 persisted 对比。
        """
        async with self._sessionmaker() as session:
            row = await ResearchBackflowRepository(session).get_fulfillment_by_id(fulfillment_id)
        if row is None:
            raise ResearchBackflowFulfillmentNotFound()
        verified_request = await self.verify_research_request_integrity(row.research_request_id)
        verified_new = await self._synthesis_analysis.verify_result_integrity(
            row.new_synthesis_result_id
        )
        self._check_continuation_identity(verified_request, verified_new)
        self._check_no_progress(verified_request, row.new_synthesis_result_id, verified_new)

        fingerprint = compute_research_backflow_fulfillment_fingerprint(
            fulfillment_schema_version=RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION,
            research_request_id=verified_request.research_request_id,
            request_fingerprint=verified_request.request_fingerprint,
            new_synthesis_result_id=verified_new.synthesis_result_id,
            new_synthesis_result_fingerprint=verified_new.result_fingerprint,
            new_synthesis_run_id=verified_new.synthesis_id,
            new_synthesis_run_fingerprint=verified_new.synthesis_fingerprint,
        )
        if row.fulfillment_schema_version != RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION:
            raise ResearchBackflowIntegrityError("research fulfillment schema version mismatch")
        if row.fulfillment_fingerprint != fingerprint:
            raise ResearchBackflowIntegrityError("research fulfillment fingerprint mismatch")

        return VerifiedResearchBackflowFulfillment(
            fulfillment_id=row.fulfillment_id,
            research_request_id=row.research_request_id,
            new_synthesis_result_id=row.new_synthesis_result_id,
            fulfillment_schema_version=row.fulfillment_schema_version,
            fulfillment_fingerprint=row.fulfillment_fingerprint,
            created_at=row.created_at,
            verified_request=verified_request,
            verified_new_synthesis=verified_new,
        )

    # ------------------------------------------------------------------ 续跑（spec O）

    async def build_stage5_continuation_request(
        self, fulfillment_id: UUID
    ) -> Stage5WorkflowRequest:
        """从 fulfillment 构造 Stage 5 续跑请求（spec O）。

        task_id 从 source run 的 ResearchTask 恢复；company / question / cutoff 与
        新 synthesis 身份一致；`synthesis_result_id` 是 fulfillment consumed 的新
        result。**不**自动创建 WorkflowRun——caller 决定何时用该请求开新 run。
        """
        verified_fulfillment = await self.verify_research_fulfillment_integrity(fulfillment_id)
        request = verified_fulfillment.verified_request
        new_synth = verified_fulfillment.verified_new_synthesis
        run = await self._load_stage5_run(request.source_stage5_run_id)
        return Stage5WorkflowRequest(
            task_id=run.task_id,
            company_id=new_synth.company_id,
            research_question=new_synth.research_question,
            analysis_as_of=new_synth.analysis_as_of,
            synthesis_result_id=new_synth.synthesis_result_id,
        )

    # ------------------------------------------------------------------ 私有

    async def _load_stage5_run(self, run_id: UUID) -> WorkflowRunModel:
        async with self._sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None or run.graph_name != STAGE5_GRAPH_NAME:
            raise ResearchBackflowInvalidRun()
        return run

    async def _recover_final_state(self, run_id: UUID) -> dict:
        """从 LangGraph checkpoint 读取 run 的 final state（spec F：真实 terminal）。

        checkpoint_manager / deps 未绑定 → `ResearchBackflowStage5ContextMissing`。
        局部 import `app.stage5.graph` 避免 module-level 依赖环。
        """
        if self._checkpoint_manager is None or self._stage5_dependencies is None:
            raise ResearchBackflowStage5ContextMissing()
        run = await self._load_stage5_run(run_id)
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        from app.stage5.graph import build_stage5_report_graph  # local import（防环）

        graph = build_stage5_report_graph(self._stage5_dependencies, checkpointer)
        state = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
        if state is None:
            raise ResearchBackflowInvalidState("stage5 run has no checkpoint state to recover")
        return dict(state.values or {})

    async def _derive_request(
        self,
        run_id: UUID,
        review_action_id: UUID,
        report_id: UUID,
        human_decision_id: UUID | None,
    ) -> tuple[
        ResearchBackflowRequestModel,
        VerifiedReviewAction,
        VerifiedHumanReviewDecision | None,
        object,
    ]:
        """verify 上游 chain → 派生 payload + fingerprint → 构造期望模型行。

        身份 / cutoff（company / question hash / analysis_as_of）只从
        Report→Outline→Synthesis chain 恢复（spec I：caller 不能提供）。
        """
        verified_action = await self._review_action_service.verify_review_action_integrity(
            review_action_id
        )
        verified_decision = None
        if human_decision_id is not None:
            verified_decision = await self._review_action_service.verify_human_decision_integrity(
                human_decision_id
            )
        self._check_legal_trigger(verified_action, verified_decision)

        verified_report = await self._report_service.verify_report_integrity(report_id)
        source_synthesis = verified_report.verified_outline.verified_synthesis_result

        payload = derive_research_request_payload(verified_action, verified_decision)
        fingerprint = compute_research_backflow_request_fingerprint(
            request_schema_version=RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION,
            source_stage5_run_id=run_id,
            review_action_id=verified_action.review_action_id,
            review_action_fingerprint=verified_action.action_fingerprint,
            human_decision_id=(verified_decision.human_decision_id if verified_decision else None),
            human_decision_fingerprint=(
                verified_decision.decision_fingerprint if verified_decision else None
            ),
            source_report_id=verified_report.report_id,
            report_fingerprint=verified_report.report_fingerprint,
            company_id=source_synthesis.company_id,
            research_question_sha256=source_synthesis.research_question_sha256,
            analysis_as_of=source_synthesis.analysis_as_of,
            request_payload=payload,
        )
        expected = ResearchBackflowRequestModel(
            research_request_id=uuid4(),
            source_stage5_run_id=run_id,
            review_action_id=verified_action.review_action_id,
            human_decision_id=(verified_decision.human_decision_id if verified_decision else None),
            source_report_id=verified_report.report_id,
            company_id=source_synthesis.company_id,
            research_question_sha256=source_synthesis.research_question_sha256,
            analysis_as_of=source_synthesis.analysis_as_of,
            request_schema_version=RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION,
            request_payload=payload,
            request_fingerprint=fingerprint,
        )
        return expected, verified_action, verified_decision, verified_report

    @staticmethod
    def _check_legal_trigger(
        verified_action: VerifiedReviewAction,
        verified_decision: VerifiedHumanReviewDecision | None,
    ) -> None:
        """spec G：research action（无 decision）或 human_review + research decision。

        其余（finalize / rewrite / revision_limit_exceeded action、research action
        带 decision、human_review 无 decision / 非 research decision）→
        `ResearchBackflowIllegalTrigger`（0 write）。
        """
        if verified_action.action_type == ACTION_TYPE_RESEARCH:
            if verified_decision is not None:
                raise ResearchBackflowIllegalTrigger(
                    "research action must not carry a human decision"
                )
            return
        if verified_action.action_type == ACTION_TYPE_HUMAN_REVIEW:
            if verified_decision is None:
                raise ResearchBackflowIllegalTrigger(
                    "human_review action requires a human decision"
                )
            if verified_decision.decision != HUMAN_DECISION_RESEARCH:
                raise ResearchBackflowIllegalTrigger(
                    f"human decision must be {HUMAN_DECISION_RESEARCH}, "
                    f"got {verified_decision.decision}"
                )
            return
        raise ResearchBackflowIllegalTrigger(
            f"action_type={verified_action.action_type} does not trigger research"
        )

    @staticmethod
    async def _verify_request_replay(
        row: ResearchBackflowRequestModel,
        expected: ResearchBackflowRequestModel,
    ) -> None:
        """persisted 行 vs 重派生期望（spec P replay；任一不一致 → IntegrityError）。"""
        checks = [
            ("source_stage5_run_id", row.source_stage5_run_id, expected.source_stage5_run_id),
            ("review_action_id", row.review_action_id, expected.review_action_id),
            ("human_decision_id", row.human_decision_id, expected.human_decision_id),
            ("source_report_id", row.source_report_id, expected.source_report_id),
            ("company_id", row.company_id, expected.company_id),
            (
                "research_question_sha256",
                row.research_question_sha256,
                expected.research_question_sha256,
            ),
            ("analysis_as_of", row.analysis_as_of, expected.analysis_as_of),
            (
                "request_schema_version",
                row.request_schema_version,
                expected.request_schema_version,
            ),
            ("request_fingerprint", row.request_fingerprint, expected.request_fingerprint),
        ]
        for field, actual, want in checks:
            if actual != want:
                raise ResearchBackflowIntegrityError(f"research request {field} replay mismatch")
        if not canonical_payload_equals(row.request_payload, expected.request_payload):
            raise ResearchBackflowIntegrityError("research request payload replay mismatch")

    @staticmethod
    def _check_continuation_identity(
        verified_request: VerifiedResearchBackflowRequest,
        verified_new: object,
    ) -> None:
        """spec L：company_id / research_question_sha256 / analysis_as_of 全等。

        任何不一致 → `ResearchBackflowContinuationMismatch`（0 write）。v1 不做
        silent cutoff update。
        """
        if verified_new.company_id != verified_request.company_id:
            raise ResearchBackflowContinuationMismatch("company_id")
        if verified_new.research_question_sha256 != verified_request.research_question_sha256:
            raise ResearchBackflowContinuationMismatch("research_question_sha256")
        if verified_new.analysis_as_of != verified_request.analysis_as_of:
            raise ResearchBackflowContinuationMismatch("analysis_as_of")

    @staticmethod
    def _check_no_progress(
        verified_request: VerifiedResearchBackflowRequest,
        new_synthesis_result_id: UUID,
        verified_new: object,
    ) -> None:
        """spec M：新 SynthesisResult ≠ source synthesis（双条件）。

        1. 新 result id ≠ source result id（不能直接引用 source result）；
        2. 新 run fingerprint ≠ source run fingerprint（同一 run 重分析无新证据）。
        任一违反 → `ResearchBackflowNoProgress`（防 research_required → 相同综合
        → 无限循环）。
        """
        source = verified_request.verified_source_synthesis
        if new_synthesis_result_id == source.synthesis_result_id:
            raise ResearchBackflowNoProgress("new result equals the source result")
        if verified_new.synthesis_fingerprint == source.synthesis_fingerprint:
            raise ResearchBackflowNoProgress("new synthesis reuses the source synthesis run")

    @staticmethod
    def _coerce_state_uuid(final_state: dict, field: str) -> UUID | None:
        """checkpoint 里的 id 字段转 UUID（可能已是 str / None）。

        None → None（human_decision_id 合法为空）；非 UUID → 防御性硬失败。
        """
        value = final_state.get(field)
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            raise ResearchBackflowInvalidState(f"invalid {field} in stage5 final state") from None
