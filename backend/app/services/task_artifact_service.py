"""Task-level read-only artifact workspace service (Stage 6B.1).

任务级 scoped（不是 company-level）：claims / reports / audits 均无 task_id
列，任务→产物的唯一权威来源是 LangGraph PG Checkpointer（thread_id==run_id）。

**canonical lineage 锚定（spec B/C）**：
- canonical synthesis = 最新 Stage5 checkpoint 的 `synthesis_result_id`；无
  Stage5 时，最新 Stage4 即 canonical analysis anchor；
- 只有 checkpoint `.synthesis_result_id == canonical` 的 Stage4 run 才成为
  `matched_stage4_run`；research backflow 的新 Synthesis（S2）没有对应 Stage4
  → 无匹配是合法状态：`work_items=[]` 且 `work_items_available=false`，**绝不
  混用旧 S1 work items / 旧 S1-only evidence**。

**只读路径 0 LLM（spec E）**：checkpoint 读取用**裸 checkpointer** 的
`aget_state`（thread_id==run_id，无需 build graph）；verify 链注入既有
Services 的 read-side `verify_*_integrity`（全部 0 模型调用，模型构造全程
lazy，不依赖 DEEPSEEK_API_KEY）。生产 DI 复用 `create_stage5_dependencies`
装配的同一批 Services。

**完整性语义（spec D）**：artifact 缺失 → 空 / null（200）；artifact ID 存在
但 `verify_*_integrity` 重建失败 → `TaskArtifactIntegrityError`（HTTP 409，
统一 `{error:{code,message,request_id}}` 信封，不泄漏 SQL / stack / 原始异常），
**不 repair / 不降级为空**。

**dual-origin sources（spec F）**：
- document_chunk：EvidenceCard → SourceRecord → RawArtifact（source_id 非空）；
- macro_observation：EvidenceCard → MacroObservation → MacroDatasetSnapshot →
  MacroSeries → SourceProvider → RawArtifact（source_id=NULL，source_identity
  由 provider/series/snapshot 恢复），按 series 去重。
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.synthesis.contracts import (
    SynthesisConflict,
    SynthesisEvidenceGap,
    SynthesisTheme,
    VerifiedSynthesisResult,
)
from app.analysis.synthesis.errors import SynthesisAnalysisError
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.audit.contracts import ReviewIssue, VerifiedReportAudit
from app.audit.errors import ReportAuditError
from app.core.errors import TaskArtifactIntegrityError, TaskNotFound
from app.core.logging import get_logger
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.research_task import ResearchTaskModel
from app.db.models.source_record import SourceRecordModel
from app.db.models.workflow_run import WorkflowRunModel
from app.draft_section.errors import DraftSectionError
from app.report.contracts import CheckFinding, VerifiedReport
from app.report.errors import ReportError
from app.report_outline.errors import ReportOutlineError
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_backflow.errors import ResearchBackflowError
from app.research_backflow.repository import ResearchBackflowRepository
from app.review.contracts import VerifiedHumanReviewDecision
from app.review.errors import ReviewError
from app.schemas.artifact import (
    AnalysisArtifactResponse,
    CheckFindingArtifact,
    ClaimArtifactResponse,
    ClaimEvidenceRelation,
    EvidenceArtifactListResponse,
    EvidenceArtifactResponse,
    HumanReviewArtifact,
    ReportArtifactResponse,
    ReportCheckArtifact,
    ReportParagraphArtifact,
    ReportSectionArtifact,
    ResearchBackflowArtifact,
    ReviewActionArtifact,
    ReviewIssueArtifactResponse,
    ReviewsArtifactResponse,
    SourceArtifactListResponse,
    SourceArtifactResponse,
    SynthesisConflictArtifact,
    SynthesisEvidenceGapArtifact,
    SynthesisThemeArtifact,
    WorkItemSummary,
)
from app.schemas.research_execution import ArtifactSummary
from app.stage4.graph import STAGE4_GRAPH_NAME
from app.stage5.contracts import STAGE5_GRAPH_NAME
from app.synthesis.contracts import VerifiedSynthesisClaim, VerifiedSynthesisRun
from app.synthesis.errors import SynthesisError
from app.synthesis.service import SynthesisService
from app.workflows.checkpoint import LangGraphCheckpointManager

logger = get_logger("app.task_artifact")

# verify 调用可能向上传播的域错误树（各自上游独立异常树，见各 errors.py）。
# synthesis 的两条独立异常树（`SynthesisAnalysisError` + `SynthesisError`）也会
# 经 outline verify → `verify_result_integrity` 原样向上传播（Gate0-E：canonical
# SynthesisResult / SynthesisRun tamper → 下游 report / reviews 同样 409，**不
# 允许原始异常泄漏为 500**）。
_SYNTHESIS_VERIFY_ERRORS = (SynthesisAnalysisError, SynthesisError)
_REPORT_VERIFY_ERRORS = (ReportError, ReportOutlineError, DraftSectionError) + (
    _SYNTHESIS_VERIFY_ERRORS
)
_AUDIT_VERIFY_ERRORS = (ReportAuditError, ReportError, ReportOutlineError, DraftSectionError) + (
    _SYNTHESIS_VERIFY_ERRORS
)
_REVIEW_VERIFY_ERRORS = (
    ReviewError,
    ReportAuditError,
    ReportError,
    ReportOutlineError,
    DraftSectionError,
) + _SYNTHESIS_VERIFY_ERRORS
_BACKFLOW_VERIFY_ERRORS = (
    ResearchBackflowError,
    ReviewError,
    ReportAuditError,
    ReportError,
    ReportOutlineError,
    DraftSectionError,
    SynthesisAnalysisError,
) + _SYNTHESIS_VERIFY_ERRORS

# Stage 4 work item 的输入证据 ID checkpoint keys（spec C：evidence scope 的
# work-item 侧闭包；只在 matched_stage4 存在时使用）。
_WORK_ITEM_EVIDENCE_KEYS = (
    "evidence_card_ids",
    "additional_evidence_ids",
    "macro_driver_evidence_ids",
    "company_evidence_ids",
)

# macro source 投影的 origin_type / source_type（spec F dual-origin）。
_ORIGIN_DOCUMENT = "document_chunk"
_ORIGIN_MACRO = "macro_observation"
_SOURCE_TYPE_MACRO = "macro_series"


def _to_uuids(raw: object) -> list[UUID]:
    """checkpoint 中 UUID 序列化为 str；统一转回 UUID（容忍已为 UUID 的值）。"""
    if not raw:
        return []
    return [value if isinstance(value, UUID) else UUID(value) for value in raw]


async def _guarded(awaitable, errors: tuple[type[Exception], ...], what: str):
    """verify 完整性守卫：域错误树 → 记录并抛 `TaskArtifactIntegrityError`（409）。

    **不泄漏 SQL / stack / 原始异常**——只暴露稳定 DomainError 信封；不 repair。
    """
    try:
        return await awaitable
    except errors as exc:
        logger.warning(
            "artifact_verify_failed",
            artifact=what,
            error_type=type(exc).__name__,
        )
        raise TaskArtifactIntegrityError() from None


@dataclass(frozen=True)
class _Anchor:
    """一次任务级 artifact 解析的 canonical lineage 锚定快照（spec B/C）。"""

    task: ResearchTaskModel
    canonical_synthesis_result_id: UUID | None
    stage5_run: WorkflowRunModel | None
    stage5_state: dict
    matched_stage4_run: WorkflowRunModel | None
    matched_stage4_state: dict
    work_items_available: bool


class TaskArtifactService:
    """任务级只读 artifact workspace；每个方法使用短生命周期 session。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
        *,
        synthesis_service: SynthesisService,
        synthesis_analysis_service: SynthesisAnalysisService,
        report_service,
        report_check_service,
        report_audit_service,
        review_action_service,
        research_backflow_service,
    ) -> None:
        """显式注入 read-side Services（0 LLM；不依赖 DEEPSEEK_API_KEY）。

        `report_*` / `review_action` / `research_backflow` 通常经
        `from_dependencies` 复用 Stage5 装配的同一批 Services。
        """
        self._sessionmaker = sessionmaker
        self._checkpoint_manager = checkpoint_manager
        self._synthesis_service = synthesis_service
        self._synthesis_analysis_service = synthesis_analysis_service
        self._report_service = report_service
        self._report_check_service = report_check_service
        self._report_audit_service = report_audit_service
        self._review_action_service = review_action_service
        self._research_backflow_service = research_backflow_service

    @classmethod
    def from_dependencies(
        cls,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
        deps,
    ) -> "TaskArtifactService":
        """从 Stage5 DI 容器装配（复用同一批 Services，verify 链共享）。

        `SynthesisAnalysisService` / `SynthesisService` 只消费 read-side
        verify，model 恒为 None（0 LLM）。
        """
        return cls(
            sessionmaker,
            checkpoint_manager,
            synthesis_service=SynthesisService(sessionmaker),
            synthesis_analysis_service=SynthesisAnalysisService(sessionmaker),
            report_service=deps.report_service,
            report_check_service=deps.report_check_service,
            report_audit_service=deps.report_audit_service,
            review_action_service=deps.review_action_service,
            research_backflow_service=deps.research_backflow_service,
        )

    # ------------------------------------------------------------------ public API

    async def get_sources(
        self,
        task_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> SourceArtifactListResponse:
        anchor = await self._anchor(task_id)
        verified_result, verified_run = await self._resolve_verified(anchor)
        evidence_ids = await self._evidence_ids(anchor, verified_run)
        all_sources = await self._combined_sources(evidence_ids)
        total = len(all_sources)
        return SourceArtifactListResponse(
            items=all_sources[offset : offset + limit],
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
        verified_result, verified_run = await self._resolve_verified(anchor)
        evidence_ids = await self._evidence_ids(anchor, verified_run)
        claim_ids = set(verified_result.input_claim_ids) if verified_result is not None else set()
        async with self._sessionmaker() as session:
            rows, total = await EvidenceCardRepository(session).list_by_ids(
                sorted(evidence_ids, key=str), limit, offset
            )
            links = await self._load_claim_evidence_links(
                session, [row.evidence_card_id for row in rows], claim_ids
            )
        return EvidenceArtifactListResponse(
            items=[self._map_evidence(row, links) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_analysis(self, task_id: UUID) -> AnalysisArtifactResponse:
        anchor = await self._anchor(task_id)
        verified_result, verified_run = await self._resolve_verified(anchor)

        work_items = (
            self._map_work_items(anchor.matched_stage4_state) if anchor.work_items_available else []
        )
        claims = (
            [self._map_claim(claim) for claim in verified_run.verified_claims]
            if verified_run is not None
            else []
        )

        themes: list[SynthesisThemeArtifact] = []
        conflicts: list[SynthesisConflictArtifact] = []
        evidence_gaps: list[SynthesisEvidenceGapArtifact] = []
        if verified_result is not None:
            themes = [
                self._map_theme(item, verified_result.alias_map)
                for item in verified_result.output.themes
            ]
            conflicts = [
                self._map_conflict(item, verified_result.alias_map)
                for item in verified_result.output.conflicts
            ]
            evidence_gaps = [
                self._map_gap(item, verified_result.alias_map)
                for item in verified_result.output.evidence_gaps
            ]

        return AnalysisArtifactResponse(
            company_id=(
                verified_result.company_id
                if verified_result is not None
                else self._state_uuid(anchor.matched_stage4_state, "company_id")
            ),
            research_question=(
                verified_result.research_question
                if verified_result is not None
                else anchor.matched_stage4_state.get("research_question")
            ),
            analysis_as_of=(
                verified_result.analysis_as_of
                if verified_result is not None
                else self._state_date(anchor.matched_stage4_state, "analysis_as_of")
            ),
            work_items=work_items,
            claims=claims,
            synthesis_id=(
                verified_result.synthesis_id
                if verified_result is not None
                else self._state_uuid(anchor.matched_stage4_state, "synthesis_id")
            ),
            synthesis_result_id=anchor.canonical_synthesis_result_id,
            synthesis_fingerprint=(
                verified_result.synthesis_fingerprint if verified_result is not None else None
            ),
            result_fingerprint=(
                verified_result.result_fingerprint if verified_result is not None else None
            ),
            themes=themes,
            conflicts=conflicts,
            evidence_gaps=evidence_gaps,
            work_items_available=anchor.work_items_available,
        )

    async def get_report(self, task_id: UUID) -> ReportArtifactResponse:
        anchor = await self._anchor(task_id)
        verified = await self._resolve_report(anchor)
        if verified is None:
            return ReportArtifactResponse()
        sections = self._map_report_sections(verified.report_payload.get("sections") or [])
        return ReportArtifactResponse(
            report_id=verified.report_id,
            outline_id=verified.outline_id,
            company_id=verified.company_id,
            research_question_sha256=verified.research_question_sha256,
            analysis_as_of=verified.analysis_as_of,
            report_schema_version=verified.report_schema_version,
            report_fingerprint=verified.report_fingerprint,
            section_count=len(sections),
            sections=sections,
        )

    async def get_reviews(self, task_id: UUID) -> ReviewsArtifactResponse:
        anchor = await self._anchor(task_id)
        audit = await self._resolve_reviews(anchor)
        if audit is None:
            return ReviewsArtifactResponse()
        check = audit.verified_check
        resp = ReviewsArtifactResponse(
            audit_id=audit.audit_id,
            report_id=audit.report_id,
            audit_status=audit.audit_status,
            recommended_route=audit.recommended_route,
            issue_count=audit.issue_count,
            audit_fingerprint=audit.audit_fingerprint,
            issues=[self._map_issue(issue) for issue in audit.issues],
            check=ReportCheckArtifact(
                check_result_id=check.check_result_id,
                status=check.status,
                findings=[self._map_check_finding(finding) for finding in check.findings],
            ),
        )
        resp.review_action = await self._resolve_review_action(anchor)
        resp.human_review = await self._resolve_human_review(anchor)
        resp.research_backflow = await self._resolve_research_backflow(anchor)
        return resp

    # ------------------------------------------------------------------ citation scope（spec J/L）

    async def resolve_evidence_scope(self, task_id: UUID) -> set[UUID]:
        """canonical lineage 的任务级 evidence scope（spec J）。

        Citation API 必须先得到 allowed evidence IDs 再决定 404 / 消费——
        不能仅凭任意 UUID 直接读取全库 Evidence。
        """
        anchor = await self._anchor(task_id)
        verified_result, verified_run = await self._resolve_verified(anchor)
        return await self._evidence_ids(anchor, verified_run)

    async def resolve_claim_scope(self, task_id: UUID) -> set[UUID]:
        """canonical synthesis 的 exact input claim IDs（spec L）。

        Claim Citation 只允许 canonical synthesis input claim；不属于 → 404。
        """
        anchor = await self._anchor(task_id)
        verified_result, _ = await self._resolve_verified(anchor)
        if verified_result is None:
            return set()
        return set(verified_result.input_claim_ids)

    async def resolve_verified_claims(self, task_id: UUID) -> list[VerifiedSynthesisClaim]:
        """canonical synthesis 的 verified claims（供 claim citation 元数据）。

        经 `verify_synthesis_integrity` 重建一致；tamper → 上游 integrity error。
        """
        anchor = await self._anchor(task_id)
        _, verified_run = await self._resolve_verified(anchor)
        if verified_run is None:
            return []
        return list(verified_run.verified_claims)

    # ------------------------------------------------- export lineage（spec H）

    async def anchor_task(self, task_id: UUID) -> ResearchTaskModel:
        """canonical lineage 锚定的 task（供导出服务读 company_query / questions）。"""
        anchor = await self._anchor(task_id)
        return anchor.task

    async def resolve_report(self, task_id: UUID) -> VerifiedReport | None:
        """canonical Stage5 checkpoint 的 verified Report（导出资格判定用）。"""
        anchor = await self._anchor(task_id)
        return await self._resolve_report(anchor)

    async def resolve_reviews(self, task_id: UUID) -> VerifiedReportAudit | None:
        """canonical 的 verified Audit（含 verified Check；导出资格 + 指纹用）。"""
        anchor = await self._anchor(task_id)
        return await self._resolve_reviews(anchor)

    async def resolve_human_decision(self, task_id: UUID) -> VerifiedHumanReviewDecision | None:
        """canonical 的 verified HumanDecision（导出资格 + 指纹用）。

        checkpoint 无 `human_decision_id` → None（audit pass 路径）；有但 verify
        失败 → `TaskArtifactIntegrityError`。
        """
        anchor = await self._anchor(task_id)
        human_decision_id = self._state_uuid(anchor.stage5_state, "human_decision_id")
        if human_decision_id is None:
            return None
        return await _guarded(
            self._review_action_service.verify_human_decision_integrity(human_decision_id),
            _REVIEW_VERIFY_ERRORS,
            "human_decision",
        )

    async def count_artifacts(self, task_id: UUID) -> ArtifactSummary:
        """任务级产物计数（workspace 投影；与各 tab 共用同一 canonical 推导）。"""
        anchor = await self._anchor(task_id)
        verified_result, verified_run = await self._resolve_verified(anchor)
        evidence_ids = await self._evidence_ids(anchor, verified_run)
        sources = await self._combined_sources(evidence_ids)
        report = await self._resolve_report(anchor)
        reviews = await self._resolve_reviews(anchor)
        if verified_result is not None:
            claim_count = len(verified_result.input_claim_ids)
        else:
            claim_count = len(anchor.matched_stage4_state.get("claim_ids") or [])
        return ArtifactSummary(
            source_count=len(sources),
            evidence_count=len(evidence_ids),
            claim_count=claim_count,
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
            stage5_run = await run_repo.get_latest_for_task_by_graph(task_id, STAGE5_GRAPH_NAME)
            stage4_run = await run_repo.get_latest_for_task_by_graph(task_id, STAGE4_GRAPH_NAME)

        stage5_state: dict = {}
        matched_stage4_run: WorkflowRunModel | None = None
        matched_stage4_state: dict = {}
        work_items_available = False

        if stage5_run is not None:
            stage5_state = await self._read_state(stage5_run)
            canonical = self._state_uuid(stage5_state, "synthesis_result_id")
            if canonical is not None:
                matched = await self._find_stage4_for_synthesis(task_id, canonical)
                if matched is not None:
                    matched_stage4_run, matched_stage4_state = matched
                    work_items_available = True
            canonical_synthesis_result_id = canonical
        else:
            # 无 Stage5：最新 Stage4 即 canonical analysis anchor（spec B）。
            if stage4_run is not None:
                matched_stage4_run = stage4_run
                matched_stage4_state = await self._read_state(stage4_run)
                work_items_available = True
            canonical_synthesis_result_id = self._state_uuid(
                matched_stage4_state, "synthesis_result_id"
            )

        return _Anchor(
            task=task,
            canonical_synthesis_result_id=canonical_synthesis_result_id,
            stage5_run=stage5_run,
            stage5_state=stage5_state,
            matched_stage4_run=matched_stage4_run,
            matched_stage4_state=matched_stage4_state,
            work_items_available=work_items_available,
        )

    async def _find_stage4_for_synthesis(
        self,
        task_id: UUID,
        synthesis_result_id: UUID,
    ) -> tuple[WorkflowRunModel, dict] | None:
        """从该 task 的全部 Stage4 run 中选 checkpoint `.synthesis_result_id ==
        canonical` 的那条（research backflow 的新 Synthesis 无匹配 → None 合法）。"""
        async with self._sessionmaker() as session:
            runs = await WorkflowRunRepository(session).list_for_task_by_graph(
                task_id, STAGE4_GRAPH_NAME
            )
        for run in runs:
            state = await self._read_state(run)
            if self._state_uuid(state, "synthesis_result_id") == synthesis_result_id:
                return run, state
        return None

    async def _read_state(self, run: WorkflowRunModel) -> dict:
        """裸 checkpointer 读取（thread_id==run_id），**不 build graph → 0 LLM**。

        `AsyncPostgresSaver` 暴露的是 `aget_tuple`（graph.aget_state 内部就是
        调它），checkpoint 无记录时返回 None → 归一为空 dict。channel_values
        在 `tuple.checkpoint["channel_values"]`。
        """
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        snapshot = await checkpointer.aget_tuple({"configurable": {"thread_id": run.thread_id}})
        if snapshot is None:
            return {}
        return dict((snapshot.checkpoint or {}).get("channel_values") or {})

    @staticmethod
    def _state_uuid(state: dict, field: str) -> UUID | None:
        """checkpoint 里的 id 字段转 UUID；缺失 → None；**存在但不可解析 → 完整性失败**。"""
        raw = state.get(field)
        if raw is None:
            return None
        try:
            return UUID(str(raw))
        except (ValueError, TypeError, AttributeError):
            logger.warning("artifact_state_uuid_invalid", field=field, raw=str(raw)[:64])
            raise TaskArtifactIntegrityError() from None

    @staticmethod
    def _state_date(state: dict, field: str) -> date | None:
        raw = state.get(field)
        if raw is None:
            return None
        try:
            return date.fromisoformat(raw)
        except (ValueError, TypeError, AttributeError):
            logger.warning("artifact_state_date_invalid", field=field, raw=str(raw)[:64])
            raise TaskArtifactIntegrityError() from None

    # ------------------------------------------------------------------ verify 解析

    async def _resolve_verified(
        self,
        anchor: _Anchor,
    ) -> tuple[VerifiedSynthesisResult | None, VerifiedSynthesisRun | None]:
        """canonical synthesis 的 verified 投影（result + run）。

        canonical 缺失 → (None, None)；ID 存在但 verify 重建失败 →
        `TaskArtifactIntegrityError`（spec D，不 repair）。
        """
        canonical = anchor.canonical_synthesis_result_id
        if canonical is None:
            return None, None
        verified_result = await _guarded(
            self._synthesis_analysis_service.verify_result_integrity(canonical),
            _SYNTHESIS_VERIFY_ERRORS,
            "synthesis_result",
        )
        verified_run = await _guarded(
            self._verify_synthesis_run(verified_result.synthesis_id),
            (SynthesisError,),
            "synthesis_run",
        )
        return verified_result, verified_run

    async def _verify_synthesis_run(self, synthesis_id: UUID) -> VerifiedSynthesisRun:
        async with self._sessionmaker() as session:
            return await self._synthesis_service.verify_synthesis_integrity(session, synthesis_id)

    async def _resolve_report(self, anchor: _Anchor) -> VerifiedReport | None:
        report_id = self._state_uuid(anchor.stage5_state, "report_id")
        if report_id is None:
            return None
        return await _guarded(
            self._report_service.verify_report_integrity(report_id),
            _REPORT_VERIFY_ERRORS,
            "report",
        )

    async def _resolve_reviews(self, anchor: _Anchor) -> VerifiedReportAudit | None:
        audit_id = self._state_uuid(anchor.stage5_state, "audit_id")
        if audit_id is None:
            return None
        return await _guarded(
            self._report_audit_service.verify_audit_integrity(audit_id),
            _AUDIT_VERIFY_ERRORS,
            "audit",
        )

    async def _resolve_review_action(self, anchor: _Anchor) -> ReviewActionArtifact | None:
        review_action_id = self._state_uuid(anchor.stage5_state, "review_action_id")
        if review_action_id is None:
            return None
        verified = await _guarded(
            self._review_action_service.verify_review_action_integrity(review_action_id),
            _REVIEW_VERIFY_ERRORS,
            "review_action",
        )
        payload = verified.action_payload or {}
        return ReviewActionArtifact(
            review_action_id=verified.review_action_id,
            action_type=verified.action_type,
            target_section_ids=list(payload.get("target_section_ids", [])),
            issue_count=len(payload.get("review_issue_ids", [])),
        )

    async def _resolve_human_review(self, anchor: _Anchor) -> HumanReviewArtifact | None:
        human_request_id = self._state_uuid(anchor.stage5_state, "human_request_id")
        if human_request_id is None:
            return None
        await _guarded(
            self._review_action_service.verify_human_request_integrity(human_request_id),
            _REVIEW_VERIFY_ERRORS,
            "human_request",
        )
        human_decision_id = self._state_uuid(anchor.stage5_state, "human_decision_id")
        decision = None
        comment = None
        decided_at = None
        if human_decision_id is not None:
            verified_decision = await _guarded(
                self._review_action_service.verify_human_decision_integrity(human_decision_id),
                _REVIEW_VERIFY_ERRORS,
                "human_decision",
            )
            decision = verified_decision.decision
            comment = verified_decision.comment
            decided_at = verified_decision.decided_at
        return HumanReviewArtifact(
            human_request_id=human_request_id,
            decision=decision,
            comment=comment,
            comment_exists=comment is not None,
            decided_at=decided_at,
        )

    async def _resolve_research_backflow(self, anchor: _Anchor) -> ResearchBackflowArtifact | None:
        """Research Backflow 层投影（Gate0-C/D）。

        - checkpoint 有 `research_request_id`（research 路由 run）→ 按 request
          查 fulfillment；存在则 verify request + fulfillment，投影来自 verified；
        - checkpoint 无 `research_request_id`（finalize continuation run）→
          canonical synthesis 就是 fulfillment 的 `new_synthesis_result_id` →
          **task-scoped** 反查：0 行 → 无 backflow；>1 行 → `TaskArtifactIntegrityError`
          （绝不静默选一行）；1 行 → verify request + fulfillment 后投影。

        两条路径都不允许「直接 repo 读 → 投影」：产物行必须经
        `verify_*_integrity` 重建一致，tamper → `TaskArtifactIntegrityError`。
        """
        research_request_id = self._state_uuid(anchor.stage5_state, "research_request_id")
        if research_request_id is None:
            canonical = anchor.canonical_synthesis_result_id
            if canonical is None:
                return None
            async with self._sessionmaker() as session:
                fulfillments = await ResearchBackflowRepository(
                    session
                ).list_fulfillments_by_new_synthesis_result_for_task(canonical, anchor.task.task_id)
            if not fulfillments:
                return None
            if len(fulfillments) > 1:
                logger.warning(
                    "artifact_backflow_ambiguous",
                    task_id=str(anchor.task.task_id),
                    new_synthesis_result_id=str(canonical),
                    count=len(fulfillments),
                )
                raise TaskArtifactIntegrityError()
            fulfillment = fulfillments[0]
            await _guarded(
                self._research_backflow_service.verify_research_request_integrity(
                    fulfillment.research_request_id
                ),
                _BACKFLOW_VERIFY_ERRORS,
                "research_request",
            )
        else:
            await _guarded(
                self._research_backflow_service.verify_research_request_integrity(
                    research_request_id
                ),
                _BACKFLOW_VERIFY_ERRORS,
                "research_request",
            )
            async with self._sessionmaker() as session:
                fulfillment = await ResearchBackflowRepository(
                    session
                ).get_fulfillment_by_request_id(research_request_id)
            if fulfillment is None:
                return ResearchBackflowArtifact(
                    research_request_id=research_request_id,
                    fulfilled=False,
                    fulfillment_id=None,
                    new_synthesis_result_id=None,
                )

        verified_fulfillment = await _guarded(
            self._research_backflow_service.verify_research_fulfillment_integrity(
                fulfillment.fulfillment_id
            ),
            _BACKFLOW_VERIFY_ERRORS,
            "research_fulfillment",
        )
        return ResearchBackflowArtifact(
            research_request_id=verified_fulfillment.research_request_id,
            fulfilled=True,
            fulfillment_id=verified_fulfillment.fulfillment_id,
            new_synthesis_result_id=verified_fulfillment.new_synthesis_result_id,
        )

    # ------------------------------------------------------------------ ID 推导

    async def _evidence_ids(
        self,
        anchor: _Anchor,
        verified_run: VerifiedSynthesisRun | None = None,
    ) -> set[UUID]:
        """任务级 evidence 集（spec C）：canonical Synthesis 的 exact input Claims'
        Evidence closure，+ matched Stage4 work-item 输入证据（仅 work_items_available）。

        **不混用旧 S1 work items**：research backflow 新 Synthesis 无匹配 Stage4
        时只取 verified claims 闭包。
        """
        ids: set[UUID] = set()
        if anchor.work_items_available:
            for item in anchor.matched_stage4_state.get("analysis_work_items") or []:
                for key in _WORK_ITEM_EVIDENCE_KEYS:
                    ids.update(_to_uuids(item.get(key)))
        if verified_run is not None:
            for claim in verified_run.verified_claims:
                ids.update(claim.evidence_card_ids)
        return ids

    async def _combined_sources(self, evidence_ids: set[UUID]) -> list[SourceArtifactResponse]:
        """dual-origin 来源并集（spec F）：document sources + macro sources，去重合并。

        分页在调用方执行；此处返回确定性排序的全集。
        """
        if not evidence_ids:
            return []
        async with self._sessionmaker() as session:
            cards, _ = await EvidenceCardRepository(session).list_by_ids(
                sorted(evidence_ids, key=str), limit=len(evidence_ids), offset=0
            )
            doc_source_ids = {
                card.source_id
                for card in cards
                if card.origin_type == _ORIGIN_DOCUMENT and card.source_id is not None
            }
            macro_obs_ids = {
                card.macro_observation_id
                for card in cards
                if card.origin_type == _ORIGIN_MACRO and card.macro_observation_id is not None
            }
            items: list[SourceArtifactResponse] = []
            if doc_source_ids:
                rows, _ = await SourceRecordRepository(session).list_by_ids(
                    sorted(doc_source_ids, key=str), limit=len(doc_source_ids), offset=0
                )
                items.extend(self._map_document_source(row) for row in rows)
            if macro_obs_ids:
                items.extend(
                    SourceArtifactResponse(**row)
                    for row in (await self._macro_source_rows(session, macro_obs_ids)).values()
                )
        items.sort(
            key=lambda s: (
                (s.published_at or s.fetched_at or s.created_at) is None,
                s.published_at or s.fetched_at or s.created_at,
                str(s.source_identity),
            )
        )
        return items

    @staticmethod
    def _map_document_source(row: SourceRecordModel) -> SourceArtifactResponse:
        return SourceArtifactResponse(
            source_id=row.source_id,
            company_id=row.company_id,
            provider_key=row.provider_key,
            document_type=row.document_type,
            title=row.title,
            published_at=row.published_at,
            reporting_period_end=row.reporting_period_end,
            source_url=row.source_url,
            status=row.status,
            created_at=row.created_at,
            source_identity=f"{row.provider_key}:{row.source_url}",
            origin_type=_ORIGIN_DOCUMENT,
            source_type=row.document_type,
            label=row.title,
            fetched_at=row.acquired_at,
            authority_tier=row.authority_tier_snapshot,
            locator_summary=row.source_url,
        )

    @staticmethod
    async def _macro_source_rows(
        session,
        obs_ids: set[UUID],
    ) -> dict[UUID, dict]:
        """macro observation 闭包 → {series_id: source row dict}（按 series 去重）。

        Observation → Snapshot → Series → Provider；同 series 多个 snapshot 时取
        最新 fetched_at。macro source 的 `source_id=NULL`（spec F：不由
        source_records 承载），source_identity 由 provider/series 身份恢复。
        """
        obs_result = await session.execute(
            select(MacroObservationModel.observation_id, MacroObservationModel.snapshot_id).where(
                MacroObservationModel.observation_id.in_(list(obs_ids))
            )
        )
        snapshot_ids = [sid for _, sid in obs_result.all() if sid is not None]
        if not snapshot_ids:
            return {}

        snap_result = await session.execute(
            select(
                MacroDatasetSnapshotModel.snapshot_id,
                MacroDatasetSnapshotModel.series_id,
                MacroDatasetSnapshotModel.indicator_name,
                MacroDatasetSnapshotModel.source_name,
                MacroDatasetSnapshotModel.geography_name,
                MacroDatasetSnapshotModel.fetched_at,
                MacroDatasetSnapshotModel.authority_tier_snapshot,
                MacroDatasetSnapshotModel.created_at,
            ).where(MacroDatasetSnapshotModel.snapshot_id.in_(snapshot_ids))
        )
        snap_by_series: dict[UUID, object] = {}
        for row in snap_result.all():
            series_id = row.series_id
            if series_id is None:
                continue
            prev = snap_by_series.get(series_id)
            if prev is None or row.fetched_at > prev.fetched_at:
                snap_by_series[series_id] = row
        if not snap_by_series:
            return {}

        series_result = await session.execute(
            select(
                MacroSeriesModel.series_id,
                MacroSeriesModel.provider_key,
                MacroSeriesModel.source_id,
                MacroSeriesModel.external_indicator_id,
                MacroSeriesModel.geography_code,
            ).where(MacroSeriesModel.series_id.in_(list(snap_by_series)))
        )
        rows: dict[UUID, dict] = {}
        for srow in series_result.all():
            snap = snap_by_series.get(srow.series_id)
            if snap is None:
                continue
            rows[srow.series_id] = {
                "source_id": None,
                "company_id": None,
                "provider_key": srow.provider_key,
                "document_type": None,
                "title": snap.indicator_name,
                "published_at": None,
                "reporting_period_end": None,
                "source_url": None,
                "status": "available",
                "created_at": snap.created_at,
                "source_identity": (
                    f"{srow.provider_key}:{srow.external_indicator_id}:{srow.geography_code}"
                ),
                "origin_type": _ORIGIN_MACRO,
                "source_type": _SOURCE_TYPE_MACRO,
                "label": snap.geography_name,
                "fetched_at": snap.fetched_at,
                "authority_tier": snap.authority_tier_snapshot,
                "locator_summary": f"{snap.source_name}｜{snap.indicator_name}",
            }
        return rows

    async def _load_claim_evidence_links(
        self,
        session,
        card_ids: list[UUID],
        claim_ids: set[UUID],
    ) -> dict[UUID, list[ClaimEvidenceRelation]]:
        """canonical synthesis 的 exact input Claims ↔ Evidence 关系（spec G）。

        只投影 canonical input Claims 对当前页 evidence 卡的关系；绝不混入旧
        synthesis 的 claim。
        """
        if not card_ids or not claim_ids:
            return {}
        result = await session.execute(
            select(
                ClaimEvidenceLinkModel.claim_id,
                ClaimEvidenceLinkModel.evidence_card_id,
                ClaimEvidenceLinkModel.relation,
            ).where(
                ClaimEvidenceLinkModel.evidence_card_id.in_(card_ids),
                ClaimEvidenceLinkModel.claim_id.in_(sorted(claim_ids, key=str)),
            )
        )
        by_card: dict[UUID, list[ClaimEvidenceRelation]] = {}
        for claim_id, card_id, relation in result.all():
            by_card.setdefault(card_id, []).append(
                ClaimEvidenceRelation(claim_id=claim_id, relation=relation)
            )
        return by_card

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _map_evidence(row: EvidenceCardModel, links: dict) -> EvidenceArtifactResponse:
        relations = links.get(row.evidence_card_id, [])
        return EvidenceArtifactResponse(
            evidence_card_id=row.evidence_card_id,
            source_id=row.source_id,
            company_id=row.company_id,
            evidence_statement=row.evidence_statement,
            evidence_type=row.evidence_type,
            extractor_confidence=row.extractor_confidence,
            quote_text=row.quote_text,
            origin_type=row.origin_type,
            created_at=row.created_at,
            used_by_claim_ids=sorted({rel.claim_id for rel in relations}, key=str),
            claim_relations=sorted(relations, key=lambda r: (str(r.claim_id), r.relation)),
            macro_observation_id=row.macro_observation_id,
            macro_snapshot_id=row.macro_snapshot_id,
            macro_series_id=row.macro_series_id,
        )

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
    def _map_theme(theme: SynthesisTheme, alias_map: dict[str, UUID]) -> SynthesisThemeArtifact:
        return SynthesisThemeArtifact(
            title=theme.title,
            summary=theme.summary,
            claim_ids=[alias_map[ref] for ref in theme.claim_refs],
        )

    @staticmethod
    def _map_conflict(
        conflict: SynthesisConflict,
        alias_map: dict[str, UUID],
    ) -> SynthesisConflictArtifact:
        return SynthesisConflictArtifact(
            claim_ids=[alias_map[ref] for ref in conflict.claim_refs],
            description=conflict.description,
            severity=conflict.severity.value,
            resolution_direction=conflict.resolution_direction,
        )

    @staticmethod
    def _map_gap(
        gap: SynthesisEvidenceGap,
        alias_map: dict[str, UUID],
    ) -> SynthesisEvidenceGapArtifact:
        return SynthesisEvidenceGapArtifact(
            description=gap.description,
            claim_ids=[alias_map[ref] for ref in gap.claim_refs],
            suggested_evidence=gap.suggested_evidence,
            priority=gap.priority.value,
        )

    @staticmethod
    def _map_report_sections(raw_sections: list[dict]) -> list[ReportSectionArtifact]:
        sections: list[ReportSectionArtifact] = []
        for index, section in enumerate(raw_sections):
            sections.append(
                ReportSectionArtifact(
                    section_id=str(section.get("section_id", "")),
                    draft_section_id=(
                        UUID(section["draft_section_id"])
                        if section.get("draft_section_id")
                        else None
                    ),
                    section_order=section.get("section_order", index + 1),
                    section_type=section.get("section_type", ""),
                    title=section.get("title", ""),
                    paragraphs=[
                        TaskArtifactService._map_paragraph(paragraph, p_index)
                        for p_index, paragraph in enumerate(section.get("paragraphs") or [])
                    ],
                )
            )
        return sections

    @staticmethod
    def _map_paragraph(paragraph: dict, index: int) -> ReportParagraphArtifact:
        return ReportParagraphArtifact(
            paragraph_index=index,
            text=paragraph.get("text", ""),
            claim_ids=[UUID(raw) for raw in (paragraph.get("claim_ids") or [])],
            evidence_card_ids=[UUID(raw) for raw in (paragraph.get("evidence_card_ids") or [])],
            conflict_indexes=list(paragraph.get("conflict_indexes") or []),
            evidence_gap_indexes=list(paragraph.get("evidence_gap_indexes") or []),
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

    @staticmethod
    def _map_check_finding(finding: CheckFinding) -> CheckFindingArtifact:
        return CheckFindingArtifact(
            code=finding.code,
            section_id=finding.section_id,
            paragraph_index=finding.paragraph_index,
            related_claim_ids=list(finding.related_claim_ids),
            related_evidence_card_ids=list(finding.related_evidence_card_ids),
        )
