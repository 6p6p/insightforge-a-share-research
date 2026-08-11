"""Real DeepSeek smoke (stage 5D): evidence-bound report audit — 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 作为 Evidence-bound Auditor，对一段
**语义 overclaim** 的段落能返回结构化 `AuditDecision`（reviewed refs + issues），
并走**完整生产链路**：

    Evidence(E1 document card) → ClaimService(正式, supports E1)
      → Synthesis → SynthesisAnalysis → ReportOutline(0 LLM)
      → Fake Writer（段落「盈利能力已显著增强」= 语义 overclaim，0 LLM）
      → Report 装配(0 LLM) → Deterministic Check(pass，0 LLM)
      → ReportAuditService + 生产适配器 `DeepSeekAuditModel`（thinking disabled、
        temperature=0、structured output、0 tools/0 web）。

seed 文案**不含任何数字**（绕过 numeric grounding，让 Deterministic Check 纯结构
pass；overclaim 是**语义**问题，只有 Auditor 能发现）：

- Claim C1：公司营业收入保持增长。（supports E1）
- Evidence E1：公司披露营业收入同比增长。
- Paragraph：公司的盈利能力已显著增强。（营收增长 ≠ 盈利能力增强）
- Claim C2（中性背景）：公司毛利率保持稳定。（supports E2；仅为满足 synthesis
  2..50 契约，段落不引用它，不进入审计 Pack）

审计期望：status=fail，route ∈ {rewrite, research}，issue ∈ {wording_overclaim,
evidence_mismatch, unsupported_by_evidence, claim_misrepresentation}。**若模型
PASS → 记录 smoke fail**（不重复刷模型）。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务数据**：清理删除 scratch 公司全部 seed 链路（含 report / check /
audit / issues）。cleanup 后实际查询受影响表并打印 cleanup_success；cleanup
失败或残留非 0 → 不声称成功（退出码 1）。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_report_audit
"""

import asyncio
import shutil
import sys
import tempfile
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisAnalysisRequest,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisTheme,
)
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.audit.adapters import DeepSeekAuditModel
from app.audit.contracts import ReportAuditRequest
from app.audit.service import ReportAuditService
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimImportance,
    ClaimKind,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.draft_section.contracts import DraftSectionRequest, ParagraphCandidate, WriterDecision
from app.draft_section.packs import SectionInputPack
from app.draft_section.service import DraftSectionService
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.report.check_service import ReportCheckService
from app.report.contracts import CHECK_STATUS_PASS, ReportAssemblyDraft
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.claim_service import ClaimService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.contracts import SynthesisInputDraft
from app.synthesis.service import SynthesisService

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与盈利能力是否协调一致？"
_ANALYSIS_AS_OF = date(2026, 8, 10)
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# 语义 overclaim 场景（全程无数字，Deterministic Check 纯结构 pass）。
_CLAIM_STATEMENT = "公司营业收入保持增长。"
_EVIDENCE_STATEMENT = "公司披露营业收入同比增长。"
_OVERCLAIM_PARAGRAPH = "公司的盈利能力已显著增强。"
_SOURCE_URL = "https://www.xinhuanet.com/2026/0809/smoke_report_audit.htm"
_HTML = (
    "<html><head><title>营收增长</title></head><body><article><p>公司披露营业收入"
    "实现同比增长，主要来自核心产品销量提升。管理层表示市场需求保持稳健。</p>"
    "</article></body></html>"
)
# 第二条中性 Claim：满足 SynthesisInputDraft 的 2..50 契约（spec I）。段落只引用
# C1，C2 只是背景 Claim，不进入审计 Pack。
_CLAIM_MARGIN = "公司毛利率保持稳定。"
_EVIDENCE_MARGIN = "公司披露毛利率保持稳定。"
_SOURCE_URL_MARGIN = "https://www.xinhuanet.com/2026/0809/smoke_report_audit_margin.htm"
_HTML_MARGIN = (
    "<html><head><title>毛利率稳定</title></head><body><article><p>公司披露毛利率"
    "保持稳定，费用结构基本不变。管理层表示经营效率平稳。</p></article></body></html>"
)


class _FakeSynthesisAnalysisModel:
    """Deterministic synthesis fake：theme + claim_role 恰好覆盖唯一 Claim。"""

    def __init__(self, output: SynthesisAnalysisOutput) -> None:
        self._output = output

    @property
    def model_id(self) -> str:
        return "deepseek:deepseek-v4-flash"

    async def analyze(self, context, claim_pack) -> SynthesisAnalysisOutput:
        return self._output


