"""Evidence-bound report audit service (stage 5D, spec Q/R/S): Agent 审计 + 完整性校验。

流程（短 DB session + 纯函数，镜像 DraftSectionService）：
1. `_check_request`（防御性 request 校验）；
2. `ReportCheckService.verify_check_result_integrity(check_result_id)`（read-side
   公共 API）→ `VerifiedReportCheckResult`（含 verified Report）；check 与
   request.report_id 不匹配 → `ReportAuditCheckMismatch`；
3. **短 DB session**：加载 report 全部段落引用的 Claims（含 fingerprint）+
   paragraph referenced Claims 当前绑定的**全部** Evidence（含 fingerprint +
   per-Claim relation，spec J）→ 关闭 session → 构造 deterministic Audit Pack
   （S/P/C/E/X/G alias，**LLM 永不看 UUID / fingerprint / provenance id**）；
4. `compute_audit_input_fingerprint`（audit schema + report / check 指纹 +
   auditor 身份 + normalized pack 身份，spec Q）；
5. **replay check**（短 session，0 LLM）：已存在同指纹行 → 完整 replay 校验
   （身份 / input 指纹 / issues scope / status / route / audit_fingerprint）→
   0 次模型调用直接返回；损坏 → `ReportAuditIntegrityError`（**不自动 repair**）；
6. 关闭 session → 调 Auditor 模型（structured output）→ `AuditDecision`；
7. hard validation（`validate_decision`：coverage / known / scope / enum，spec
   M/N）→ 解析为真实 ID 的 `ResolvedAuditIssue` → 按 spec R 排序确定 ordinal；
8. `derive_route`（spec O，程序确定性，模型不决定 routing）；
9. `compute_audit_fingerprint`（input 指纹 + normalized resolved issues + status +
   route，NOT UNIQUE）；
10. 短 DB transaction：create_or_get（ON CONFLICT(audit_input_fingerprint)
    DO NOTHING，无进程锁）→ 新建时原子写入 ReviewIssues（同事务，任一失败 →
    整条 rollback）；命中时完整 replay 校验；SQLAlchemyError → rollback +
    `ReportAuditPersistenceFailed`；
11. 返回 `ReportAuditResult`（不含 issues 明细 / prompt / raw response）。

**公共 read-side**：`verify_audit_integrity(audit_id)`（spec S）——重新 verify
Report / CheckResult / rebuild Audit Pack / recompute audit_input_fingerprint /
load ReviewIssues / 验证 refs / issue enums / scope / 重派生 status / route /
recompute audit_fingerprint；任一损坏 → `ReportAuditIntegrityError`（**不自动
repair**）。

**不创建 Report / CheckResult 之外的行**；不接 LangGraph；不调用 Retrieval /
Chroma / tools / web search；Auditor 不重写正文 / 不生成新事实 / 不重算数字 /
不检索（spec P）。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.audit.contracts import (
    AUDIT_ISSUE_TYPES,
    AUDIT_SEVERITIES,
    AUDITOR_NAME,
    AUDITOR_VERSION,
    REPORT_AUDIT_SCHEMA_VERSION,
    AuditDecision,
    ReportAuditRequest,
    ReportAuditResult,
    ResolvedAuditIssue,
    ReviewIssue,
    VerifiedReportAudit,
    compute_audit_fingerprint,
    compute_audit_input_fingerprint,
)
from app.audit.errors import (
    ReportAuditCheckMismatch,
    ReportAuditError,
    ReportAuditInputError,
    ReportAuditIntegrityError,
    ReportAuditMalformedOutput,
    ReportAuditModelUnavailable,
    ReportAuditNotFound,
    ReportAuditPersistenceFailed,
)
from app.audit.model import AuditModel
from app.audit.packs import (
    AuditPack,
    LoadedAuditClaim,
    LoadedAuditEvidence,
    ResolvedAuditConflict,
    ResolvedAuditGap,
    audit_pack_identity,
    build_audit_pack,
)
from app.audit.repository import ReportAuditRepository
from app.audit.route import derive_route
from app.audit.validate import validate_decision
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.report_audit import ReportAuditModel, ReviewIssueModel
from app.report.check_service import ReportCheckService
from app.report.contracts import VerifiedReport, VerifiedReportCheckResult
from app.report.errors import ReportCheckNotFound


@dataclass(frozen=True)
class _LoadedAuditInput:
    """短 DB session 的加载产物（session 关闭后不再持有连接）。"""

    claims: list[LoadedAuditClaim]
    evidence: list[LoadedAuditEvidence]
    conflicts: list[ResolvedAuditConflict]
    gaps: list[ResolvedAuditGap]


class ReportAuditService:
    """Evidence-bound Report Audit：verified Report + verified CheckResult → Agent 审计。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        model: AuditModel,
        check_service: ReportCheckService,
    ) -> None:
        """model 必须提供（replay 也用它取 auditor_model_id 重算输入指纹）。

        check_service 显式注入（上游 Report / Check 装配链由调用方组合，镜像
        ReportService → ReportCheckService 的依赖注入模式）。构造不触发模型
        调用；只有 `create_or_get_audit` 真正缺少既有行时才调 `model.audit()`。
        """
        self._sessionmaker = sessionmaker
        self._model = model
        self._check_service = check_service

    async def create_or_get_audit(self, request: ReportAuditRequest) -> ReportAuditResult:
        """对 verified Report + verified CheckResult 执行 Evidence-bound 审计；
        同 input → replay 同一行。"""
        self._check_request(request)

        # 1-2. verify check result（read-side 公共 API）→ verified Report 复用。
        try:
            verified_check = await self._check_service.verify_check_result_integrity(
                request.check_result_id
            )
        except ReportCheckNotFound:
            raise ReportAuditNotFound() from None
        if verified_check.report_id != request.report_id:
            raise ReportAuditCheckMismatch()
        verified = verified_check.verified_report

        # 3. 短 DB session 加载 audit 输入 → 纯函数构造 deterministic Audit Pack。
        loaded = await self._load_audit_input(verified)
        pack = build_audit_pack(
            verified_report=verified,
            verified_check=verified_check,
            claims=loaded.claims,
            evidence=loaded.evidence,
            conflicts=loaded.conflicts,
            gaps=loaded.gaps,
        )

        # 4. LLM 输入边界的确定性指纹（spec Q）。
        audit_input_fingerprint = self._compute_input_fingerprint(verified, verified_check, pack)

        # 5. replay check（0 LLM）：同输入既有行 → 完整校验后直接返回。
        existing = await self._find_existing(audit_input_fingerprint)
        if existing is not None:
            async with self._sessionmaker() as session:
                issue_rows = await ReportAuditRepository(session).list_issues(existing.audit_id)
            self._verify_replay(
                verified=verified,
                verified_check=verified_check,
                pack=pack,
                audit_input_fingerprint=audit_input_fingerprint,
                row=existing,
                issue_rows=issue_rows,
            )
            return self._result(existing, replayed=True)

        # 6. 关闭 session → 调模型（structured output）。
        decision = await self._call_model(pack)

        # 7. hard validation → 解析为真实 ID → 按 spec R 排序确定 ordinal。
        resolved = validate_decision(pack, decision)
        ordinal_issues = self._sort_for_ordinal(resolved, pack)

        # 8. deterministic status / route（spec O，模型不决定 routing）。
        status, route = derive_route(ordinal_issues)

        # 9. 审计不可变指纹（input 指纹 + normalized issues + status + route）。
        audit_fingerprint = compute_audit_fingerprint(
            audit_input_fingerprint=audit_input_fingerprint,
            issues=[issue.to_fingerprint_dict() for issue in ordinal_issues],
            status=status,
            route=route,
        )
        expected = self._audit_model(
            verified_check=verified_check,
            audit_input_fingerprint=audit_input_fingerprint,
            issues=ordinal_issues,
            status=status,
            route=route,
            audit_fingerprint=audit_fingerprint,
        )

        # 10. 短 DB transaction：create_or_get（原子）+ issues 同事务写入。
        async with self._sessionmaker() as session:
            try:
                row, was_created = await ReportAuditRepository(session).create_or_get(expected)
                if not was_created:
                    # 并发输家 / 已存在结果：完整 replay 校验（不写任何行）。
                    issue_rows = await ReportAuditRepository(session).list_issues(row.audit_id)
                    self._verify_replay(
                        verified=verified,
                        verified_check=verified_check,
                        pack=pack,
                        audit_input_fingerprint=audit_input_fingerprint,
                        row=row,
                        issue_rows=issue_rows,
                    )
                else:
                    session.add_all(self._issue_models(row.audit_id, ordinal_issues))
                await session.commit()
            except ReportAuditError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ReportAuditPersistenceFailed() from exc

        # 11. 结果摘要（不含 issues 明细 / prompt / raw response）。
        return self._result(row, replayed=not was_created)

    async def verify_audit_integrity(self, audit_id: UUID) -> VerifiedReportAudit:
        """public read-only 校验（spec S）：完整重建输入并重放验证。

        步骤：加载 audit 行 + 其 ReviewIssues（同短 session）→ verify
        Report / CheckResult（read-side 公共 API）→ 重建 Audit Pack → 重算
        audit_input_fingerprint → 身份字段逐一对比 → load ReviewIssues → 验证
        refs / issue enums / scope → 重派生 status / route → 重算
        audit_fingerprint。任一损坏 → `ReportAuditIntegrityError`（**不自动
        repair**）。
        """
        async with self._sessionmaker() as session:
            row = await ReportAuditRepository(session).get_by_id(audit_id)
            issue_rows = (
                await ReportAuditRepository(session).list_issues(audit_id)
                if row is not None
                else []
            )
        if row is None:
            raise ReportAuditNotFound()

        # verify Report / CheckResult（上游损坏 → 对应 IntegrityError 向上传播）。
        try:
            verified_check = await self._check_service.verify_check_result_integrity(
                row.check_result_id
            )
        except ReportCheckNotFound:
            raise ReportAuditIntegrityError("report audit check result missing") from None
        verified = verified_check.verified_report
        if verified_check.report_id != row.report_id:
            raise ReportAuditIntegrityError("report audit report_id mismatch")

        # rebuild Audit Pack → recompute input fingerprint。
        loaded = await self._load_audit_input(verified)
        pack = build_audit_pack(
            verified_report=verified,
            verified_check=verified_check,
            claims=loaded.claims,
            evidence=loaded.evidence,
            conflicts=loaded.conflicts,
            gaps=loaded.gaps,
        )
        recomputed_input = self._compute_input_fingerprint(verified, verified_check, pack)

        # 身份字段逐一对比。
        identity_checks = [
            (row.audit_schema_version, REPORT_AUDIT_SCHEMA_VERSION, "audit_schema_version"),
            (row.auditor_name, AUDITOR_NAME, "auditor_name"),
            (row.auditor_version, AUDITOR_VERSION, "auditor_version"),
            (row.auditor_model_id, self._model.model_id, "auditor_model_id"),
            (row.audit_input_fingerprint, recomputed_input, "audit_input_fingerprint"),
        ]
        for actual, want, field in identity_checks:
            if actual != want:
                raise ReportAuditIntegrityError(f"report audit {field} mismatch")

        # load ReviewIssues → 验证 refs / enums / scope → 重派生 status / route。
        issues = [self._to_resolved(issue_row) for issue_row in issue_rows]
        self._verify_resolved_issues(pack, issues)
        status, route = derive_route(issues)
        if row.audit_status != status or row.recommended_route != route:
            raise ReportAuditIntegrityError("report audit status/route mismatch")
        if row.issue_count != len(issues):
            raise ReportAuditIntegrityError("report audit issue_count mismatch")

        # 重算 audit_fingerprint（issues 按 persisted ordinal 顺序 = spec R 顺序）。
        recomputed_audit = compute_audit_fingerprint(
            audit_input_fingerprint=recomputed_input,
            issues=[issue.to_fingerprint_dict() for issue in issues],
            status=status,
            route=route,
        )
        if recomputed_audit != row.audit_fingerprint:
            raise ReportAuditIntegrityError("report audit audit_fingerprint mismatch")

        return VerifiedReportAudit(
            audit_id=row.audit_id,
            report_id=row.report_id,
            check_result_id=row.check_result_id,
            audit_schema_version=row.audit_schema_version,
            auditor_name=row.auditor_name,
            auditor_version=row.auditor_version,
            auditor_model_id=row.auditor_model_id,
            audit_input_fingerprint=row.audit_input_fingerprint,
            audit_status=row.audit_status,
            recommended_route=row.recommended_route,
            issue_count=row.issue_count,
            audit_fingerprint=row.audit_fingerprint,
            issues=tuple(self._to_review_issue(issue_row) for issue_row in issue_rows),
            verified_report=verified,
            verified_check=verified_check,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: ReportAuditRequest) -> None:
        # 构造时已校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if isinstance(request.report_id, bool) or not isinstance(request.report_id, UUID):
            raise ReportAuditInputError("report_id 必须是 UUID")
        if isinstance(request.check_result_id, bool) or not isinstance(
            request.check_result_id, UUID
        ):
            raise ReportAuditInputError("check_result_id 必须是 UUID")

    def _compute_input_fingerprint(
        self,
        verified: VerifiedReport,
        verified_check: VerifiedReportCheckResult,
        pack: AuditPack,
    ) -> str:
        """audit_input_fingerprint（spec Q）：audit schema + report / check 指纹 +
        auditor 身份 + normalized pack 身份。"""
        return compute_audit_input_fingerprint(
            audit_schema_version=REPORT_AUDIT_SCHEMA_VERSION,
            report_id=verified.report_id,
            report_fingerprint=verified.report_fingerprint,
            check_result_id=verified_check.check_result_id,
            check_fingerprint=verified_check.check_fingerprint,
            auditor_name=AUDITOR_NAME,
            auditor_version=AUDITOR_VERSION,
            auditor_model_id=self._model.model_id,
            pack_identity=audit_pack_identity(pack),
        )

    async def _load_audit_input(self, verified: VerifiedReport) -> _LoadedAuditInput:
        """短 DB session：加载 report 引用的 Claims + 绑定的全部 Evidence（0 LLM）。

        Evidence 不只是 paragraph 已引用的：对 paragraph referenced Claims 加载
        这些 Claim 当前绑定的**全部** ClaimEvidenceLinks（supports / contradicts /
        context，spec J）——让 Auditor 能看到"作者只引用了 supports E1，但 Claim
        其实还有 contradicts E2"。claim_relations 只保留属于本 pack Claims 的
        绑定（pack 外 Claim 与本报告无关且不泄漏其 UUID）；段落引用的 Evidence
        必须全部可绑定（任一缺失 → 数据损坏）。
        """
        claim_ids, referenced_evidence_ids = self._referenced_ids(verified)
        conflicts, gaps = self._resolve_conflicts_and_gaps(verified)
        async with self._sessionmaker() as session:
            claims = await self._load_claims(session, claim_ids)
            evidence = await self._load_evidence(session, claim_ids)
        loaded_evidence_ids = {item.evidence_card_id for item in evidence}
        missing_evidence = referenced_evidence_ids - loaded_evidence_ids
        if missing_evidence:
            raise ReportAuditIntegrityError(
                f"{len(missing_evidence)} referenced evidence card(s) missing claim binding"
            )
        return _LoadedAuditInput(
            claims=claims,
            evidence=evidence,
            conflicts=conflicts,
            gaps=gaps,
        )

    @staticmethod
    def _referenced_ids(verified: VerifiedReport) -> tuple[set[UUID], set[UUID]]:
        """report payload 全部段落引用的 claim_id / evidence_card_id 集合。"""
        claim_ids: set[UUID] = set()
        evidence_ids: set[UUID] = set()
        for section in verified.report_payload["sections"]:
            for paragraph in section["paragraphs"]:
                for raw in paragraph["claim_ids"]:
                    claim_ids.add(UUID(raw))
                for raw in paragraph["evidence_card_ids"]:
                    evidence_ids.add(UUID(raw))
        return claim_ids, evidence_ids

    async def _load_claims(self, session, claim_ids: set[UUID]) -> list[LoadedAuditClaim]:
        """加载 report 引用的 Claims（含 fingerprint，供输入指纹用）。"""
        if not claim_ids:
            raise ReportAuditIntegrityError("report has no referenced claims")
        result = await session.execute(select(ClaimModel).where(ClaimModel.claim_id.in_(claim_ids)))
        rows = result.scalars().all()
        by_id = {row.claim_id: row for row in rows}
        missing = [cid for cid in claim_ids if cid not in by_id]
        if missing:
            raise ReportAuditIntegrityError(f"{len(missing)} referenced claim(s) missing from DB")
        return [
            LoadedAuditClaim(
                claim_id=row.claim_id,
                claim_fingerprint=row.claim_fingerprint,
                statement=row.statement,
                analysis_domain=row.analysis_domain,
                claim_kind=row.claim_kind,
                confidence=row.confidence,
                importance=row.importance,
            )
            for row in rows
        ]

    async def _load_evidence(self, session, claim_ids: set[UUID]) -> list[LoadedAuditEvidence]:
        """加载真实绑定于 report 引用 Claims 的 Evidence（只含真实绑定，spec J）。

        按 evidence_card_id 聚合：claim_relations = 每张 Evidence 与其绑定
        pack Claims 的 `(claim_id, relation)` 对（canonical 排序，supports /
        contradicts / context 全部保留，不折叠）。**不重新 Retrieval**。
        """
        if not claim_ids:
            return []
        result = await session.execute(
            select(
                EvidenceCardModel,
                ClaimEvidenceLinkModel.claim_id,
                ClaimEvidenceLinkModel.relation,
            )
            .join(
                ClaimEvidenceLinkModel,
                ClaimEvidenceLinkModel.evidence_card_id == EvidenceCardModel.evidence_card_id,
            )
            .where(ClaimEvidenceLinkModel.claim_id.in_(claim_ids))
        )
        grouped: dict[UUID, dict] = {}
        for card, claim_id, relation in result.all():
            entry = grouped.setdefault(card.evidence_card_id, {"card": card, "relations": set()})
            entry["relations"].add((claim_id, relation))
        items: list[LoadedAuditEvidence] = []
        for card_id in sorted(grouped, key=str):
            entry = grouped[card_id]
            card = entry["card"]
            bound = tuple(
                sorted(
                    ((cid, rel) for cid, rel in entry["relations"] if cid in claim_ids),
                    key=lambda pair: str(pair[0]),
                )
            )
            if not bound:
                # 查询按 pack claims 过滤，理论不可达；防御性跳过未绑定卡。
                continue
            items.append(
                LoadedAuditEvidence(
                    evidence_card_id=card.evidence_card_id,
                    evidence_fingerprint=card.evidence_fingerprint,
                    evidence_statement=card.evidence_statement,
                    evidence_type=card.evidence_type,
                    quote_text=card.quote_text,
                    provider_key=card.provider_key,
                    authority_tier=card.authority_tier_snapshot,
                    critical_eligible=card.critical_claim_eligible_snapshot,
                    source_published_at=card.source_published_at,
                    reporting_period_end=card.reporting_period_end,
                    origin_type=card.origin_type,
                    claim_relations=bound,
                )
            )
        return items

    @staticmethod
    def _resolve_conflicts_and_gaps(
        verified: VerifiedReport,
    ) -> tuple[list[ResolvedAuditConflict], list[ResolvedAuditGap]]:
        """synthesis 冲突 / 缺口（spec K）：按 Outline sections 的 indexes 并集。

        加载"与 section 相关"的 conflicts / evidence gaps：收集全部 outline
        section 的 conflict_indexes / evidence_gap_indexes（保持 synthesis
        output 顺序、去重），经 `alias_map` 解析回真实 claim_id。越界 index →
        完整性错误（Outline 已校验，正常不可达）。
        """
        outline = verified.verified_outline
        synthesis = outline.verified_synthesis_result
        conflict_indexes: set[int] = set()
        gap_indexes: set[int] = set()
        for section in outline.sections:
            conflict_indexes.update(section.conflict_indexes)
            gap_indexes.update(section.evidence_gap_indexes)
        alias_map = synthesis.alias_map

        conflicts_raw = synthesis.output.conflicts
        conflicts: list[ResolvedAuditConflict] = []
        for index in sorted(conflict_indexes):
            if not 0 <= index < len(conflicts_raw):
                raise ReportAuditIntegrityError("synthesis conflict index out of range")
            conflict = conflicts_raw[index]
            conflicts.append(
                ResolvedAuditConflict(
                    claim_ids=tuple(alias_map[ref] for ref in conflict.claim_refs),
                    description=conflict.description,
                    severity=conflict.severity.value,
                    resolution_direction=conflict.resolution_direction,
                )
            )

        gaps_raw = synthesis.output.evidence_gaps
        gaps: list[ResolvedAuditGap] = []
        for index in sorted(gap_indexes):
            if not 0 <= index < len(gaps_raw):
                raise ReportAuditIntegrityError("synthesis evidence gap index out of range")
            gap = gaps_raw[index]
            gaps.append(
                ResolvedAuditGap(
                    claim_ids=tuple(alias_map[ref] for ref in gap.claim_refs),
                    description=gap.description,
                    suggested_evidence=gap.suggested_evidence,
                    priority=gap.priority.value,
                )
            )
        return conflicts, gaps

    async def _call_model(self, pack: AuditPack) -> AuditDecision:
        """调用模型并归一到 AuditDecision（防御性 double-check）。"""
        if self._model is None:
            raise ReportAuditModelUnavailable()
        raw = await self._model.audit(pack)
        if isinstance(raw, AuditDecision):
            return raw
        try:
            return AuditDecision.model_validate(raw)
        except ValidationError as exc:
            raise ReportAuditMalformedOutput() from exc

    async def _find_existing(self, audit_input_fingerprint: str) -> ReportAuditModel | None:
        async with self._sessionmaker() as session:
            return await ReportAuditRepository(session).get_by_audit_input_fingerprint(
                audit_input_fingerprint
            )

    @staticmethod
    def _sort_for_ordinal(
        issues: list[ResolvedAuditIssue], pack: AuditPack
    ) -> list[ResolvedAuditIssue]:
        """spec R：ordinal 按 deterministic `section_order paragraph_index issue_type
        claim IDs evidence IDs message` 排序。

        `paragraph_index` 只在同 section 内比较（先按 section_order 分组）；
        section-level issue（paragraph_index=None）用 -1 排在该 section 段落级
        之前。spec 未列 severity 作为排序字段——同 key 全同（除 severity）的
        极端情况由 Python stable sort 保留 validate_decision 的相对顺序
        （fingerprint 仍含 severity，篡改会被发现）。
        """
        section_order = {section.section_id: index for index, section in enumerate(pack.sections)}
        return sorted(
            issues,
            key=lambda issue: (
                section_order[issue.section_id],
                -1 if issue.paragraph_index is None else issue.paragraph_index,
                issue.issue_type,
                tuple(issue.related_claim_ids),
                tuple(issue.related_evidence_card_ids),
                issue.message,
            ),
        )

    @staticmethod
    def _verify_resolved_issues(pack: AuditPack, issues: list[ResolvedAuditIssue]) -> None:
        """验证 persisted ReviewIssues 的 refs / enums / scope（spec S）。

        镜像 validate.py 的 scope 规则但用真实 ID：section 必须存在；section-level
        issue 必须空 claim/evidence；段落级 issue 的 claim 必须属于该 paragraph
        引用 Claims、evidence 必须绑定到该 issue 的 claims。任一破坏 →
        `ReportAuditIntegrityError`（**不自动 repair**）。
        """
        for issue in issues:
            if issue.issue_type not in AUDIT_ISSUE_TYPES:
                raise ReportAuditIntegrityError("review issue issue_type invalid")
            if issue.severity not in AUDIT_SEVERITIES:
                raise ReportAuditIntegrityError("review issue severity invalid")
            try:
                pack.section_by_id(issue.section_id)
            except StopIteration:
                raise ReportAuditIntegrityError("review issue section_id not in pack") from None
            if issue.paragraph_index is None:
                if issue.related_claim_ids or issue.related_evidence_card_ids:
                    raise ReportAuditIntegrityError(
                        "section-level issue must have empty claim/evidence refs"
                    )
                continue
            paragraph = next(
                (
                    item
                    for item in pack.paragraphs
                    if item.section_id == issue.section_id
                    and item.paragraph_index == issue.paragraph_index
                ),
                None,
            )
            if paragraph is None:
                raise ReportAuditIntegrityError("review issue paragraph not in pack")
            allowed_claims = {str(cid) for cid in paragraph.claim_ids}
            for claim_id in issue.related_claim_ids:
                if claim_id not in allowed_claims:
                    raise ReportAuditIntegrityError(
                        "review issue claim not referenced by paragraph"
                    )
            claim_set = set(issue.related_claim_ids)
            for evidence_id in issue.related_evidence_card_ids:
                try:
                    item = pack.evidence_by_id(UUID(evidence_id))
                except StopIteration:
                    raise ReportAuditIntegrityError("review issue evidence not in pack") from None
                bound_claim_ids = {str(claim_id) for claim_id, _ in item.claim_relations}
                if not (bound_claim_ids & claim_set):
                    raise ReportAuditIntegrityError(
                        "review issue evidence not bound to issue claims"
                    )

    def _verify_replay(
        self,
        *,
        verified: VerifiedReport,
        verified_check: VerifiedReportCheckResult,
        pack: AuditPack,
        audit_input_fingerprint: str,
        row: ReportAuditModel,
        issue_rows: list[ReviewIssueModel],
    ) -> None:
        """replay 完整性校验（spec R/S）：既有行与本次派生完全一致。

        - 身份 / auditor / 输入指纹字段逐一对比；
        - load ReviewIssues → `_verify_resolved_issues`（refs / enums / scope）；
        - `derive_route` 重派生 status / route 对比；
        - 重算 audit_fingerprint（input 指纹 + issues + status + route）与
          persisted 对比。任一损坏 → `ReportAuditIntegrityError`（**不自动
          repair**）。
        """
        checks = [
            (row.report_id, verified.report_id, "report_id"),
            (row.check_result_id, verified_check.check_result_id, "check_result_id"),
            (row.audit_schema_version, REPORT_AUDIT_SCHEMA_VERSION, "audit_schema_version"),
            (row.auditor_name, AUDITOR_NAME, "auditor_name"),
            (row.auditor_version, AUDITOR_VERSION, "auditor_version"),
            (row.auditor_model_id, self._model.model_id, "auditor_model_id"),
            (row.audit_input_fingerprint, audit_input_fingerprint, "audit_input_fingerprint"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise ReportAuditIntegrityError(f"report audit {field} mismatch")

        issues = [self._to_resolved(issue_row) for issue_row in issue_rows]
        self._verify_resolved_issues(pack, issues)
        status, route = derive_route(issues)
        if row.audit_status != status or row.recommended_route != route:
            raise ReportAuditIntegrityError("report audit status/route mismatch")
        if row.issue_count != len(issues):
            raise ReportAuditIntegrityError("report audit issue_count mismatch")
        recomputed = compute_audit_fingerprint(
            audit_input_fingerprint=audit_input_fingerprint,
            issues=[issue.to_fingerprint_dict() for issue in issues],
            status=status,
            route=route,
        )
        if recomputed != row.audit_fingerprint:
            raise ReportAuditIntegrityError("report audit audit_fingerprint mismatch")

    def _audit_model(
        self,
        *,
        verified_check: VerifiedReportCheckResult,
        audit_input_fingerprint: str,
        issues: list[ResolvedAuditIssue],
        status: str,
        route: str,
        audit_fingerprint: str,
    ) -> ReportAuditModel:
        """把验证过的输出构造成不可变 audit 行（issues 只存真实 ID）。"""
        return ReportAuditModel(
            audit_id=uuid.uuid4(),
            report_id=verified_check.report_id,
            check_result_id=verified_check.check_result_id,
            audit_schema_version=REPORT_AUDIT_SCHEMA_VERSION,
            auditor_name=AUDITOR_NAME,
            auditor_version=AUDITOR_VERSION,
            auditor_model_id=self._model.model_id,
            audit_input_fingerprint=audit_input_fingerprint,
            audit_status=status,
            recommended_route=route,
            issue_count=len(issues),
            audit_fingerprint=audit_fingerprint,
        )

    @staticmethod
    def _issue_models(audit_id: UUID, issues: list[ResolvedAuditIssue]) -> list[ReviewIssueModel]:
        """ordinal 1..N = spec R 排序后的 deterministic 序号。"""
        return [
            ReviewIssueModel(
                review_issue_id=uuid.uuid4(),
                audit_id=audit_id,
                ordinal=ordinal,
                issue_type=issue.issue_type,
                severity=issue.severity,
                section_id=issue.section_id,
                paragraph_index=issue.paragraph_index,
                message=issue.message,
                related_claim_ids=list(issue.related_claim_ids),
                related_evidence_card_ids=list(issue.related_evidence_card_ids),
            )
            for ordinal, issue in enumerate(issues, start=1)
        ]

    @staticmethod
    def _to_resolved(row: ReviewIssueModel) -> ResolvedAuditIssue:
        return ResolvedAuditIssue(
            issue_type=row.issue_type,
            severity=row.severity,
            section_id=row.section_id,
            paragraph_index=row.paragraph_index,
            message=row.message,
            related_claim_ids=tuple(row.related_claim_ids),
            related_evidence_card_ids=tuple(row.related_evidence_card_ids),
        )

    @staticmethod
    def _to_review_issue(row: ReviewIssueModel) -> ReviewIssue:
        return ReviewIssue(
            review_issue_id=row.review_issue_id,
            audit_id=row.audit_id,
            ordinal=row.ordinal,
            issue_type=row.issue_type,
            severity=row.severity,
            section_id=row.section_id,
            paragraph_index=row.paragraph_index,
            message=row.message,
            related_claim_ids=tuple(row.related_claim_ids),
            related_evidence_card_ids=tuple(row.related_evidence_card_ids),
        )

    def _result(self, row: ReportAuditModel, *, replayed: bool) -> ReportAuditResult:
        return ReportAuditResult(
            audit_id=row.audit_id,
            report_id=row.report_id,
            check_result_id=row.check_result_id,
            audit_schema_version=row.audit_schema_version,
            audit_status=row.audit_status,
            recommended_route=row.recommended_route,
            issue_count=row.issue_count,
            audit_fingerprint=row.audit_fingerprint,
            replayed=replayed,
        )
