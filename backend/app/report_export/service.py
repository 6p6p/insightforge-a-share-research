"""Deterministic report export service (stage 6C spec H/I/J/M/N).

角色边界（Export 是**确定性导出**，不是 LLM 判断，spec F）：
- 0 LLM / 0 Retrieval / 0 Chroma / 0 Web——renderer 只消费 `ExportReportPack`
  纯结构；本服务只做 lineage 恢复 / 资格判定 / 指纹 / replay / 内容寻址归档 /
  完整性校验；
- `create_or_get_export(task_id, format)`：canonical lineage → 资格判定（spec H：
  check pass + (audit pass/route pass) 或 (audit fail/route human_review +
  人工 approve)）→ 引用编号 + ExportReportPack → 指纹 → replay（同输入 → 同
  行/字节，并发 → 1 行）→ renderer → `ExportArtifactStore` 内容寻址 → DB
  create-or-get；
- `verify_export_integrity(export_id)`：按导出**自己引用的** Report / Check /
  Audit / HumanDecision 独立重验（不依赖 task canonical lineage 是否变化）→
  重建 pack → 重算指纹 → 读归档字节比对 content_sha256 / byte_size / media_type /
  format。只验证、**不 repair**（spec N）。
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import BinaryIO
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.synthesis.errors import SynthesisAnalysisError
from app.audit.contracts import (
    AUDIT_ROUTE_HUMAN_REVIEW,
    AUDIT_ROUTE_PASS,
    AUDIT_STATUS_FAIL,
    AUDIT_STATUS_PASS,
    VerifiedReportAudit,
)
from app.audit.errors import ReportAuditError
from app.core.errors import CompanyIdentityNotFound, TaskNotFound
from app.db.models.report_export import ReportExportModel
from app.db.models.research_task import ResearchTaskModel
from app.draft_section.errors import DraftSectionError
from app.evidence.errors import EvidenceProvenanceIntegrityError
from app.evidence.provenance_service import EvidenceProvenanceService
from app.report.contracts import CHECK_STATUS_PASS, VerifiedReport
from app.report.errors import ReportError
from app.report_export.contracts import (
    EXPORT_FORMATS,
    EXTENSION_BY_FORMAT,
    MEDIA_TYPE_BY_FORMAT,
    RENDERER_NAME_BY_FORMAT,
    RENDERER_VERSION_BY_FORMAT,
    ExportReportPack,
    compute_export_input_fingerprint,
)
from app.report_export.errors import (
    ReportExportError,
    ReportExportIntegrityError,
    ReportExportNotFound,
    ReportNotExportable,
)
from app.report_export.pack import (
    AUDIT_NOTE_BACKFLOW_ACCEPTED,
    AUDIT_NOTE_HUMAN_APPROVED,
    ExportCardDetail,
    build_export_report_pack,
)
from app.report_export.renderers import render_export
from app.report_outline.errors import ReportOutlineError
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.report_export_repository import ReportExportRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.review.contracts import (
    HUMAN_DECISION_APPROVE,
    VerifiedHumanReviewDecision,
)
from app.review.errors import ReviewError
from app.schemas.citation import DocumentProvenance, MacroProvenance
from app.schemas.company import CompanyIdentityResponse
from app.services.company_identity_service import CompanyIdentityService
from app.services.task_artifact_service import TaskArtifactService
from app.storage.export_store import ExportArtifactStore
from app.synthesis.errors import SynthesisError

logger = logging.getLogger("app.report_export")

# verify 链可能向上传播的域错误树（与 TaskArtifactService 对齐；tamper → 导出
# 完整性错误，不泄漏 SQL / stack / 原始异常）。
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


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果摘要（POST /tasks/{id}/export）。"""

    export_id: UUID
    format: str
    file_name: str
    media_type: str
    byte_size: int
    replayed: bool
    created_at: datetime


@dataclass(frozen=True)
class ExportRecord:
    """一次导出的 metadata 投影（GET /tasks/{id}/exports/{export_id}）。"""

    export_id: UUID
    task_id: UUID
    report_id: UUID
    format: str
    file_name: str
    media_type: str
    byte_size: int
    content_sha256: str
    created_at: datetime


