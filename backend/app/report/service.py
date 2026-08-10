"""Deterministic report assembly service (stage 5C, spec G/O/P): 0 LLM.

流程（短 DB session + 纯函数，镜像 ReportOutlineService / DraftSectionService）：
1. 防御性 request 校验（构造已校验，服务层再兜底）；
2. **short DB verify artifacts**：`ReportOutlineService.verify_outline_integrity`
   （read-side 公共 API）→ 关闭 session；
3. 逐个 `DraftSectionService.verify_draft_section_integrity(id)`（read-side 公共
   API，各自短 session）→ 每个 DraftSection 的 outline_id 必须等于 input outline；
4. **short DB session**：加载 selected DraftSections 的 `section_payload`（拼装正文
   用；VerifiedDraftSection 只含身份 / 指纹 / 段落计数，不含正文）→ 关闭 session；
5. 纯函数 `assemble_report_payload`（coverage / identity 硬边界，按 Outline order）
   → `compute_report_fingerprint`（含 selected draft sections 指纹数据，spec N）；
6. **short transaction create_or_get**（ON CONFLICT DO NOTHING，无进程锁）→ 并发
   同输入 → 1 个 Report；命中时 replay 校验（同指纹行 payload 与本次派生不一致 →
   `ReportIntegrityError`）；SQLAlchemyError → rollback + `ReportPersistenceFailed`。

**公共 read-side**：`verify_report_integrity(report_id)`——重新 verify Outline +
全部 selected DraftSections + rebuild exact payload + 重算 fingerprint；任一
text / section / draft id / metadata / fingerprint 被 SQL tamper →
`ReportIntegrityError`（**不自动 repair**）。

**不创建 CheckResult / Audit**；不接 LangGraph；不调用 Retrieval / Chroma / LLM /
tools / web search。caller 只提供 `outline_id` + 显式 `draft_section_ids`（spec L），
其余全部派生。
"""

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.draft_section import DraftSectionModel
from app.db.models.report import ReportModel
from app.draft_section.contracts import VerifiedDraftSection
from app.draft_section.service import DraftSectionService
from app.report.assemble import (
    AssembledSectionDraft,
    assemble_report_payload,
    draft_section_fingerprint_data,
    extract_draft_section_ids,
)
from app.report.contracts import (
    REPORT_SCHEMA_VERSION,
    ReportAssemblyDraft,
    ReportResult,
    VerifiedReport,
    compute_report_fingerprint,
)
from app.report.errors import (
    ReportAssemblyError,
    ReportIntegrityError,
    ReportNotFound,
    ReportPersistenceFailed,
)
from app.report.repository import ReportRepository
from app.report_outline.contracts import VerifiedReportOutline
from app.report_outline.service import ReportOutlineService


