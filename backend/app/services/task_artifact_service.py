"""Task-level read-only artifact workspace service (Stage 6B.1).

任务级 scoped（不是 company-level）：claims / reports / audits 均无 task_id
列，任务→产物的唯一权威来源是 LangGraph PG Checkpointer（thread_id==run_id）。
锚定语义复用 ResearchExecutionRecoveryCoordinator——每个 task 取最近一条
Stage4 run + 最近一条 Stage5 run（线性链尾，Stage4 checkpoint 的
`synthesis_result_id` 链接到 Stage5）。

精确 artifact ID 集合恢复路径：
- claim 集：Stage4 checkpoint `claim_ids`（权威）；analysis tab 经
  `SynthesisService.verify_synthesis_integrity` 恢复可验证的 claim + 内联证据。
- evidence 集：work item 输入证据 ID 并集（checkpoint `analysis_work_items`）
  ∪ verified claims 的 `evidence_card_ids`（evidence tab 与 count 共用同一
  helper，保证一致）。
- source 集：evidence 集在 `evidence_cards` 的 distinct `source_id`。
- report / reviews：Stage5 checkpoint `report_id` / `audit_id` →
  `verify_report_integrity` / `verify_audit_integrity`。

错误策略：synthesis / report / audit / outline / draft 异常是独立异常树（非
DomainError），服务层统一 catch → log warning → 降级为空 / null（200 语义），
不 500。task 不存在 → `TaskNotFound`（DomainError，已注册 404）。
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.audit.contracts import ReviewIssue, VerifiedReportAudit
from app.audit.errors import ReportAuditError
from app.core.errors import TaskNotFound
from app.core.logging import get_logger
from app.db.models.research_task import ResearchTaskModel
from app.db.models.workflow_run import WorkflowRunModel
from app.draft_section.errors import DraftSectionError
from app.report.contracts import VerifiedReport
from app.report.errors import ReportError
from app.report_outline.errors import ReportOutlineError
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.artifact import (
    AnalysisArtifactResponse,
    ClaimArtifactResponse,
    EvidenceArtifactListResponse,
    EvidenceArtifactResponse,
    ReportArtifactResponse,
    ReviewIssueArtifactResponse,
    ReviewsArtifactResponse,
    SourceArtifactListResponse,
    SourceArtifactResponse,
    WorkItemSummary,
)
from app.schemas.research_execution import ArtifactSummary
from app.services.research_execution_service import ResearchExecutionService
from app.stage4.graph import STAGE4_GRAPH_NAME
from app.stage5.contracts import STAGE5_GRAPH_NAME
from app.synthesis.contracts import VerifiedSynthesisClaim, VerifiedSynthesisRun
from app.synthesis.errors import SynthesisError
from app.synthesis.service import SynthesisService

logger = get_logger("app.task_artifact")

# verify 调用可能向上传播的域错误树（report/audit 各自的上游独立异常树）。
_VERIFY_DOMAIN_ERRORS = (ReportError, ReportAuditError, ReportOutlineError, DraftSectionError)

_WORK_ITEM_EVIDENCE_KEYS = (
    "evidence_card_ids",
    "additional_evidence_ids",
    "macro_driver_evidence_ids",
    "company_evidence_ids",
)


def _to_uuids(raw: object) -> list[UUID]:
    """checkpoint 中 UUID 序列化为 str；统一转回 UUID（容忍已为 UUID 的值）。"""
    if not raw:
        return []
    return [value if isinstance(value, UUID) else UUID(value) for value in raw]


@dataclass(frozen=True)
class _Anchor:
    """一次任务级 artifact 解析的锚定快照（task + 两条 run + 两个 checkpoint state）。"""

    task: ResearchTaskModel
    stage4_run: WorkflowRunModel | None
    stage4_state: dict
    stage5_run: WorkflowRunModel | None
    stage5_state: dict


class TaskArtifactService:
    """任务级只读 artifact workspace；每个方法使用短生命周期 session。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        research_execution: ResearchExecutionService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._research_execution = research_execution

    # ------------------------------------------------------------------ public API

    async def get_sources(
        self,
        task_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> SourceArtifactListResponse:
        anchor = await self._anchor(task_id)
        verified = await self._resolve_synthesis(anchor)
        evidence_ids = await self._evidence_ids(anchor, verified)
        source_ids = await self._source_ids(evidence_ids)
        async with self._sessionmaker() as session:
            rows, total = await SourceRecordRepository(session).list_by_ids(
                sorted(source_ids, key=str), limit, offset
            )
        return SourceArtifactListResponse(
            items=[SourceArtifactResponse.model_validate(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_evidence(
        self,
        task_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> EvidenceArtifactListResponse:
        anchor = await self._anchor(task_id)
        verified = await self._resolve_synthesis(anchor)
        evidence_ids = await self._evidence_ids(anchor, verified)
        async with self._sessionmaker() as session:
            rows, total = await EvidenceCardRepository(session).list_by_ids(
                sorted(evidence_ids, key=str), limit, offset
            )
        return EvidenceArtifactListResponse(
            items=[EvidenceArtifactResponse.model_validate(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_analysis(self, task_id: UUID) -> AnalysisArtifactResponse:
        anchor = await self._anchor(task_id)
        verified = await self._resolve_synthesis(anchor)
        state = anchor.stage4_state
        work_items = self._map_work_items(state)
        claims: list[ClaimArtifactResponse] = []
        synthesis_id = None
        synthesis_fingerprint = None
        if verified is not None:
            synthesis_id = verified.synthesis_id
            synthesis_fingerprint = verified.synthesis_fingerprint
            claims = [self._map_claim(claim) for claim in verified.verified_claims]
        elif state.get("synthesis_id"):
            # verify 降级时仍暴露 checkpoint 里的原始 synthesis_id。
            synthesis_id = UUID(state["synthesis_id"])
        return AnalysisArtifactResponse(
            company_id=UUID(state["company_id"]) if state.get("company_id") else None,
            research_question=state.get("research_question"),
            analysis_as_of=(
                date.fromisoformat(state["analysis_as_of"]) if state.get("analysis_as_of") else None
            ),
            work_items=work_items,
            claims=claims,
            synthesis_id=synthesis_id,
            synthesis_fingerprint=synthesis_fingerprint,
        )

    async def get_report(self, task_id: UUID) -> ReportArtifactResponse:
        anchor = await self._anchor(task_id)
        verified = await self._resolve_report(anchor)
        if verified is None:
            return ReportArtifactResponse()
        payload = verified.report_payload or {}
        return ReportArtifactResponse(
            report_id=verified.report_id,
            outline_id=verified.outline_id,
            company_id=verified.company_id,
            research_question_sha256=verified.research_question_sha256,
            analysis_as_of=verified.analysis_as_of,
            report_schema_version=verified.report_schema_version,
            report_fingerprint=verified.report_fingerprint,
            section_count=len(payload.get("sections") or []),
        )

    async def get_reviews(self, task_id: UUID) -> ReviewsArtifactResponse:
        anchor = await self._anchor(task_id)
        audit = await self._resolve_reviews(anchor)
        if audit is None:
            return ReviewsArtifactResponse()
        return ReviewsArtifactResponse(
            audit_id=audit.audit_id,
            report_id=audit.report_id,
            audit_status=audit.audit_status,
            recommended_route=audit.recommended_route,
            issue_count=audit.issue_count,
            audit_fingerprint=audit.audit_fingerprint,
            issues=[self._map_issue(issue) for issue in audit.issues],
        )

    async def count_artifacts(self, task_id: UUID) -> ArtifactSummary:
        """任务级产物计数（供 workspace 投影复用；与各 tab 共用同一 ID 推导）。"""
        anchor = await self._anchor(task_id)
        verified = await self._resolve_synthesis(anchor)
        evidence_ids = await self._evidence_ids(anchor, verified)
        source_ids = await self._source_ids(evidence_ids)
        report = await self._resolve_report(anchor)
        reviews = await self._resolve_reviews(anchor)
        return ArtifactSummary(
            source_count=len(source_ids),
            evidence_count=len(evidence_ids),
            claim_count=len(anchor.stage4_state.get("claim_ids") or []),
            report_count=1 if report is not None else 0,
            review_issue_count=reviews.issue_count if reviews is not None else 0,
        )

    # ------------------------------------------------------------------ anchor

    async def _anchor(self, task_id: UUID) -> _Anchor:
        async with self._sessionmaker() as session:
            task = await ResearchTaskRepository(session).get_by_id(task_id)
            if task is None:
                raise TaskNotFound()
            run_repo = WorkflowRunRepository(session)
            stage4_run = await run_repo.get_latest_for_task_by_graph(task_id, STAGE4_GRAPH_NAME)
            stage5_run = await run_repo.get_latest_for_task_by_graph(task_id, STAGE5_GRAPH_NAME)

        stage4_state: dict = {}
        if stage4_run is not None:
            runner = self._research_execution.stage4_runner_factory()
            stage4_state = await runner.read_checkpoint_state(stage4_run.run_id)
        stage5_state: dict = {}
        if stage5_run is not None:
            runner = self._research_execution.stage5_runner_factory()
            stage5_state = await runner.read_checkpoint_state(stage5_run.run_id)
        return _Anchor(
            task=task,
            stage4_run=stage4_run,
            stage4_state=stage4_state,
            stage5_run=stage5_run,
            stage5_state=stage5_state,
        )

    # ------------------------------------------------------------------ ID resolution

    async def _resolve_synthesis(self, anchor: _Anchor) -> VerifiedSynthesisRun | None:
        synthesis_id = anchor.stage4_state.get("synthesis_id")
        if not synthesis_id:
            return None
        service = SynthesisService(self._sessionmaker)
        try:
            async with self._sessionmaker() as session:
                return await service.verify_synthesis_integrity(session, UUID(synthesis_id))
        except SynthesisError as exc:
            logger.warning(
                "artifact_synthesis_verify_failed",
                error_type=type(exc).__name__,
            )
            return None

    async def _evidence_ids(
        self,
        anchor: _Anchor,
        verified: VerifiedSynthesisRun | None = None,
    ) -> set[UUID]:
        """任务级 evidence 集：work item 输入 ∪ verified claims 的 evidence。"""
        ids = set()
        for item in anchor.stage4_state.get("analysis_work_items") or []:
            for key in _WORK_ITEM_EVIDENCE_KEYS:
                ids.update(_to_uuids(item.get(key)))
        if verified is not None:
            for claim in verified.verified_claims:
                ids.update(claim.evidence_card_ids)
        return ids

    async def _source_ids(self, evidence_ids: set[UUID]) -> set[UUID]:
        if not evidence_ids:
            return set()
        async with self._sessionmaker() as session:
            rows, _ = await EvidenceCardRepository(session).list_by_ids(
                sorted(evidence_ids, key=str), limit=len(evidence_ids), offset=0
            )
        return {row.source_id for row in rows if row.source_id is not None}

    async def _resolve_report(self, anchor: _Anchor) -> VerifiedReport | None:
        report_id = anchor.stage5_state.get("report_id")
        if not report_id:
            return None
        try:
            runner = self._research_execution.stage5_runner_factory()
            return await runner.dependencies.report_service.verify_report_integrity(
                UUID(report_id)
            )
        except _VERIFY_DOMAIN_ERRORS as exc:
            logger.warning(
                "artifact_report_verify_failed",
                error_type=type(exc).__name__,
            )
            return None

    async def _resolve_reviews(self, anchor: _Anchor) -> VerifiedReportAudit | None:
        audit_id = anchor.stage5_state.get("audit_id")
        if not audit_id:
            return None
        try:
            runner = self._research_execution.stage5_runner_factory()
            return await runner.dependencies.report_audit_service.verify_audit_integrity(
                UUID(audit_id)
            )
        except _VERIFY_DOMAIN_ERRORS as exc:
            logger.warning(
                "artifact_audit_verify_failed",
                error_type=type(exc).__name__,
            )
            return None

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _map_work_items(state: dict) -> list[WorkItemSummary]:
        claims_by_item: dict[str, list[UUID]] = {}
        for result in state.get("analysis_results") or []:
            item_id = result.get("item_id")
            if item_id is not None:
                claims_by_item[str(item_id)] = _to_uuids(result.get("claim_ids"))
        items: list[WorkItemSummary] = []
        for item in state.get("analysis_work_items") or []:
            item_id = item.get("item_id")
            items.append(
                WorkItemSummary(
                    item_id=str(item_id) if item_id is not None else "",
                    analysis_type=item.get("analysis_type") or "",
                    evidence_card_ids=_to_uuids(item.get("evidence_card_ids")),
                    additional_evidence_ids=_to_uuids(item.get("additional_evidence_ids")),
                    macro_driver_evidence_ids=_to_uuids(item.get("macro_driver_evidence_ids")),
                    company_evidence_ids=_to_uuids(item.get("company_evidence_ids")),
                    calculation_ids=_to_uuids(item.get("calculation_ids")),
                    comparison_ids=_to_uuids(item.get("comparison_ids")),
                    claim_ids=claims_by_item.get(str(item_id) if item_id is not None else "", []),
                )
            )
        return items

    @staticmethod
    def _map_claim(claim: VerifiedSynthesisClaim) -> ClaimArtifactResponse:
        # domain / kind / confidence / importance 是 StrEnum，显式取 .value 转 str。
        return ClaimArtifactResponse(
            claim_id=claim.claim_id,
            company_id=claim.company_id,
            analysis_domain=claim.analysis_domain.value,
            claim_kind=claim.claim_kind.value,
            statement=claim.statement,
            confidence=claim.confidence.value,
            importance=claim.importance.value,
            evidence_card_ids=list(claim.evidence_card_ids),
            analyst_name=claim.analyst_name,
        )

    @staticmethod
    def _map_issue(issue: ReviewIssue) -> ReviewIssueArtifactResponse:
        return ReviewIssueArtifactResponse(
            review_issue_id=issue.review_issue_id,
            ordinal=issue.ordinal,
            issue_type=issue.issue_type,
            severity=issue.severity,
            section_id=issue.section_id,
            paragraph_index=issue.paragraph_index,
            message=issue.message,
            related_claim_ids=list(issue.related_claim_ids),
            related_evidence_card_ids=list(issue.related_evidence_card_ids),
        )
