"""Deterministic report check service (stage 5C, spec Q/R/S): 0 LLM.

流程（短 DB session + 纯函数，镜像 ReportService）：
1. `ReportService.verify_report_integrity(report_id)`（read-side 公共 API）→
   `VerifiedReport`（verified Outline + verified DraftSections + rebuilt payload）；
2. **short DB session**：加载 report 全部段落引用的 Claims（statement）与
   Evidence Cards（statement / quote / origin_type / provenance FK）+
   `claim_evidence_links` 的 Evidence–Claim binding → 关闭 session → 构造
   `CheckInput`（**纯函数 checks 的全部输入**）；
3. `run_checks(check_input)`（纯函数，10 个 v1 checks）→ 确定性 findings；
4. status：无 findings → `pass`，有任何 finding → `fail`（**不自动修改 Report**）；
5. `compute_check_fingerprint`（check schema + report_id + report_fingerprint +
   normalized findings，spec S）→ 相同 → replay；Report 改变 → 新指纹 → 新
   CheckResult；
6. **short transaction create_or_get**（ON CONFLICT DO NOTHING，无进程锁）→ 并发
   同输入 → 1 个 CheckResult；SQLAlchemyError → rollback + `ReportCheckPersistenceFailed`。

**公共 read-side**：`verify_check_result_integrity(check_result_id)`——重新 verify
上游 Report + **重跑确定性 checks**（重算 expected status / findings /
check_fingerprint），任一 status / findings / fingerprint / schema / report_id 被
SQL tamper → `ReportCheckIntegrityError`（**不自动 repair**）。status 不在指纹内，
必须重跑 checks 才能发现 pass/fail 篡改；上游 Report 损坏 → verify_report_integrity
先行拒绝。

`citation_provenance_closure` 的 `has_provenance` 按 spec D 走**真实 provenance
闭包**（FK 非空不够）：document_chunk 沿 `source_id → SourceRecord.artifact_id →
RawArtifact`，macro_observation 沿 `observation → snapshot →（series / provider +
artifact links）→ RawArtifact`，任一断裂 → finding（不 repair / 不重新 retrieval）。

**不创建 Audit**；不接 LangGraph；不调用 Retrieval / Chroma / LLM / tools / web
search。check 只检查 closure，不重新 retrieval（spec Q.10）。
"""

import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.report import ReportCheckResultModel
from app.evidence.provenance_service import EvidenceProvenanceService
from app.report.checks import CheckInput, EvidenceCheckData, run_checks
from app.report.contracts import (
    CHECK_STATUS_FAIL,
    CHECK_STATUS_PASS,
    REPORT_CHECK_SCHEMA_VERSION,
    CheckFinding,
    ReportCheckResult,
    VerifiedReportCheckResult,
    compute_check_fingerprint,
)
from app.report.errors import (
    ReportCheckIntegrityError,
    ReportCheckNotFound,
    ReportCheckPersistenceFailed,
)
from app.report.repository import ReportCheckResultRepository
from app.report.service import ReportService


