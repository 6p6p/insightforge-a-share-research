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
from app.report.checks import CheckInput, EvidenceCheckData, run_checks
from app.report.contracts import (
    CHECK_STATUS_FAIL,
    CHECK_STATUS_PASS,
    REPORT_CHECK_SCHEMA_VERSION,
    CheckFinding,
    ReportCheckResult,
    compute_check_fingerprint,
)
from app.report.errors import ReportCheckPersistenceFailed
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
            for card_id, card in cards.items():
                evidence[str(card_id)] = EvidenceCheckData(
                    evidence_card_id=card_id,
                    evidence_statement=card.evidence_statement,
                    quote_text=card.quote_text,
                    origin_type=card.origin_type,
                    has_provenance=_has_source_provenance(card),
                    bound_claim_ids=tuple(sorted(bound[card_id], key=str)),
                )

        return CheckInput(
            verified_outline=verified.verified_outline,
            verified_drafts={item.section_id: item for item in verified.verified_drafts},
            report_payload=verified.report_payload,
            claim_statements=claim_statements,
            evidence=evidence,
        )


def _has_source_provenance(card: EvidenceCardModel) -> bool:
    """Evidence → source provenance 是否真实可追溯（只检查 closure，不重新 retrieval）。

    EvidenceCardModel 的 origin_consistency CHECK 保证 document_chunk 必有
    source_id、macro_observation 必有 observation_id；这里按 origin_type 校验
    对应 FK 存在（防 SQL tamper 删掉 FK）。
    """
    if card.origin_type == "document_chunk":
        return card.source_id is not None
    return card.macro_observation_id is not None


def _finding_from_dict(data: dict) -> CheckFinding:
    """持久化 finding（规范化 dict）→ CheckFinding（replay 返回用）。"""
    return CheckFinding(
        code=data["code"],
        section_id=data.get("section_id"),
        paragraph_index=data.get("paragraph_index"),
        related_claim_ids=tuple(data.get("related_claim_ids", [])),
        related_evidence_card_ids=tuple(data.get("related_evidence_card_ids", [])),
    )