class ReportService:
    """Deterministic Report：verified Outline + explicit DraftSections → Report（0 LLM）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        draft_section_service: DraftSectionService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._draft_section_service = draft_section_service
        self._outline_service = ReportOutlineService(sessionmaker)

    async def create_or_get_report(self, draft: ReportAssemblyDraft) -> ReportResult:
        """装配一次确定性 Report；同 outline + 同 selected drafts → replay 同一行。"""
        self._check_draft(draft)

        # 2. verify outline（read-side 公共 API，短 session）。
        verified_outline = await self._outline_service.verify_outline_integrity(draft.outline_id)

        # 3-4. 逐个 verify DraftSection + 加载 section_payload（short DB session）。
        assembled = await self._verify_and_load(draft.draft_section_ids, verified_outline)

        # 5. 纯函数拼装 + 指纹（DB session 已关闭）。
        payload = assemble_report_payload(verified_outline=verified_outline, drafts=assembled)
        fingerprint = compute_report_fingerprint(
            report_schema_version=REPORT_SCHEMA_VERSION,
            outline_id=verified_outline.outline_id,
            outline_fingerprint=verified_outline.outline_fingerprint,
            company_id=verified_outline.company_id,
            research_question_sha256=verified_outline.research_question_sha256,
            analysis_as_of=verified_outline.analysis_as_of,
            draft_sections=draft_section_fingerprint_data(assembled),
            report_payload=payload,
        )
        expected = self._report_model(verified_outline, payload, fingerprint)

        # 6. short transaction create_or_get（原子）+ replay 校验。
        async with self._sessionmaker() as session:
            try:
                row, was_created = await ReportRepository(session).create_or_get(expected)
                if not was_created:
                    self._verify_replay(row, expected)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReportPersistenceFailed() from exc

        return ReportResult(
            report_id=row.report_id,
            outline_id=row.outline_id,
            company_id=row.company_id,
            research_question_sha256=row.research_question_sha256,
            analysis_as_of=row.analysis_as_of,
            report_schema_version=row.report_schema_version,
            report_fingerprint=row.report_fingerprint,
            replayed=not was_created,
            section_count=len(payload["sections"]),
        )

    # ------------------------------------------- public read-side verify (Stage 5C checks)

    async def verify_report_integrity(self, report_id: UUID) -> VerifiedReport:
        """公共 read-only 完整性校验（Stage 5C checks 的 verified 输入）。

        流程（短 DB session + 纯函数，**0 LLM / 0 写**）：
        1. 短 session 加载 Report 行；缺失 → `ReportNotFound`；
        2. verify outline（read-side 公共 API）→ 从 **persisted payload** 提取
           selected draft_section_id（按 section order）；
        3. 逐个 `DraftSectionService.verify_draft_section_integrity`（read-side
           公共 API，**不复制** replay 逻辑）→ 上游 DraftSection 损坏 →
           `DraftSectionIntegrityError` / legacy → `DraftSectionLegacyVersionUnsupported`
           （不自动 repair）；
        4. 加载 selected DraftSections 的 section_payload → **rebuild exact payload**
           （纯函数 `assemble_report_payload`）；
        5. 与 persisted 逐一对比（metadata 5 字段 + report_payload +
           report_fingerprint），任一不同 → `ReportIntegrityError`；
        6. 返回 `VerifiedReport`（含 verified_outline + verified_drafts）。

        从**重派生** payload 出发，与 persisted 逐字段对比（等于 persisted 才通过）。
        **不 repair / 不 update**——checks 只消费本投影，不直接相信 report_payload。
        """
        async with self._sessionmaker() as session:
            row = await ReportRepository(session).get_by_id(report_id)
            if row is None:
                raise ReportNotFound()

        verified_outline = await self._outline_service.verify_outline_integrity(row.outline_id)
        draft_section_ids = extract_draft_section_ids(row.report_payload)
        assembled = await self._verify_and_load(draft_section_ids, verified_outline)
        payload = assemble_report_payload(verified_outline=verified_outline, drafts=assembled)
        fingerprint = compute_report_fingerprint(
            report_schema_version=REPORT_SCHEMA_VERSION,
            outline_id=verified_outline.outline_id,
            outline_fingerprint=verified_outline.outline_fingerprint,
            company_id=verified_outline.company_id,
            research_question_sha256=verified_outline.research_question_sha256,
            analysis_as_of=verified_outline.analysis_as_of,
            draft_sections=draft_section_fingerprint_data(assembled),
            report_payload=payload,
        )
        checks = [
            (row.outline_id, verified_outline.outline_id, "outline_id"),
            (row.company_id, verified_outline.company_id, "company_id"),
            (
                row.research_question_sha256,
                verified_outline.research_question_sha256,
                "research_question_sha256",
            ),
            (row.analysis_as_of, verified_outline.analysis_as_of, "analysis_as_of"),
            (row.report_schema_version, REPORT_SCHEMA_VERSION, "report_schema_version"),
            (row.report_payload, payload, "report_payload"),
            (row.report_fingerprint, fingerprint, "report_fingerprint"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise ReportIntegrityError(f"report {field} mismatch")

        return VerifiedReport(
            report_id=row.report_id,
            outline_id=row.outline_id,
            company_id=row.company_id,
            research_question_sha256=row.research_question_sha256,
            analysis_as_of=row.analysis_as_of,
            report_schema_version=row.report_schema_version,
            report_fingerprint=row.report_fingerprint,
            report_payload=payload,
            verified_outline=verified_outline,
            verified_drafts=tuple(item.verified for item in assembled),
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_draft(draft: ReportAssemblyDraft) -> None:
        # 构造时已校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if isinstance(draft.outline_id, bool) or not isinstance(draft.outline_id, UUID):
            raise ReportAssemblyError("outline_id 必须是 UUID")
        if not isinstance(draft.draft_section_ids, tuple) or not draft.draft_section_ids:
            raise ReportAssemblyError("draft_section_ids 必须是非空 tuple")

    async def _verify_and_load(
        self,
        draft_section_ids: tuple[UUID, ...],
        verified_outline: VerifiedReportOutline,
    ) -> list[AssembledSectionDraft]:
        """逐个 verify DraftSection + 加载 section_payload（short DB session）。

        每个 DraftSection 的 outline_id 必须等于 input outline（spec K）。DraftSection
        完整性 / not-found / legacy 错误由 `DraftSectionService` 原样向上传播。
        """
        verified: list[VerifiedDraftSection] = []
        for draft_section_id in draft_section_ids:
            item = await self._draft_section_service.verify_draft_section_integrity(
                draft_section_id
            )
            if item.outline_id != verified_outline.outline_id:
                raise ReportAssemblyError(
                    f"draft section {draft_section_id} belongs to a different outline "
                    f"than {verified_outline.outline_id}"
                )
            verified.append(item)

        payloads = await self._load_section_payloads(
            tuple(item.draft_section_id for item in verified)
        )
        return [
            AssembledSectionDraft(verified=item, section_payload=payloads[item.draft_section_id])
            for item in verified
        ]

    async def _load_section_payloads(self, draft_section_ids: tuple[UUID, ...]) -> dict:
        """短 DB session：加载 selected DraftSections 的 section_payload（供拼装）。"""
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(DraftSectionModel).where(
                    DraftSectionModel.draft_section_id.in_(draft_section_ids)
                )
            )
            rows = result.scalars().all()
        by_id = {row.draft_section_id: row.section_payload for row in rows}
        missing = [did for did in draft_section_ids if did not in by_id]
        if missing:
            raise ReportAssemblyError(f"{len(missing)} draft section(s) missing from DB")
        return by_id

    def _report_model(
        self,
        verified: VerifiedReportOutline,
        payload: dict,
        fingerprint: str,
    ) -> ReportModel:
        return ReportModel(
            report_id=uuid.uuid4(),
            outline_id=verified.outline_id,
            company_id=verified.company_id,
            research_question_sha256=verified.research_question_sha256,
            analysis_as_of=verified.analysis_as_of,
            report_schema_version=REPORT_SCHEMA_VERSION,
            report_payload=payload,
            report_fingerprint=fingerprint,
        )

    @staticmethod
    def _verify_replay(row: ReportModel, expected: ReportModel) -> None:
        """replay 完整性校验：同 fingerprint 的既有行必须与本次装配完全一致。

        fingerprint 已覆盖 outline / selected draft sections / payload 全部派生
        字段；命中同指纹却内容不同 → 数据被篡改 → `ReportIntegrityError`（不自动
        repair）。
        """
        checks = [
            (row.outline_id, expected.outline_id, "outline_id"),
            (row.company_id, expected.company_id, "company_id"),
            (
                row.research_question_sha256,
                expected.research_question_sha256,
                "research_question_sha256",
            ),
            (row.analysis_as_of, expected.analysis_as_of, "analysis_as_of"),
            (
                row.report_schema_version,
                expected.report_schema_version,
                "report_schema_version",
            ),
            (row.report_payload, expected.report_payload, "report_payload"),
            (row.report_fingerprint, expected.report_fingerprint, "report_fingerprint"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise ReportIntegrityError(f"report {field} mismatch")