class ReportCheckService:
    """Deterministic Report checks：verified Report → 10 v1 checks → CheckResult（0 LLM）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        report_service: ReportService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._report_service = report_service

    async def run_report_checks(self, report_id: UUID) -> ReportCheckResult:
        """运行全部 v1 checks 并原子持久化 CheckResult；同输入 → replay 同一行。"""
        verified = await self._report_service.verify_report_integrity(report_id)
        check_input = await self._load_check_input(verified)
        findings = run_checks(check_input)

        status = CHECK_STATUS_PASS if not findings else CHECK_STATUS_FAIL
        normalized_findings = [finding.to_dict() for finding in findings]
        fingerprint = compute_check_fingerprint(
            check_schema_version=REPORT_CHECK_SCHEMA_VERSION,
            report_id=verified.report_id,
            report_fingerprint=verified.report_fingerprint,
            findings=normalized_findings,
        )
        expected = ReportCheckResultModel(
            check_result_id=uuid.uuid4(),
            report_id=verified.report_id,
            check_schema_version=REPORT_CHECK_SCHEMA_VERSION,
            status=status,
            findings=normalized_findings,
            check_fingerprint=fingerprint,
        )

        async with self._sessionmaker() as session:
            try:
                row, was_created = await ReportCheckResultRepository(session).create_or_get(
                    expected
                )
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReportCheckPersistenceFailed() from exc

        return ReportCheckResult(
            check_result_id=row.check_result_id,
            report_id=row.report_id,
            check_schema_version=row.check_schema_version,
            status=row.status,
            findings=tuple(_finding_from_dict(item) for item in row.findings),
            check_fingerprint=row.check_fingerprint,
            replayed=not was_created,
        )

    async def verify_check_result_integrity(
        self, check_result_id: UUID
    ) -> VerifiedReportCheckResult:
        """公共 read-only 完整性校验（Stage 5D Gate 0，spec A：缺失的 API，本次新增）。

        流程（短 DB session + 纯函数，**0 LLM / 0 写**）：
        1. 短 session 加载 CheckResult 行；缺失 → `ReportCheckNotFound`；
        2. `ReportService.verify_report_integrity(report_id)`（read-side 公共 API）
           → 上游 Report / Outline / DraftSection 任一损坏 → 对应 IntegrityError
           向上传播（**不 repair**）；
        3. 短 session 加载 Claims / Evidence → `run_checks` **重跑确定性 checks**
           → 重算 expected status（无 findings → pass，有 → fail）与 findings；
        4. 重算 expected check_fingerprint（REPORT_CHECK_SCHEMA_VERSION + verified
           report_fingerprint + normalized findings）；
        5. 与 persisted 逐一对比（report_id / check_schema_version / status /
           findings / check_fingerprint），任一不同 → `ReportCheckIntegrityError`；
        6. 返回 `VerifiedReportCheckResult`（含 verified_report，供 5D Audit 复用）。

        **不 repair / 不 update**。status 不在 check_fingerprint 内 → 必须重跑
        checks 才能发现 pass/fail 篡改；上游 Report 被篡改 → verify_report_integrity
        先行拒绝（不能只重算指纹，否则 tampered report_fingerprint 会自洽）。
        """
        async with self._sessionmaker() as session:
            row = await ReportCheckResultRepository(session).get_by_id(check_result_id)
            if row is None:
                raise ReportCheckNotFound()

        verified = await self._report_service.verify_report_integrity(row.report_id)
        check_input = await self._load_check_input(verified)
        findings = run_checks(check_input)
        status = CHECK_STATUS_PASS if not findings else CHECK_STATUS_FAIL
        normalized_findings = [finding.to_dict() for finding in findings]
        fingerprint = compute_check_fingerprint(
            check_schema_version=REPORT_CHECK_SCHEMA_VERSION,
            report_id=verified.report_id,
            report_fingerprint=verified.report_fingerprint,
            findings=normalized_findings,
        )
        checks = [
            (row.report_id, verified.report_id, "report_id"),
            (row.check_schema_version, REPORT_CHECK_SCHEMA_VERSION, "check_schema_version"),
            (row.status, status, "status"),
            (row.findings, normalized_findings, "findings"),
            (row.check_fingerprint, fingerprint, "check_fingerprint"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise ReportCheckIntegrityError(f"report check {field} mismatch")

        return VerifiedReportCheckResult(
            check_result_id=row.check_result_id,
            report_id=row.report_id,
            check_schema_version=row.check_schema_version,
            status=row.status,
            findings=tuple(_finding_from_dict(item) for item in row.findings),
            check_fingerprint=row.check_fingerprint,
            verified_report=verified,
        )

    # ------------------------------------------------------------------ 内部

    async def _load_check_input(self, verified) -> CheckInput:
        """短 DB session：加载 report 引用 Claims / Evidence 数据 + binding。

        从 verified.report_payload 收集全部段落引用的 claim_id / evidence_card_id
        （字符串 UUID），一次 IN 查询加载 statement / provenance 字段 +
        `claim_evidence_links` 的 binding；session 关闭后纯函数 checks 使用。
        """
        claim_ids: set[UUID] = set()
        evidence_ids: set[UUID] = set()
        for section in verified.report_payload["sections"]:
            for paragraph in section["paragraphs"]:
                for raw in paragraph["claim_ids"]:
                    claim_ids.add(UUID(raw))
                for raw in paragraph["evidence_card_ids"]:
                    evidence_ids.add(UUID(raw))

        claim_statements: dict[str, str] = {}
        if claim_ids:
            async with self._sessionmaker() as session:
                result = await session.execute(
                    select(ClaimModel).where(ClaimModel.claim_id.in_(claim_ids))
                )
                for row in result.scalars().all():
                    claim_statements[str(row.claim_id)] = row.statement

        evidence: dict[str, EvidenceCheckData] = {}
        if evidence_ids:
            async with self._sessionmaker() as session:
                result = await session.execute(
                    select(EvidenceCardModel).where(
                        EvidenceCardModel.evidence_card_id.in_(evidence_ids)
                    )
                )
                cards = {row.evidence_card_id: row for row in result.scalars().all()}
                links = await session.execute(
                    select(
                        ClaimEvidenceLinkModel.claim_id,
                        ClaimEvidenceLinkModel.evidence_card_id,
                    ).where(ClaimEvidenceLinkModel.evidence_card_id.in_(evidence_ids))
                )
                bound: dict[UUID, set[UUID]] = {cid: set() for cid in evidence_ids}
                for claim_id, card_id in links.all():
                    bound[card_id].add(claim_id)
                # spec D：真实 provenance 闭包（FK 非空不够），同一短 session 批量加载。
                provenance = await self._load_provenance_closure(session, cards)
            for card_id, card in cards.items():
                evidence[str(card_id)] = EvidenceCheckData(
                    evidence_card_id=card_id,
                    evidence_statement=card.evidence_statement,
                    quote_text=card.quote_text,
                    origin_type=card.origin_type,
                    has_provenance=provenance.get(card_id, False),
                    bound_claim_ids=tuple(sorted(bound[card_id], key=str)),
                )

        return CheckInput(
            verified_outline=verified.verified_outline,
            verified_drafts={item.section_id: item for item in verified.verified_drafts},
            report_payload=verified.report_payload,
            claim_statements=claim_statements,
            evidence=evidence,
        )

    async def _load_provenance_closure(
        self, session, cards: dict[UUID, EvidenceCardModel]
    ) -> dict[UUID, bool]:
        """按 origin_type 批量验证 Evidence → source → RawArtifact 真实可追溯。

        委托 `EvidenceProvenanceService.load_closure`（spec I：Document 与
        Macro 共用这一条 verified provenance path，此处不维护私有第三套逻辑）。
        """
        return await EvidenceProvenanceService.load_closure(session, cards)


def _finding_from_dict(data: dict) -> CheckFinding:
    """持久化 finding（规范化 dict）→ CheckFinding（replay 返回用）。"""
    return CheckFinding(
        code=data["code"],
        section_id=data.get("section_id"),
        paragraph_index=data.get("paragraph_index"),
        related_claim_ids=tuple(data.get("related_claim_ids", [])),
        related_evidence_card_ids=tuple(data.get("related_evidence_card_ids", [])),
    )