@dataclass(frozen=True)
class VerifiedExport:
    """`verify_export_integrity` 的 read-side 产物（下载前必须校验通过）。"""

    record: ExportRecord
    storage_key: str


class ReportExportService:
    """确定性导出服务；短生命周期 session，0 LLM。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        task_artifact_service: TaskArtifactService,
        *,
        report_service,
        report_check_service,
        report_audit_service,
        review_action_service,
        company_service: CompanyIdentityService,
        export_store: ExportArtifactStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._task_artifact_service = task_artifact_service
        self._report_service = report_service
        self._report_check_service = report_check_service
        self._report_audit_service = report_audit_service
        self._review_action_service = review_action_service
        self._company_service = company_service
        self._export_store = export_store
        self._provenance_service = EvidenceProvenanceService()

    # ------------------------------------------------------------------ create / replay

    async def create_or_get_export(self, task_id: UUID, format: str) -> ExportResult:
        """资格判定 + pack 构建 + 指纹 + replay → render → 内容寻址 → create-or-get。

        - 不满足资格 → `ReportNotExportable`（409，spec H）；
        - 同输入（指纹相同）→ 返回已有行（replayed=True，不重复渲染 / 不建行）；
        - 并发同输入 → ON CONFLICT 保证 1 行（loser 复用 winner 字节）。
        """
        if format not in EXPORT_FORMATS:
            raise ReportExportError(f"不支持的导出格式: {format}")

        verified_report = await self._task_artifact_service.resolve_report(task_id)
        verified_audit = await self._task_artifact_service.resolve_reviews(task_id)
        verified_decision = await self._task_artifact_service.resolve_human_decision(task_id)
        if verified_report is None or verified_audit is None:
            raise ReportNotExportable()

        backflow_accepted = await self._backflow_accept_for_task(task_id)
        audit_note, eligible = _eligibility(
            verified_audit, verified_decision, backflow_accepted=backflow_accepted
        )
        if not eligible or verified_audit.verified_check.status != CHECK_STATUS_PASS:
            raise ReportNotExportable()

        task = await self._task_artifact_service.anchor_task(task_id)
        company = await self._resolve_company(verified_report)
        pack = await self._build_pack(
            verified_report=verified_report,
            task=task,
            company=company,
            audit_note=audit_note,
            verified_audit=verified_audit,
            verified_decision=verified_decision,
        )

        fingerprint = _compute_fingerprint(pack, format)
        file_name = _file_name(pack, format)
        media_type = MEDIA_TYPE_BY_FORMAT[format]

        async with self._sessionmaker() as session:
            repo = ReportExportRepository(session)
            existing = await repo.get_by_fingerprint(fingerprint)
            if existing is not None:
                return ExportResult(
                    export_id=existing.export_id,
                    format=existing.export_format,
                    file_name=existing.file_name,
                    media_type=existing.media_type,
                    byte_size=existing.byte_size,
                    replayed=True,
                    created_at=existing.created_at,
                )

            payload = render_export(pack, format)
            content_sha256 = hashlib.sha256(payload).hexdigest()
            stored = self._export_store.put_bytes(payload, EXTENSION_BY_FORMAT[format])
            if stored.content_sha256 != content_sha256:
                raise ReportExportIntegrityError()

            row = await repo.create_or_get(
                export_id=uuid.uuid4(),
                task_id=task.task_id,
                report_id=pack.report_id,
                check_result_id=pack.check_result_id or UUID(int=0),
                audit_id=pack.audit_id or UUID(int=0),
                human_decision_id=pack.human_decision_id,
                export_schema_version=pack.export_schema_version,
                export_format=format,
                export_input_fingerprint=fingerprint,
                content_sha256=content_sha256,
                byte_size=stored.byte_size,
                media_type=media_type,
                file_name=file_name,
                storage_key=stored.storage_key,
            )
            await session.commit()

        return ExportResult(
            export_id=row.export_id,
            format=row.export_format,
            file_name=row.file_name,
            media_type=row.media_type,
            byte_size=row.byte_size,
            replayed=False,
            created_at=row.created_at,
        )

    # ------------------------------------------------------------------ read / verify

    async def get_export(self, task_id: UUID, export_id: UUID) -> ExportRecord:
        """task-scoped metadata（不属于该 task → 404，spec P）。"""
        row = await self._load_export_row(task_id, export_id)
        return _record_of(row)

    async def get_export_content(
        self, task_id: UUID, export_id: UUID
    ) -> tuple[ExportRecord, BinaryIO]:
        """下载前必须 verify（spec N）；验证通过 → 返回 (record, 字节流)。

        验证失败 / 字节缺失 → `ReportExportIntegrityError` / `ExportArtifactNotFound`。
        """
        verified = await self.verify_export_integrity(export_id)
        if verified.record.task_id != task_id:
            raise ReportExportNotFound()
        stream = self._export_store.open(verified.storage_key)
        return verified.record, stream

    async def verify_export_integrity(self, export_id: UUID) -> VerifiedExport:
        """按导出自己引用的 Report / Check / Audit / HumanDecision 独立重验。

        rebuild pack → 重算 input fingerprint → 读归档字节比对 content_sha256 /
        byte_size / media_type / format。任一不一致 → `ReportExportIntegrityError`，
        **不 repair**（spec N）。
        """
        async with self._sessionmaker() as session:
            row = await ReportExportRepository(session).get_by_id(export_id)
            if row is None:
                raise ReportExportNotFound()

            verified_report = await _guarded_verify(
                self._report_service.verify_report_integrity(row.report_id),
                _REPORT_VERIFY_ERRORS,
                "report",
            )
            verified_check = await _guarded_verify(
                self._report_check_service.verify_check_result_integrity(row.check_result_id),
                _REPORT_VERIFY_ERRORS,
                "check",
            )
            verified_audit = await _guarded_verify(
                self._report_audit_service.verify_audit_integrity(row.audit_id),
                _AUDIT_VERIFY_ERRORS,
                "audit",
            )
            verified_decision: VerifiedHumanReviewDecision | None = None
            if row.human_decision_id is not None:
                verified_decision = await _guarded_verify(
                    self._review_action_service.verify_human_decision_integrity(
                        row.human_decision_id
                    ),
                    _REVIEW_VERIFY_ERRORS,
                    "human_decision",
                )

            if verified_audit.report_id != row.report_id:
                raise ReportExportIntegrityError()
            if verified_audit.check_result_id != row.check_result_id:
                raise ReportExportIntegrityError()
            if (
                verified_audit.verified_report.report_fingerprint
                != verified_report.report_fingerprint
            ):
                raise ReportExportIntegrityError()
            if verified_audit.verified_check.check_result_id != row.check_result_id:
                raise ReportExportIntegrityError()

            task = await ResearchTaskRepository(session).get_by_id(row.task_id)
            if task is None:
                raise TaskNotFound()
            company = await self._resolve_company(verified_report)

            backflow_accepted = await self._backflow_accept_for_task(row.task_id)
            audit_note, eligible = _eligibility(
                verified_audit, verified_decision, backflow_accepted=backflow_accepted
            )
            if not eligible or verified_check.status != CHECK_STATUS_PASS:
                raise ReportExportIntegrityError()

            pack = await self._build_pack(
                verified_report=verified_report,
                task=task,
                company=company,
                audit_note=audit_note,
                verified_audit=verified_audit,
                verified_decision=verified_decision,
            )
            if (
                pack.check_result_id != row.check_result_id
                or pack.audit_id != row.audit_id
                or pack.human_decision_id != row.human_decision_id
            ):
                raise ReportExportIntegrityError()

            recomputed = _compute_fingerprint(pack, row.export_format)
            if recomputed != row.export_input_fingerprint:
                raise ReportExportIntegrityError()

            if row.export_format not in EXPORT_FORMATS:
                raise ReportExportIntegrityError()
            if MEDIA_TYPE_BY_FORMAT[row.export_format] != row.media_type:
                raise ReportExportIntegrityError()

            with self._export_store.open(row.storage_key) as handle:
                stored = handle.read()
            if hashlib.sha256(stored).hexdigest() != row.content_sha256:
                raise ReportExportIntegrityError()
            if len(stored) != row.byte_size:
                raise ReportExportIntegrityError()

        return VerifiedExport(record=_record_of(row), storage_key=row.storage_key)

    # ------------------------------------------------------------------ internals

    async def _resolve_company(
        self, verified_report: VerifiedReport
    ) -> CompanyIdentityResponse | None:
        try:
            return await self._company_service.get_company(verified_report.company_id)
        except CompanyIdentityNotFound:
            # 公司行缺失 → 回退 task.company_query（pack 层 fallback，spec I）。
            # 其余异常（如 DB 故障）原样上抛，不掩盖真实错误。
            logger.warning(
                "export_company_missing",
                company_id=str(verified_report.company_id),
            )
            return None

    async def _build_pack(
        self,
        *,
        verified_report: VerifiedReport,
        task: ResearchTaskModel,
        company: CompanyIdentityResponse | None,
        audit_note: str | None,
        verified_audit: VerifiedReportAudit,
        verified_decision: VerifiedHumanReviewDecision | None,
    ) -> ExportReportPack:
        details, provenance = await self._load_evidence_details(verified_report)
        pack = build_export_report_pack(
            verified_report=verified_report,
            task=task,
            company=company,
            cards_by_id=details,
            provenance_by_card=provenance,
            audit_note=audit_note,
        )
        return replace(
            pack,
            check_result_id=verified_audit.verified_check.check_result_id,
            check_fingerprint=verified_audit.verified_check.check_fingerprint,
            audit_id=verified_audit.audit_id,
            audit_fingerprint=verified_audit.audit_fingerprint,
            human_decision_id=(
                verified_decision.human_decision_id if verified_decision is not None else None
            ),
            decision_fingerprint=(
                verified_decision.decision_fingerprint if verified_decision is not None else None
            ),
        )

    async def _load_evidence_details(
        self, verified_report: VerifiedReport
    ) -> tuple[dict[UUID, ExportCardDetail], dict[UUID, DocumentProvenance | MacroProvenance]]:
        """报告引用的 evidence 卡 → (card detail, verified provenance)。

        任何引用卡缺失 / provenance 链断裂 → `ReportExportIntegrityError`
        （export 要求 check=pass，闭包必须完整；tamper → 不静默降级）。
        """
        referenced: set[UUID] = set()
        for section in verified_report.report_payload.get("sections") or []:
            for paragraph in section.get("paragraphs") or []:
                referenced.update(UUID(raw) for raw in (paragraph.get("evidence_card_ids") or []))
        if not referenced:
            return {}, {}

        async with self._sessionmaker() as session:
            cards, _ = await EvidenceCardRepository(session).list_by_ids(
                sorted(referenced, key=str), limit=len(referenced), offset=0
            )
            cards_by_id = {card.evidence_card_id: card for card in cards}
            missing = referenced - set(cards_by_id)
            if missing:
                logger.warning(
                    "export_evidence_missing",
                    count=len(missing),
                    first=str(sorted(missing, key=str)[0]),
                )
                raise ReportExportIntegrityError()

            details: dict[UUID, ExportCardDetail] = {}
            provenance: dict[UUID, DocumentProvenance | MacroProvenance] = {}
            for card_id in sorted(referenced, key=str):
                card = cards_by_id[card_id]
                details[card_id] = ExportCardDetail(
                    evidence_card_id=card.evidence_card_id,
                    statement=card.evidence_statement,
                    quote_text=card.quote_text,
                    origin_type=card.origin_type,
                )
                try:
                    provenance[card_id] = await self._provenance_service.resolve(session, card)
                except EvidenceProvenanceIntegrityError:
                    raise ReportExportIntegrityError() from None
        return details, provenance

    async def _backflow_accept_for_task(self, task_id: UUID) -> bool:
        """task 的（active/最新）orchestration 是否已有 backflow closure accept。

        只读 closure 决策；任何解析失败（行缺失）→ False（export 资格不因
        closure 侧异常而放宽）。accept 守卫已在服务层保证不含 critical
        integrity failure。
        """
        from app.repositories.backflow_review_repository import BackflowReviewRepository
        from app.research_orchestration.repository import ResearchOrchestrationRepository

        try:
            async with self._sessionmaker() as session:
                orchestration = await ResearchOrchestrationRepository(session).get_active_for_task(
                    task_id
                )
                if orchestration is None:
                    orchestration = await ResearchOrchestrationRepository(
                        session
                    ).get_latest_for_task(task_id)
                if orchestration is None:
                    return False
                request = await BackflowReviewRepository(session).get_by_orchestration(
                    orchestration.orchestration_id
                )
                if request is None:
                    return False
                decision = await BackflowReviewRepository(session).get_decision_by_request(
                    request.backflow_human_request_id
                )
        except Exception:  # noqa: BLE001 - 只读资格判定，不因闭包侧异常放宽
            return False
        return decision is not None and decision.decision == "accept"

    async def _load_export_row(self, task_id: UUID, export_id: UUID) -> ReportExportModel:
        async with self._sessionmaker() as session:
            row = await ReportExportRepository(session).get_by_id(export_id)
        if row is None or row.task_id != task_id:
            raise ReportExportNotFound()
        return row


def _eligibility(
    audit: VerifiedReportAudit,
    decision: VerifiedHumanReviewDecision | None,
    *,
    backflow_accepted: bool = False,
) -> tuple[str | None, bool]:
    """spec H 资格判定：返回 (audit_note, eligible)。

    A. audit pass + route pass → 可导出，无 audit_note；
    B. audit fail + route human_review + 人工 approve → 可导出，audit_note 固定文案；
    C. audit fail + backflow manual closure accept（补充研究已达上限，用户接受当前
       报告；accept 守卫已保证不含 critical integrity failure）→ 可导出，固定文案。
       确定性 Check=pass 由调用方另行强制；critical 守卫在 accept 时已拒绝，此处
       只需读 closure 决策。
    其余（rewrite / research / waiting_human / 无 audit）→ 不可导出。
    """
    if audit.audit_status == AUDIT_STATUS_PASS and audit.recommended_route == AUDIT_ROUTE_PASS:
        return None, True
    if (
        audit.audit_status == AUDIT_STATUS_FAIL
        and audit.recommended_route == AUDIT_ROUTE_HUMAN_REVIEW
        and decision is not None
        and decision.decision == HUMAN_DECISION_APPROVE
    ):
        return AUDIT_NOTE_HUMAN_APPROVED, True
    if audit.audit_status == AUDIT_STATUS_FAIL and backflow_accepted:
        return AUDIT_NOTE_BACKFLOW_ACCEPTED, True
    return None, False


async def _guarded_verify(awaitable, errors: tuple[type, ...], what: str):
    """verify 完整性守卫：域错误树 → `ReportExportIntegrityError`（不泄漏）。"""
    try:
        return await awaitable
    except errors as exc:
        logger.warning("export_verify_failed", extra={"artifact": what, "exc": type(exc).__name__})
        raise ReportExportIntegrityError() from None


def _compute_fingerprint(pack: ExportReportPack, format: str) -> str:
    return compute_export_input_fingerprint(
        export_schema_version=pack.export_schema_version,
        task_id=pack.task_id,
        report_id=pack.report_id,
        report_fingerprint=pack.report_fingerprint,
        check_result_id=pack.check_result_id or UUID(int=0),
        check_fingerprint=pack.check_fingerprint,
        audit_id=pack.audit_id or UUID(int=0),
        audit_fingerprint=pack.audit_fingerprint,
        human_decision_id=pack.human_decision_id,
        decision_fingerprint=pack.decision_fingerprint,
        format=format,
        renderer_name=RENDERER_NAME_BY_FORMAT[format],
        renderer_version=RENDERER_VERSION_BY_FORMAT[format],
        pack_identity=pack.to_identity_dict(),
    )


def _file_name(pack: ExportReportPack, format: str) -> str:
    return f"report_{pack.report_id}.{EXTENSION_BY_FORMAT[format]}"


def _record_of(row: ReportExportModel) -> ExportRecord:
    return ExportRecord(
        export_id=row.export_id,
        task_id=row.task_id,
        report_id=row.report_id,
        format=row.export_format,
        file_name=row.file_name,
        media_type=row.media_type,
        byte_size=row.byte_size,
        content_sha256=row.content_sha256,
        created_at=row.created_at,
    )
