"""Report outline service (stage 5A): create deterministic outline from a synthesis result.

流程（镜像 claim synthesis result 的 short-transaction 模式，**0 LLM**）：
1. **short PG verify**：`SynthesisAnalysisService.verify_result_integrity`——
   短 DB session 加载 result 行 + verify_synthesis_integrity（read-side 公共
   API）→ 关闭 session → 纯函数校验（schema / analyst / payload / claim
   coverage / fingerprint）。损坏 → `SynthesisResultIntegrityError`；result
   缺失 → `SynthesisAnalysisResultNotFound`；
2. **纯 derive**：`derive_outline_payload`（theme → theme section +
   risks_and_gaps section + coverage 硬边界）；**不调用 LLM**；
3. `compute_outline_fingerprint`（含 schema / result fingerprint / payload；
   **不含** outline_id / created_at）；
4. **short transaction create_or_get**（ON CONFLICT(fingerprint)，无进程锁）→
   并发同输入 → 1 个 Outline；命中时 replay 校验（同指纹行 payload 与本次
   派生不一致 → `ReportOutlineIntegrityError`）；SQLAlchemyError → rollback +
   `ReportOutlinePersistenceFailed`。

**不创建 Report / DraftSection / Audit**；不接 LangGraph；不调用 Retrieval /
Chroma / RawArtifact / tools / web search。caller 只提供 synthesis_result_id，
其余全部派生。
"""

import uuid
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.db.models.report_outline import ReportOutlineModel
from app.report_outline.contracts import (
    REPORT_OUTLINE_SCHEMA_VERSION,
    ReportOutlineResult,
    VerifiedReportOutline,
    compute_outline_fingerprint,
    parse_outline_sections,
)
from app.report_outline.derive import derive_outline_payload
from app.report_outline.errors import (
    ReportOutlineIntegrityError,
    ReportOutlineNotFound,
    ReportOutlinePersistenceFailed,
)
from app.report_outline.repository import ReportOutlineRepository