class _OverclaimWriterModel:
    """Deterministic section writer fake：输出语义 overclaim 段落（0 LLM）。

    引用 pack 中真实绑定的 C/E alias；text 是「盈利能力增强」——与 claim（营收
    增长）语义不符，但结构合法（refs 真实、无数字），Deterministic Check 不拦截。
    """

    @property
    def model_id(self) -> str:
        return "deepseek:deepseek-v4-flash"

    async def write(self, pack: SectionInputPack) -> WriterDecision:
        # 选中「营业收入保持增长」Claim（审计目标保持唯一，不受 alias 排序影响）。
        claim = next(item for item in pack.claims if item.statement == _CLAIM_STATEMENT)
        evidence = next(item for item in pack.evidence if claim.alias in item.claim_aliases)
        return WriterDecision(
            paragraphs=[
                ParagraphCandidate(
                    text=_OVERCLAIM_PARAGRAPH,
                    claim_refs=[claim.alias],
                    evidence_refs=[evidence.alias],
                )
            ]
        )


async def _cleanup(sessionmaker, *, company_id: uuid.UUID, artifact_ids: list[uuid.UUID]) -> None:
    """删除 scratch 公司全部 seed 链路（含 smoke 创建的 outline / draft /
    report / check / audit / issues），只删本 smoke 数据。按 FK 依赖逆序删除。"""
    cid = company_id
    chain_outline = "SELECT outline_id FROM report_outlines WHERE company_id = :cid"
    chain_report = f"SELECT report_id FROM reports WHERE outline_id IN ({chain_outline})"
    chain_check = (
        f"SELECT check_result_id FROM report_check_results WHERE report_id IN ({chain_report})"
    )
    chain_audit = f"SELECT audit_id FROM report_audits WHERE check_result_id IN ({chain_check})"
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    async with sessionmaker() as session:
        await session.execute(
            text(f"DELETE FROM review_issues WHERE audit_id IN ({chain_audit})").bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM report_audits WHERE check_result_id IN ({chain_check})").bindparams(
                cid=cid
            )
        )
        await session.execute(
            text(
                f"DELETE FROM report_check_results WHERE report_id IN ({chain_report})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM reports WHERE outline_id IN ({chain_outline})").bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM draft_sections WHERE outline_id IN ({chain_outline})").bindparams(
                cid=cid
            )
        )
        await session.execute(
            text("DELETE FROM report_outlines WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.execute(
            text(
                "DELETE FROM claim_synthesis_results WHERE synthesis_id IN "
                "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                "DELETE FROM claim_synthesis_input_links WHERE synthesis_id IN "
                "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM claim_synthesis_runs WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.execute(
            text(
                "DELETE FROM claim_evidence_links WHERE claim_id IN "
                "(SELECT claim_id FROM claims WHERE company_id = :cid)"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM claims WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM evidence_cards WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.execute(
            text(
                f"DELETE FROM document_chunks WHERE chunk_set_id IN ({chain_chunkset})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                f"DELETE FROM chunk_vector_indexes WHERE chunk_set_id IN ({chain_chunkset})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})").bindparams(
                cid=cid
            )
        )
        await session.execute(
            text(
                f"DELETE FROM parsed_source_blocks WHERE parsed_source_id IN ({chain_parsed})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM parsed_sources WHERE source_id IN ({chain_src})").bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM source_records WHERE company_id = :cid").bindparams(cid=cid)
        )
        for aid in artifact_ids:
            await session.execute(
                text("DELETE FROM raw_artifacts WHERE artifact_id = :aid").bindparams(aid=aid)
            )
        await session.execute(
            text("DELETE FROM company_aliases WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM companies WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.commit()


async def _residual_counts(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    artifact_ids: list[uuid.UUID],
) -> dict[str, int]:
    """实际查询 scratch company 在受影响表中的残留行数（不猜测、不声称 0 残留）。"""
    cid = company_id
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    scoped: dict[str, str] = {
        "companies": "SELECT count(*) FROM companies WHERE company_id = :cid",
        "company_aliases": "SELECT count(*) FROM company_aliases WHERE company_id = :cid",
        "report_outlines": "SELECT count(*) FROM report_outlines WHERE company_id = :cid",
        "draft_sections": (
            "SELECT count(*) FROM draft_sections WHERE outline_id IN "
            "(SELECT outline_id FROM report_outlines WHERE company_id = :cid)"
        ),
        "reports": (
            "SELECT count(*) FROM reports WHERE outline_id IN "
            "(SELECT outline_id FROM report_outlines WHERE company_id = :cid)"
        ),
        "report_check_results": (
            "SELECT count(*) FROM report_check_results WHERE report_id IN "
            "(SELECT report_id FROM reports WHERE outline_id IN "
            "(SELECT outline_id FROM report_outlines WHERE company_id = :cid))"
        ),
        "report_audits": (
            "SELECT count(*) FROM report_audits WHERE check_result_id IN "
            "(SELECT check_result_id FROM report_check_results WHERE report_id IN "
            "(SELECT report_id FROM reports WHERE outline_id IN "
            "(SELECT outline_id FROM report_outlines WHERE company_id = :cid)))"
        ),
        "review_issues": (
            "SELECT count(*) FROM review_issues WHERE audit_id IN "
            "(SELECT audit_id FROM report_audits WHERE check_result_id IN "
            "(SELECT check_result_id FROM report_check_results WHERE report_id IN "
            "(SELECT report_id FROM reports WHERE outline_id IN "
            "(SELECT outline_id FROM report_outlines WHERE company_id = :cid))))"
        ),
        "claim_synthesis_runs": "SELECT count(*) FROM claim_synthesis_runs WHERE company_id = :cid",
        "claim_synthesis_input_links": (
            "SELECT count(*) FROM claim_synthesis_input_links WHERE synthesis_id IN "
            "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
        ),
        "claim_synthesis_results": (
            "SELECT count(*) FROM claim_synthesis_results WHERE synthesis_id IN "
            "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
        ),
        "claims": "SELECT count(*) FROM claims WHERE company_id = :cid",
        "claim_evidence_links": (
            "SELECT count(*) FROM claim_evidence_links WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid)"
        ),
        "evidence_cards": "SELECT count(*) FROM evidence_cards WHERE company_id = :cid",
        "source_records": "SELECT count(*) FROM source_records WHERE company_id = :cid",
        "parsed_sources": (
            "SELECT count(*) FROM parsed_sources WHERE source_id IN (" + chain_src + ")"
        ),
        "parsed_source_blocks": (
            "SELECT count(*) FROM parsed_source_blocks WHERE parsed_source_id IN ("
            + chain_parsed
            + ")"
        ),
        "chunk_sets": (
            "SELECT count(*) FROM chunk_sets WHERE parsed_source_id IN (" + chain_parsed + ")"
        ),
        "document_chunks": (
            "SELECT count(*) FROM document_chunks WHERE chunk_set_id IN (" + chain_chunkset + ")"
        ),
        "chunk_vector_indexes": (
            "SELECT count(*) FROM chunk_vector_indexes WHERE chunk_set_id IN ("
            + chain_chunkset
            + ")"
        ),
    }
    async with sessionmaker() as session:
        counts: dict[str, int] = {}
        for table, sql in scoped.items():
            counts[table] = (await session.execute(text(sql).bindparams(cid=cid))).scalar_one()
        for aid in artifact_ids:
            counts[f"raw_artifacts:{aid.hex[:8]}"] = (
                await session.execute(
                    text("SELECT count(*) FROM raw_artifacts WHERE artifact_id = :aid").bindparams(
                        aid=aid
                    )
                )
            ).scalar_one()
        return counts


async def _seed_document_card(
    sessionmaker,
    raw_store,
    *,
    company_id: uuid.UUID,
    html: bytes,
    statement: str,
    source_url: str,
) -> dict:
    """真实 HTML 链（RawArtifact → SourceRecord → Parsing → Chunking →
    EvidenceCardService）→ 1 张 news_article document EvidenceCard。"""
    stored = raw_store.put_html_bytes(html)
    async with sessionmaker() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
        assert artifact is not None
        record = SourceRecordModel(
            company_id=company_id,
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title="新闻标题",
            published_at=_PUBLISHED_AT,
            reporting_period_end=None,
            source_url=source_url,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
        artifact_id = artifact.artifact_id
    parsed_service = SourceParsingService(sessionmaker, raw_store)
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(sessionmaker).chunk_parsed_source(parsed.parsed_source_id)
    async with sessionmaker() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "smoke seed must produce chunks"
    chunk = chunks[0]
    draft = EvidenceCardDraft(
        research_question=_QUESTION,
        evidence_statement=statement,
        evidence_type=EvidenceType.STATEMENT,
        chunk_id=chunk.chunk_id,
        quote_start=0,
        quote_end=20,
        extractor_name="smoke-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    result_card = await EvidenceCardService(sessionmaker).create_card(draft)
    return {
        "evidence_card_id": result_card.evidence_card_id,
        "artifact_id": artifact_id,
        "source_id": source_id,
    }


async def _main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    settings = get_settings()
    if settings.deepseek_api_key is None:
        print("DEEPSEEK_API_KEY 未配置，跳过真实 smoke（零真实 LLM）。", file=sys.stderr)
        return 2
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    sessionmaker = manager.session_factory()
    company_id = uuid.uuid4()
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_report_audit_"))
    raw_store = LocalRawArtifactStore(root=smoke_root / "raw", max_bytes=1024 * 1024)
    card_ids: list[uuid.UUID] = []
    artifact_ids: list[uuid.UUID] = []
    audit_ok = False
    try:
        await SourceRegistryService(sessionmaker).seed_defaults()
        async with sessionmaker() as session:
            await CompanyRepository(session).create(
                CompanyModel(
                    company_id=company_id,
                    exchange="SSE",
                    security_code="600519",
                    identity_key="SSE:600519",
                    board="sse_main",
                    official_name="Smoke测试公司",
                    short_name="Smoke",
                    listing_status="listed",
                    identity_source_provider_key="sse",
                    identity_source_url="https://www.sse.com.cn",
                )
            )
            await session.commit()

        card = await _seed_document_card(
            sessionmaker,
            raw_store,
            company_id=company_id,
            html=_HTML.encode(),
            statement=_EVIDENCE_STATEMENT,
            source_url=_SOURCE_URL,
        )
        card_ids.append(card["evidence_card_id"])
        artifact_ids.append(card["artifact_id"])

        # 正式 ClaimService：Claim「营业收入保持增长」supports E1（真实 fingerprint）。
        claim_result = await ClaimService(sessionmaker).create_claim(
            ClaimDraft(
                company_id=company_id,
                research_question=_QUESTION,
                statement=_CLAIM_STATEMENT,
                analysis_domain=ClaimAnalysisDomain.BUSINESS,
                claim_kind=ClaimKind.INFERENCE,
                confidence=ClaimConfidence.MEDIUM,
                importance=ClaimImportance.NORMAL,
                support_evidence_ids=[card["evidence_card_id"]],
                contradict_evidence_ids=[],
                context_evidence_ids=[],
                analyst_name="smoke-audit-seeder",
                analyst_version=1,
                analyst_model_id="deepseek:deepseek-v4-flash",
            )
        )
        claim_id = claim_result.claim_id

        # 第二张卡 + 第二 Claim：满足 SynthesisInputDraft 的 2..50 契约（spec I）。
        card2 = await _seed_document_card(
            sessionmaker,
            raw_store,
            company_id=company_id,
            html=_HTML_MARGIN.encode(),
            statement=_EVIDENCE_MARGIN,
            source_url=_SOURCE_URL_MARGIN,
        )
        card_ids.append(card2["evidence_card_id"])
        artifact_ids.append(card2["artifact_id"])
        claim2_result = await ClaimService(sessionmaker).create_claim(
            ClaimDraft(
                company_id=company_id,
                research_question=_QUESTION,
                statement=_CLAIM_MARGIN,
                analysis_domain=ClaimAnalysisDomain.BUSINESS,
                claim_kind=ClaimKind.INFERENCE,
                confidence=ClaimConfidence.MEDIUM,
                importance=ClaimImportance.NORMAL,
                support_evidence_ids=[card2["evidence_card_id"]],
                contradict_evidence_ids=[],
                context_evidence_ids=[],
                analyst_name="smoke-audit-seeder",
                analyst_version=1,
                analyst_model_id="deepseek:deepseek-v4-flash",
            )
        )
        claim2_id = claim2_result.claim_id

        # Synthesis + SynthesisAnalysis（Fake，theme 覆盖两条 Claim）。
        synthesis = await SynthesisService(sessionmaker).create_or_get_synthesis(
            SynthesisInputDraft(
                company_id=company_id,
                research_question=_QUESTION,
                analysis_as_of=_ANALYSIS_AS_OF,
                claim_ids=[claim_id, claim2_id],
            )
        )
        synth_model = _FakeSynthesisAnalysisModel(
            output=SynthesisAnalysisOutput(
                summary="综合判断：营收与盈利能力。",
                themes=[
                    SynthesisTheme(title="主题A：营收与盈利", summary="A", claim_refs=["C1", "C2"]),
                ],
                claim_roles=[
                    SynthesisClaimRoleAssignment(
                        claim_ref="C1", role=SynthesisClaimRole.SUPPORT, rationale="支持 C1"
                    ),
                    SynthesisClaimRoleAssignment(
                        claim_ref="C2", role=SynthesisClaimRole.SUPPORT, rationale="支持 C2"
                    ),
                ],
                duplicates=[],
                conflicts=[],
                evidence_gaps=[],
            )
        )
        synth_result = await SynthesisAnalysisService(sessionmaker, synth_model).analyze(
            SynthesisAnalysisRequest(synthesis_id=synthesis.synthesis_id)
        )

        # Outline（0 LLM，verified synthesis result → S1）。
        outline = await ReportOutlineService(sessionmaker).create_or_get_outline(
            synth_result.synthesis_result_id
        )

        # Fake Writer：段落「盈利能力已显著增强」（语义 overclaim，结构合法）。
        async with sessionmaker() as session:
            outline_row = (
                (
                    await session.execute(
                        text(
                            "SELECT outline_payload FROM report_outlines WHERE outline_id = :oid"
                        ).bindparams(oid=outline.outline_id)
                    )
                )
                .mappings()
                .first()
            )
        assert outline_row is not None
        section_ids = [s["section_id"] for s in outline_row["outline_payload"]["sections"]]
        writer = _OverclaimWriterModel()
        draft_service = DraftSectionService(sessionmaker, writer)
        draft_ids: list[uuid.UUID] = []
        for section_id in section_ids:
            draft = await draft_service.create_or_get_section(
                DraftSectionRequest(outline_id=outline.outline_id, section_id=section_id)
            )
            draft_ids.append(draft.draft_section_id)

        # Report 装配（0 LLM）。
        report_service = ReportService(sessionmaker, DraftSectionService(sessionmaker, writer))
        report = await report_service.create_or_get_report(
            ReportAssemblyDraft(outline_id=outline.outline_id, draft_section_ids=tuple(draft_ids))
        )

        # Deterministic Check（10 项 v1，0 LLM）→ 必须 pass。
        check_service = ReportCheckService(sessionmaker, report_service)
        check = await check_service.run_report_checks(report.report_id)
        print(f"check_status = {check.status}")
        if check.status != CHECK_STATUS_PASS:
            print(f"FAIL: Deterministic Check 应 pass（结构合法），实际 = {check.status}")
            return 1

        # 真实审计：生产适配器 DeepSeekAuditModel → ReportAuditService。
        model = DeepSeekAuditModel(settings)
        audit_service = ReportAuditService(sessionmaker, model, check_service)
        print(f"provider = {settings.llm_provider}")
        print(f"auditor_model_id = {model.model_id}")

        start = time.perf_counter()
        result = await audit_service.create_or_get_audit(
            ReportAuditRequest(report_id=report.report_id, check_result_id=check.check_result_id)
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        print(f"latency_ms = {elapsed_ms}")
        print(f"audit_schema_version = {result.audit_schema_version}")
        print(f"audit_status = {result.audit_status}")
        print(f"recommended_route = {result.recommended_route}")
        print(f"issue_count = {result.issue_count}")
        print(f"replayed = {result.replayed}")
        print(f"audit_fingerprint = {result.audit_fingerprint}")

        if result.audit_status == "pass":
            print("FAIL: Auditor 未发现语义 overclaim（应 fail + rewrite/research）")
            print("smoke_note = model passed a clear overclaim; no re-roll")
            return 1
        if result.recommended_route not in ("rewrite", "research"):
            print(f"FAIL: route 应为 rewrite/research，实际 = {result.recommended_route}")
            return 1
        print("audit_found_semantic_overclaim = True")
        audit_ok = True
    finally:
        cleanup_ok = False
        try:
            await _cleanup(sessionmaker, company_id=company_id, artifact_ids=artifact_ids)
            residual = await _residual_counts(
                sessionmaker, company_id=company_id, artifact_ids=artifact_ids
            )
            cleanup_ok = all(count == 0 for count in residual.values())
            print(f"cleanup_success = {cleanup_ok}")
            if not cleanup_ok:
                print(f"residual_rows = {sum(residual.values())}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup_failure = {type(exc).__name__}")
        await manager.dispose()
        shutil.rmtree(smoke_root, ignore_errors=True)
    if audit_ok and cleanup_ok:
        print("OK: real DeepSeek evidence-bound report audit smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
