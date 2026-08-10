"""Evidence-bound section writer service (stage 5B, spec C/I/J/K/L/M/N/O/P).

流程（两步提交，镜像 SynthesisAnalysisService / ReportOutlineService）：
1. 防御性 request 校验（构造已校验，服务层再兜底）；
2. **短 DB session + 纯函数**：`ReportOutlineService.verify_outline_integrity`
   （read-side 公共 API）→ 关闭 session → 定位 section（缺失 →
   `DraftSectionNotFound`）→ 恢复 allowed Claim 集 + conflict/gap；
3. 短 DB session：加载 section 允许的 Claims（含 fingerprint）+ 真实绑定
   Evidence（含 fingerprint）+ company name → 关闭 session；
4. 纯函数构造 deterministic Section Input Pack（C/E/X/G alias，**LLM 永不看
   UUID / fingerprint / provenance id**）；
5. `compute_writer_input_fingerprint`（outline + section 身份 + allowed
   Claim/Evidence fingerprints + conflict/gap 数据 + writer 身份）；
6. **replay check**（短 session，0 LLM）：已存在同指纹行 → 完整 replay 校验
   （verify_resolved_payload + 重算 section_fingerprint + 身份字段）→ 0 次模型
   调用直接返回；损坏 → `DraftSectionIntegrityError`（**不自动 repair**）；
7. 关闭 session → 调 Writer 模型（structured output）→ `WriterDecision`；
8. hard provenance validation（`validate_decision`：known / cross-section /
   unbound / numeric grounding / forbidden language）→ `resolve_decision`
   把 alias / index 解析回真实 ID，产出规范化 persisted payload；
9. `compute_section_fingerprint`（writer_input_fingerprint + payload）；
10. 短 DB transaction：create_or_get（ON CONFLICT(writer_input_fingerprint)，
    无进程锁）→ 命中时完整 replay 校验；SQLAlchemyError → rollback +
    `DraftSectionPersistenceFailed`；
11. 返回 `DraftSectionResult`（不含正文段落 / prompt / raw response）。

**不创建 Report / DraftSection 之外的行 / Audit**；不接 LangGraph；不调用
Retrieval / Chroma / tools / web search；Writer 无法自主扩大 Outline scope
（cross-section ref → 拒绝）；风险段允许模型引用合成输入集内任意 Claim
（spec I：不限制模型选择额外 claims）。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.company import CompanyModel
from app.db.models.draft_section import DraftSectionModel
from app.db.models.evidence_card import EvidenceCardModel
from app.draft_section.contracts import (
    DRAFT_SECTION_SCHEMA_VERSION,
    WRITER_NAME,
    WRITER_VERSION,
    DraftSectionRequest,
    DraftSectionResult,
    WriterDecision,
    compute_section_fingerprint,
    compute_writer_input_fingerprint,
)
from app.draft_section.errors import (
    DraftSectionError,
    DraftSectionInputError,
    DraftSectionIntegrityError,
    DraftSectionMalformedOutput,
    DraftSectionModelUnavailable,
    DraftSectionNotFound,
    DraftSectionPersistenceFailed,
)
from app.draft_section.model import DraftSectionModel as DraftSectionModelProtocol
from app.draft_section.packs import (
    LoadedClaim,
    LoadedEvidence,
    ResolvedConflict,
    ResolvedGap,
    SectionInputPack,
    build_section_input_pack,
)
from app.draft_section.repository import DraftSectionRepository
from app.draft_section.validate import (
    resolve_decision,
    validate_decision,
    verify_resolved_payload,
)
from app.report_outline.contracts import (
    SECTION_TYPE_RISKS_AND_GAPS,
    SECTION_TYPE_THEME,
    OutlineSection,
    VerifiedReportOutline,
)
from app.report_outline.errors import ReportOutlineNotFound
from app.report_outline.service import ReportOutlineService

# Evidence 多 (claim, evidence) 关系时取确定性最强关系（spec H）。
_RELATION_ORDER = {"supports": 0, "contradicts": 1, "context": 2}


@dataclass(frozen=True)
class _LoadedSection:
    """短 DB session 的加载产物（session 关闭后不再持有连接）。"""

    company_name: str
    claims: list[LoadedClaim]
    evidence: list[LoadedEvidence]
    conflicts: list[ResolvedConflict]
    gaps: list[ResolvedGap]


class DraftSectionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        model: DraftSectionModelProtocol,
    ) -> None:
        """model 必须提供（replay 也用它取 writer_model_id 重算输入指纹）。

        构造不触发模型调用；只有 `create_or_get_section` 真正缺少既有行时才调
        `model.write()`。
        """
        self._sessionmaker = sessionmaker
        self._model = model
        self._outline_service = ReportOutlineService(sessionmaker)

    async def create_or_get_section(self, request: DraftSectionRequest) -> DraftSectionResult:
        """起草一个已验证 Outline section；同 writer 输入 → replay 同一行。"""
        self._check_request(request)

        # 1. verify outline（replay 也重新校验，spec P）——read-side 公共 API。
        try:
            verified = await self._outline_service.verify_outline_integrity(request.outline_id)
        except ReportOutlineNotFound:
            raise DraftSectionNotFound() from None
        section = self._find_section(verified, request.section_id)

        # 2-3. 短 DB session 加载 section 输入（claims + evidence + company）。
        loaded = await self._load_section(verified, section)

        # 4. 纯函数构造 deterministic Section Input Pack。
        pack = build_section_input_pack(
            outline=verified,
            section=section,
            company_name=loaded.company_name,
            claims=loaded.claims,
            evidence=loaded.evidence,
            conflicts=loaded.conflicts,
            gaps=loaded.gaps,
        )

        # 5. LLM 输入边界的确定性指纹。
        writer_input_fingerprint = compute_writer_input_fingerprint(
            section_schema_version=DRAFT_SECTION_SCHEMA_VERSION,
            outline_fingerprint=verified.outline_fingerprint,
            section_id=section.section_id,
            section_order=section.section_order,
            section_type=section.section_type,
            title=section.title,
            claim_fingerprints=[claim.claim_fingerprint for claim in loaded.claims],
            evidence_fingerprints=[item.evidence_fingerprint for item in loaded.evidence],
            conflicts=_conflict_fingerprint_data(loaded.conflicts),
            gaps=_gap_fingerprint_data(loaded.gaps),
            writer_name=WRITER_NAME,
            writer_version=WRITER_VERSION,
            writer_model_id=self._model.model_id,
        )

        # 6. replay check（0 LLM）：同输入既有行 → 完整校验后直接返回。
        existing = await self._find_existing(writer_input_fingerprint)
        if existing is not None:
            self._verify_replay(
                verified=verified,
                section=section,
                pack=pack,
                writer_input_fingerprint=writer_input_fingerprint,
                writer_model_id=self._model.model_id,
                row=existing,
            )
            return self._result(existing, replayed=True)

        # 7. 关闭 session → 调模型（structured output）。
        decision = await self._call_model(pack)

        # 8. hard provenance validation → 解析为 persisted payload（真实 ID）。
        validate_decision(
            pack=pack,
            decision=decision,
            total_claim_count=len(verified.verified_synthesis_result.input_claim_ids),
        )
        payload = resolve_decision(pack, decision)

        # 9. 草稿不可变指纹（writer_input_fingerprint + normalized payload）。
        section_fingerprint = compute_section_fingerprint(
            writer_input_fingerprint=writer_input_fingerprint,
            section_payload=payload,
        )
        expected = self._draft_model(
            verified=verified,
            section=section,
            writer_input_fingerprint=writer_input_fingerprint,
            payload=payload,
            section_fingerprint=section_fingerprint,
        )

        # 10. 短 DB transaction：create_or_get（原子）+ replay 校验。
        async with self._sessionmaker() as session:
            try:
                row, was_created = await DraftSectionRepository(session).create_or_get(expected)
                if not was_created:
                    # 并发输家 / 已存在结果：完整 replay 校验（不写任何行）。
                    self._verify_replay(
                        verified=verified,
                        section=section,
                        pack=pack,
                        writer_input_fingerprint=writer_input_fingerprint,
                        writer_model_id=self._model.model_id,
                        row=row,
                    )
                await session.commit()
            except DraftSectionError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise DraftSectionPersistenceFailed() from exc

        # 11. 结果摘要（不含正文段落 / prompt / raw response）。
        return self._result(row, replayed=not was_created)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: DraftSectionRequest) -> None:
        # 构造时已校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if isinstance(request.outline_id, bool) or not isinstance(request.outline_id, UUID):
            raise DraftSectionInputError("outline_id 必须是 UUID")
        if not request.section_id.strip():
            raise DraftSectionInputError("section_id 不能为空（trim 后）")

    @staticmethod
    def _find_section(verified: VerifiedReportOutline, section_id: str) -> OutlineSection:
        for section in verified.sections:
            if section.section_id == section_id:
                return section
        raise DraftSectionNotFound(f"outline has no section {section_id!r}")

    @staticmethod
    def _allowed_claim_ids(
        verified: VerifiedSynthesisResult, section: OutlineSection
    ) -> list[UUID]:
        """section 允许的 Claim 集。

        - theme section：**只**允许 outline 分配给该 theme 的 Claim（spec G）；
        - risks_and_gaps section：不限制模型选择额外 Claims（spec I）→ 允许
          整个合成输入集（X/G 编号给出被标记的冲突 / 缺口）。
        """
        if section.section_type == SECTION_TYPE_THEME:
            return list(section.claim_ids)
        return list(verified.input_claim_ids)

    @staticmethod
    def _resolve_conflicts_and_gaps(
        verified: VerifiedSynthesisResult, section: OutlineSection
    ) -> tuple[list[ResolvedConflict], list[ResolvedGap]]:
        """risks_and_gaps 恢复：按 outline index 从 VerifiedSynthesisResult 取。

        claim_refs（合成 C alias）经 `verified.alias_map` 解析回真实 claim_id；
        theme section 无 conflicts / gaps。越界 index → 完整性错误。
        """
        conflicts: list[ResolvedConflict] = []
        gaps: list[ResolvedGap] = []
        if section.section_type != SECTION_TYPE_RISKS_AND_GAPS:
            return conflicts, gaps
        alias_map = verified.alias_map
        for index in section.conflict_indexes:
            if not 0 <= index < len(verified.output.conflicts):
                raise DraftSectionIntegrityError("conflict index out of range")
            conflict = verified.output.conflicts[index]
            conflicts.append(
                ResolvedConflict(
                    claim_ids=tuple(alias_map[ref] for ref in conflict.claim_refs),
                    description=conflict.description,
                    severity=conflict.severity.value,
                    resolution_direction=conflict.resolution_direction,
                )
            )
        for index in section.evidence_gap_indexes:
            if not 0 <= index < len(verified.output.evidence_gaps):
                raise DraftSectionIntegrityError("evidence gap index out of range")
            gap = verified.output.evidence_gaps[index]
            gaps.append(
                ResolvedGap(
                    claim_ids=tuple(alias_map[ref] for ref in gap.claim_refs),
                    description=gap.description,
                    suggested_evidence=gap.suggested_evidence,
                    priority=gap.priority.value,
                )
            )
        return conflicts, gaps

    async def _load_section(
        self, verified: VerifiedReportOutline, section: OutlineSection
    ) -> _LoadedSection:
        """短 DB session：company + allowed Claims + 真实绑定 Evidence（0 LLM）。"""
        synthesis_result = verified.verified_synthesis_result
        allowed_claim_ids = self._allowed_claim_ids(synthesis_result, section)
        conflicts, gaps = self._resolve_conflicts_and_gaps(synthesis_result, section)
        async with self._sessionmaker() as session:
            company = await session.get(CompanyModel, verified.company_id)
            if company is None:
                raise DraftSectionIntegrityError("outline company missing")
            company_name = company.short_name or company.official_name
            claims = await self._load_claims(session, allowed_claim_ids)
            evidence = await self._load_evidence(session, allowed_claim_ids)
        return _LoadedSection(
            company_name=company_name,
            claims=claims,
            evidence=evidence,
            conflicts=conflicts,
            gaps=gaps,
        )

    async def _load_claims(self, session, allowed_claim_ids: list[UUID]) -> list[LoadedClaim]:
        """加载 section 允许的 Claims（含 fingerprint，供输入指纹用）。"""
        if not allowed_claim_ids:
            raise DraftSectionIntegrityError("section allowed claim set is empty")
        result = await session.execute(
            select(ClaimModel).where(ClaimModel.claim_id.in_(allowed_claim_ids))
        )
        rows = result.scalars().all()
        by_id = {row.claim_id: row for row in rows}
        missing = [cid for cid in allowed_claim_ids if cid not in by_id]
        if missing:
            raise DraftSectionIntegrityError(f"{len(missing)} allowed claim(s) missing from DB")
        return [
            LoadedClaim(
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

    async def _load_evidence(self, session, allowed_claim_ids: list[UUID]) -> list[LoadedEvidence]:
        """加载真实绑定于 allowed Claims 的 Evidence（只含真实绑定 Evidence，spec H）。

        按 evidence_card_id 聚合：claim_ids = 绑定的 allowed Claims（canonical
        排序）；relation 取确定性最强（supports > contradicts > context）。
        """
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
            .where(ClaimEvidenceLinkModel.claim_id.in_(allowed_claim_ids))
        )
        grouped: dict[UUID, dict] = {}
        for card, claim_id, relation in result.all():
            entry = grouped.setdefault(
                card.evidence_card_id,
                {"card": card, "claim_ids": set(), "relations": set()},
            )
            entry["claim_ids"].add(claim_id)
            entry["relations"].add(relation)
        allowed = set(allowed_claim_ids)
        items: list[LoadedEvidence] = []
        for card_id in sorted(grouped, key=str):
            entry = grouped[card_id]
            card = entry["card"]
            bound = tuple(sorted(entry["claim_ids"] & allowed, key=str))
            if not bound:
                # 查询按 allowed claims 过滤，理论不可达；防御性跳过未绑定卡。
                continue
            items.append(
                LoadedEvidence(
                    evidence_card_id=card.evidence_card_id,
                    evidence_fingerprint=card.evidence_fingerprint,
                    evidence_statement=card.evidence_statement,
                    evidence_type=card.evidence_type,
                    quote_text=card.quote_text,
                    provider_key=card.provider_key,
                    authority_tier=card.authority_tier_snapshot,
                    reporting_period_end=card.reporting_period_end,
                    source_published_at=card.source_published_at,
                    origin_type=card.origin_type,
                    relation=_strongest_relation(entry["relations"]),
                    claim_ids=bound,
                )
            )
        return items

    async def _call_model(self, pack: SectionInputPack) -> WriterDecision:
        """调用模型并归一到 WriterDecision（防御性 double-check）。"""
        if self._model is None:
            raise DraftSectionModelUnavailable()
        raw = await self._model.write(pack)
        if isinstance(raw, WriterDecision):
            return raw
        try:
            return WriterDecision.model_validate(raw)
        except ValidationError as exc:
            raise DraftSectionMalformedOutput() from exc

    async def _find_existing(self, writer_input_fingerprint: str) -> DraftSectionModel | None:
        async with self._sessionmaker() as session:
            return await DraftSectionRepository(session).get_by_writer_input_fingerprint(
                writer_input_fingerprint
            )

    @staticmethod
    def _verify_replay(
        *,
        verified: VerifiedReportOutline,
        section: OutlineSection,
        pack: SectionInputPack,
        writer_input_fingerprint: str,
        writer_model_id: str,
        row: DraftSectionModel,
    ) -> None:
        """replay 完整性校验（spec P）：既有行与本次派生完全一致。

        - 身份 / writer / 输入指纹字段逐一对比；
        - `verify_resolved_payload` 解析 persisted payload，验证全部
          Claim/Evidence ID 属于本 section allowed 集、index 在范围内；
        - 重算 section_fingerprint（writer_input_fingerprint + 既有 payload）
          与 persisted 对比。任一损坏 → `DraftSectionIntegrityError`，
          **不自动 repair**（payload / ID / 正文被篡改 → 拒绝）。
        """
        checks = [
            (row.outline_id, verified.outline_id, "outline_id"),
            (row.section_id, section.section_id, "section_id"),
            (row.section_order, section.section_order, "section_order"),
            (row.section_type, section.section_type, "section_type"),
            (row.title, section.title, "title"),
            (
                row.section_schema_version,
                DRAFT_SECTION_SCHEMA_VERSION,
                "section_schema_version",
            ),
            (row.writer_name, WRITER_NAME, "writer_name"),
            (row.writer_version, WRITER_VERSION, "writer_version"),
            (row.writer_model_id, writer_model_id, "writer_model_id"),
            (
                row.writer_input_fingerprint,
                writer_input_fingerprint,
                "writer_input_fingerprint",
            ),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise DraftSectionIntegrityError(f"draft section {field} mismatch")

        payload = row.section_payload
        verify_resolved_payload(pack, payload)
        recomputed = compute_section_fingerprint(
            writer_input_fingerprint=writer_input_fingerprint,
            section_payload=payload,
        )
        if recomputed != row.section_fingerprint:
            raise DraftSectionIntegrityError("draft section section_fingerprint mismatch")

    def _draft_model(
        self,
        *,
        verified: VerifiedReportOutline,
        section: OutlineSection,
        writer_input_fingerprint: str,
        payload: dict,
        section_fingerprint: str,
    ) -> DraftSectionModel:
        """把验证过的输出构造成不可变草稿行（payload 只存真实 ID）。"""
        return DraftSectionModel(
            draft_section_id=uuid.uuid4(),
            outline_id=verified.outline_id,
            section_id=section.section_id,
            section_order=section.section_order,
            section_type=section.section_type,
            title=section.title,
            section_schema_version=DRAFT_SECTION_SCHEMA_VERSION,
            writer_name=WRITER_NAME,
            writer_version=WRITER_VERSION,
            writer_model_id=self._model.model_id,
            writer_input_fingerprint=writer_input_fingerprint,
            section_payload=payload,
            section_fingerprint=section_fingerprint,
        )

    def _result(self, row: DraftSectionModel, *, replayed: bool) -> DraftSectionResult:
        return DraftSectionResult(
            draft_section_id=row.draft_section_id,
            outline_id=row.outline_id,
            section_id=row.section_id,
            section_fingerprint=row.section_fingerprint,
            writer_input_fingerprint=row.writer_input_fingerprint,
            replayed=replayed,
            paragraph_count=len(row.section_payload["paragraphs"]),
        )


def _strongest_relation(relations: set[str]) -> str:
    """多 (claim, evidence) 关系时取确定性最强（supports > contradicts > context）。"""
    return min(relations, key=_RELATION_ORDER.get)


def _conflict_fingerprint_data(conflicts: list[ResolvedConflict]) -> list[dict]:
    """conflict 的 canonical 指纹数据（claim_ids 排序，供输入指纹用）。"""
    return [
        {
            "claim_ids": sorted(str(cid) for cid in conflict.claim_ids),
            "description": conflict.description,
            "severity": conflict.severity,
            "resolution_direction": conflict.resolution_direction,
        }
        for conflict in conflicts
    ]


def _gap_fingerprint_data(gaps: list[ResolvedGap]) -> list[dict]:
    """gap 的 canonical 指纹数据（claim_ids 排序，供输入指纹用）。"""
    return [
        {
            "claim_ids": sorted(str(cid) for cid in gap.claim_ids),
            "description": gap.description,
            "suggested_evidence": gap.suggested_evidence,
            "priority": gap.priority,
        }
        for gap in gaps
    ]