class ReportOutlineService:
    """Deterministic ReportOutline：verified SynthesisResult → 提纲（0 LLM）。"""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker
        # 只消费 read-side 校验，不需要 model（0 planner model / 0 analyst version）。
        self._synthesis_analysis = SynthesisAnalysisService(sessionmaker)

    async def create_or_get_outline(self, synthesis_result_id: UUID) -> ReportOutlineResult:
        """从 synthesis_result_id 派生提纲；同输入 → replay 同一行。"""
        verified = await self._synthesis_analysis.verify_result_integrity(synthesis_result_id)

        payload = derive_outline_payload(verified)
        fingerprint = compute_outline_fingerprint(
            outline_schema_version=REPORT_OUTLINE_SCHEMA_VERSION,
            synthesis_result_id=verified.synthesis_result_id,
            synthesis_result_fingerprint=verified.result_fingerprint,
            company_id=verified.company_id,
            research_question_sha256=verified.research_question_sha256,
            analysis_as_of=verified.analysis_as_of,
            outline_payload=payload,
        )
        expected = self._outline_model(verified, payload, fingerprint)

        async with self._sessionmaker() as session:
            try:
                row, was_created = await ReportOutlineRepository(session).create_or_get(expected)
                if not was_created:
                    await self._verify_replay(session, row, expected)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReportOutlinePersistenceFailed() from exc

        return ReportOutlineResult(
            outline_id=row.outline_id,
            synthesis_result_id=row.synthesis_result_id,
            company_id=row.company_id,
            research_question_sha256=row.research_question_sha256,
            analysis_as_of=row.analysis_as_of,
            outline_schema_version=row.outline_schema_version,
            outline_fingerprint=row.outline_fingerprint,
            replayed=not was_created,
            section_count=len(payload["sections"]),
        )

    # ------------------------------------------- public read-side verify (Stage 5B)

    async def verify_outline_integrity(self, outline_id: UUID) -> VerifiedReportOutline:
        """公共 read-only 完整性校验（Stage 5B：Writer 的 verified 输入）。

        流程（短 DB session + 纯函数，**0 LLM / 0 写**）：
        1. 短 session 加载 ReportOutline 行；缺失 → `ReportOutlineNotFound`；
        2. 关闭 session → `SynthesisAnalysisService.verify_result_integrity`
           （read-side 公共 API，**不复制** replay 逻辑）→ 上游 result 损坏 →
           `SynthesisResultIntegrityError`（不自动 repair）；
        3. 纯函数重派生 `derive_outline_payload` + 重算 `compute_outline_fingerprint`；
        4. 与 persisted 7 字段逐一对比（synthesis_result_id / company_id /
           research_question_sha256 / analysis_as_of / outline_schema_version /
           outline_payload / outline_fingerprint），任一不同 →
           `ReportOutlineIntegrityError`；
        5. 从**重派生** payload 解析 `OutlineSection`（等于 persisted 才通过）。

        返回 `VerifiedReportOutline`（含 `verified_synthesis_result`）。**不 repair /
        不 update**——Writer 只消费本投影，不直接相信 `outline_payload`。
        """
        async with self._sessionmaker() as session:
            row = await ReportOutlineRepository(session).get_by_id(outline_id)
            if row is None:
                raise ReportOutlineNotFound()

        verified = await self._synthesis_analysis.verify_result_integrity(row.synthesis_result_id)
        payload = derive_outline_payload(verified)
        fingerprint = compute_outline_fingerprint(
            outline_schema_version=REPORT_OUTLINE_SCHEMA_VERSION,
            synthesis_result_id=verified.synthesis_result_id,
            synthesis_result_fingerprint=verified.result_fingerprint,
            company_id=verified.company_id,
            research_question_sha256=verified.research_question_sha256,
            analysis_as_of=verified.analysis_as_of,
            outline_payload=payload,
        )
        checks = [
            (row.synthesis_result_id, verified.synthesis_result_id, "synthesis_result_id"),
            (row.company_id, verified.company_id, "company_id"),
            (
                row.research_question_sha256,
                verified.research_question_sha256,
                "research_question_sha256",
            ),
            (row.analysis_as_of, verified.analysis_as_of, "analysis_as_of"),
            (
                row.outline_schema_version,
                REPORT_OUTLINE_SCHEMA_VERSION,
                "outline_schema_version",
            ),
            (row.outline_payload, payload, "outline_payload"),
            (row.outline_fingerprint, fingerprint, "outline_fingerprint"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise ReportOutlineIntegrityError(f"report outline {field} mismatch")

        return VerifiedReportOutline(
            outline_id=row.outline_id,
            synthesis_result_id=row.synthesis_result_id,
            company_id=row.company_id,
            research_question_sha256=row.research_question_sha256,
            analysis_as_of=row.analysis_as_of,
            outline_schema_version=row.outline_schema_version,
            outline_fingerprint=row.outline_fingerprint,
            sections=parse_outline_sections(payload),
            verified_synthesis_result=verified,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _outline_model(
        verified: VerifiedSynthesisResult,
        payload: dict,
        fingerprint: str,
    ) -> ReportOutlineModel:
        return ReportOutlineModel(
            outline_id=uuid.uuid4(),
            synthesis_result_id=verified.synthesis_result_id,
            company_id=verified.company_id,
            research_question_sha256=verified.research_question_sha256,
            analysis_as_of=verified.analysis_as_of,
            outline_schema_version=REPORT_OUTLINE_SCHEMA_VERSION,
            outline_payload=payload,
            outline_fingerprint=fingerprint,
        )

    @staticmethod
    async def _verify_replay(
        session,
        row: ReportOutlineModel,
        expected: ReportOutlineModel,
    ) -> None:
        """replay 完整性校验：同 fingerprint 的既有行必须与本次派生完全一致。

        fingerprint 已覆盖 schema / result / payload 全部派生字段；命中同指纹
        却内容不同 → 数据被篡改 → `ReportOutlineIntegrityError`（不自动 repair）。
        """
        checks = [
            (row.synthesis_result_id, expected.synthesis_result_id, "synthesis_result_id"),
            (row.company_id, expected.company_id, "company_id"),
            (
                row.research_question_sha256,
                expected.research_question_sha256,
                "research_question_sha256",
            ),
            (row.analysis_as_of, expected.analysis_as_of, "analysis_as_of"),
            (
                row.outline_schema_version,
                expected.outline_schema_version,
                "outline_schema_version",
            ),
            (row.outline_payload, expected.outline_payload, "outline_payload"),
            (row.outline_fingerprint, expected.outline_fingerprint, "outline_fingerprint"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise ReportOutlineIntegrityError(f"report outline {field} mismatch")
