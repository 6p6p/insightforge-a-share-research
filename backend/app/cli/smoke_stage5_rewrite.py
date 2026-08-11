"""Real DeepSeek smoke (stage 5E.2A): Stage5 rewrite control loop — 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 在 Stage 5 控制环（spec D/O/N/R）里的
**两个生产适配器**能配合跑通 bounded rewrite loop：

    Stage5WorkflowRunner（真实 LangGraph + AsyncPostgresSaver）
      → build_report_draft（0 LLM：replay 已 seed 的 sections）
      → assemble_report → Deterministic Check（0 LLM，pass）
      → audit_report（**真实 DeepSeekAuditModel**：发现语义 overclaim → fail）
      → route_action（rewrite）→ rewrite_sections（**真实
        DeepSeekRevisionWriterModel**：修订 target section → 新 DraftSection）
      → assemble_report（**新** Report，spec N，不 UPDATE 旧 Report）
      → 新 Check（0 LLM）→ 新 audit（真实 DeepSeek）→ 收敛则 terminal
        `finalize`（run COMPLETED）。

seed 文案与 `smoke_report_audit` 相同（全程无数字，Deterministic Check 纯结构
pass；overclaim 是**语义**问题，只有 Auditor 能发现）：

- Claim C1：公司营业收入保持增长。（supports E1）
- Evidence E1：公司披露营业收入同比增长。
- Paragraph：公司的盈利能力已显著增强。（营收增长 ≠ 盈利能力增强）
- Claim C2（中性背景）：公司毛利率保持稳定。（满足 synthesis 2..50 契约）

outline 无 conflicts / evidence_gaps → 单 theme section（S1）→ 只走 1 轮修订
（target=S1）。期望真实模型收敛：audit#1 fail→rewrite→rewrite（真实）→新
Report→check→audit#2 pass→finalize。**若 2 轮内未收敛 → 记录 smoke fail**
（不重复刷模型）。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务数据**：清理删除 scratch 公司全部 seed 链路 + scratch
research_task / workflow_runs / workflow_events / checkpoint rows +
draft_section_revisions + review 层（action / request / decision / audit /
check / report / outline / draft / synthesis / claims / evidence / source /
company）。cleanup 后实际查询受影响表并打印 cleanup_success；cleanup 失败或
残留非 0 → 不声称成功（退出码 1）。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_stage5_rewrite
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
from app.db.models.research_task import ResearchTaskModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.contracts import DraftSectionRequest, ParagraphCandidate, WriterDecision
from app.draft_section.packs import SectionInputPack
from app.draft_section.service import DraftSectionService
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.report.check_service import ReportCheckService
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.research_backflow.service import ResearchBackflowService
from app.review.service import ReviewActionService
from app.revision.factory import create_revision_writer_model
from app.revision.service import RevisionService
from app.services.chunking_service import ChunkingService
from app.services.claim_service import ClaimService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.stage5.contracts import STAGE5_TERMINAL_FINALIZE, Stage5WorkflowRequest
from app.stage5.dependencies import Stage5WorkflowDependencies
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.contracts import SynthesisInputDraft
from app.synthesis.service import SynthesisService
from app.workflows.checkpoint import LangGraphCheckpointManager

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与盈利能力是否协调一致？"
_ANALYSIS_AS_OF = date(2026, 8, 10)
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# 语义 overclaim 场景（全程无数字，Deterministic Check 纯结构 pass）。
# 段落与 claim 同指标（营收增长），只把「保持增长」夸大为「大幅高速增长」——
# 量级 overclaim → evidence_mismatch / wording_overclaim → rewrite 路由（若换成
# 不同指标断言，如盈利能力增强，模型会判 unsupported → research，见 smoke）。
_CLAIM_STATEMENT = "公司营业收入保持增长。"
_EVIDENCE_STATEMENT = "公司披露营业收入同比增长。"
_OVERCLAIM_PARAGRAPH = "公司营业收入大幅高速增长。"
_SOURCE_URL = "https://www.xinhuanet.com/2026/0809/smoke_stage5_rewrite.htm"
_HTML = (
    "<html><head><title>营收增长</title></head><body><article><p>公司披露营业收入"
    "实现同比增长，主要来自核心产品销量提升。管理层表示市场需求保持稳健。</p>"
    "</article></body></html>"
)
# 第二条中性 Claim：满足 SynthesisInputDraft 的 2..50 契约（spec I）。
_CLAIM_MARGIN = "公司毛利率保持稳定。"
_EVIDENCE_MARGIN = "公司披露毛利率保持稳定。"
_SOURCE_URL_MARGIN = "https://www.xinhuanet.com/2026/0809/smoke_stage5_rewrite_margin.htm"
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

    引用 pack 中真实绑定的 C/E alias；text 把「保持增长」夸大为「大幅高速增长」
    ——同指标量级 overclaim，refs 真实、无数字 → Deterministic Check 结构 pass，
    但 Agent Audit 应判 evidence_mismatch / wording_overclaim（rewrite 路由）。
    """

    @property
    def model_id(self) -> str:
        return "deepseek:deepseek-v4-flash"

    async def write(self, pack: SectionInputPack) -> WriterDecision:
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


async def _cleanup(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    artifact_ids: list[uuid.UUID],
    run_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
) -> None:
    """删除 scratch 公司全部 seed 链路 + workflow / checkpoint / revision / review 层。

    只删本 smoke 数据，按 FK 依赖逆序删除。
    """
    cid = company_id
    chain_outline = "SELECT outline_id FROM report_outlines WHERE company_id = :cid"
    chain_report = f"SELECT report_id FROM reports WHERE outline_id IN ({chain_outline})"
    chain_check = (
        f"SELECT check_result_id FROM report_check_results WHERE report_id IN ({chain_report})"
    )
    chain_audit = f"SELECT audit_id FROM report_audits WHERE check_result_id IN ({chain_check})"
    chain_action = (
        f"SELECT review_action_id FROM report_review_actions WHERE audit_id IN ({chain_audit})"
    )
    chain_request = (
        f"SELECT human_request_id FROM human_review_requests WHERE review_action_id IN "
        f"({chain_action})"
    )
    chain_revision = (
        f"SELECT revision_id FROM draft_section_revisions WHERE source_draft_section_id IN "
        f"(SELECT draft_section_id FROM draft_sections WHERE outline_id IN ({chain_outline}))"
    )
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    async with sessionmaker() as session:
        if run_id is not None:
            tid = str(run_id)
            await session.execute(
                text("DELETE FROM workflow_events WHERE run_id = :rid").bindparams(rid=run_id)
            )
            await session.execute(
                text("DELETE FROM workflow_runs WHERE run_id = :rid").bindparams(rid=run_id)
            )
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :tid").bindparams(tid=tid)
                )
        if task_id is not None:
            await session.execute(
                text("DELETE FROM research_tasks WHERE task_id = :tid").bindparams(tid=task_id)
            )
        await session.execute(
            text(
                f"DELETE FROM draft_section_revisions WHERE revision_id IN ({chain_revision})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                f"DELETE FROM human_review_decisions WHERE human_request_id IN ({chain_request})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                f"DELETE FROM human_review_requests WHERE review_action_id IN ({chain_action})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM report_review_actions WHERE audit_id IN ({chain_audit})").bindparams(
                cid=cid
            )
        )
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
    run_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
) -> dict[str, int]:
    """实际查询 scratch 数据在受影响表中的残留行数（不猜测、不声称 0 残留）。"""
    cid = company_id
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    chain_outline = "SELECT outline_id FROM report_outlines WHERE company_id = :cid"
    chain_report = f"SELECT report_id FROM reports WHERE outline_id IN ({chain_outline})"
    chain_check = (
        f"SELECT check_result_id FROM report_check_results WHERE report_id IN ({chain_report})"
    )
    chain_audit = f"SELECT audit_id FROM report_audits WHERE check_result_id IN ({chain_check})"
    chain_action = (
        f"SELECT review_action_id FROM report_review_actions WHERE audit_id IN ({chain_audit})"
    )
    chain_request = (
        f"SELECT human_request_id FROM human_review_requests WHERE review_action_id IN "
        f"({chain_action})"
    )
    scoped: dict[str, str] = {
        "companies": "SELECT count(*) FROM companies WHERE company_id = :cid",
        "company_aliases": "SELECT count(*) FROM company_aliases WHERE company_id = :cid",
        "report_outlines": "SELECT count(*) FROM report_outlines WHERE company_id = :cid",
        "draft_sections": (
            "SELECT count(*) FROM draft_sections WHERE outline_id IN (" + chain_outline + ")"
        ),
        "reports": ("SELECT count(*) FROM reports WHERE outline_id IN (" + chain_outline + ")"),
        "report_check_results": (
            "SELECT count(*) FROM report_check_results WHERE report_id IN (" + chain_report + ")"
        ),
        "report_audits": (
            "SELECT count(*) FROM report_audits WHERE check_result_id IN (" + chain_check + ")"
        ),
        "review_issues": (
            "SELECT count(*) FROM review_issues WHERE audit_id IN (" + chain_audit + ")"
        ),
        "report_review_actions": (
            "SELECT count(*) FROM report_review_actions WHERE audit_id IN (" + chain_audit + ")"
        ),
        "human_review_requests": (
            "SELECT count(*) FROM human_review_requests WHERE review_action_id IN ("
            + chain_action
            + ")"
        ),
        "human_review_decisions": (
            "SELECT count(*) FROM human_review_decisions WHERE human_request_id IN ("
            + chain_request
            + ")"
        ),
        "draft_section_revisions": (
            "SELECT count(*) FROM draft_section_revisions WHERE source_draft_section_id IN "
            "(SELECT draft_section_id FROM draft_sections WHERE outline_id IN ("
            + chain_outline
            + "))"
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
    if run_id is not None:
        scoped["workflow_runs"] = "SELECT count(*) FROM workflow_runs WHERE run_id = :rid"
        scoped["workflow_events"] = "SELECT count(*) FROM workflow_events WHERE run_id = :rid"
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            scoped[table] = f"SELECT count(*) FROM {table} WHERE thread_id = :thread_key"
    if task_id is not None:
        scoped["research_tasks"] = "SELECT count(*) FROM research_tasks WHERE task_id = :task_key"
    async with sessionmaker() as session:
        counts: dict[str, int] = {}
        for table, sql in scoped.items():
            # 每条 SQL 只绑定它自己的参数：`TextClause.bindparams(**kw)` 遇到 text
            # 中不存在的关键字会抛 ArgumentError（不能为所有语句统一绑定全部参数）。
            if table in ("workflow_runs", "workflow_events"):
                params: dict = {"rid": run_id}
            elif table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                params = {"thread_key": str(run_id)}
            elif table == "research_tasks":
                params = {"task_key": task_id}
            else:
                params = {"cid": cid}
            counts[table] = (await session.execute(text(sql).bindparams(**params))).scalar_one()
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


async def _seed_research_task(sessionmaker) -> uuid.UUID:
    """seed 一个真实 ResearchTask（Stage 5 WorkflowRun 必须绑定任务）。"""
    task_id = uuid.uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=date(2026, 12, 31),
                modules=["company_profile"],
                questions=[],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


def _stage5_deps(sessionmaker, settings, revision_model) -> Stage5WorkflowDependencies:
    """Stage5 DI：draft_section_service 用 Fake writer（replay 已 seed 的 sections，
    0 LLM）；audit / revision 用**真实生产适配器**（DeepSeekAuditModel /
    DeepSeekRevisionWriterModel）。"""
    draft_service = DraftSectionService(sessionmaker, _OverclaimWriterModel())
    report_service = ReportService(sessionmaker, draft_service)
    check_service = ReportCheckService(sessionmaker, report_service)
    audit_service = ReportAuditService(sessionmaker, DeepSeekAuditModel(settings), check_service)
    review_service = ReviewActionService(sessionmaker, audit_service)
    revision_service = RevisionService(
        sessionmaker,
        model=revision_model,
        draft_section_service=draft_service,
        check_service=check_service,
        review_action_service=review_service,
    )
    report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环
    return Stage5WorkflowDependencies(
        sessionmaker=sessionmaker,
        report_outline_service=ReportOutlineService(sessionmaker),
        draft_section_service=draft_service,
        report_service=report_service,
        report_check_service=check_service,
        report_audit_service=audit_service,
        review_action_service=review_service,
        revision_service=revision_service,
        research_backflow_service=ResearchBackflowService(
            sessionmaker, review_service, report_service
        ),
    )


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
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_stage5_rewrite_"))
    raw_store = LocalRawArtifactStore(root=smoke_root / "raw", max_bytes=1024 * 1024)
    card_ids: list[uuid.UUID] = []
    artifact_ids: list[uuid.UUID] = []
    run_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    smoke_ok = False
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
                analyst_name="smoke-stage5-seeder",
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
                analyst_name="smoke-stage5-seeder",
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

        # Outline（0 LLM，verified synthesis result → 单 theme section S1）。
        outline = await ReportOutlineService(sessionmaker).create_or_get_outline(
            synth_result.synthesis_result_id
        )

        # Fake Writer seed sections（0 LLM）：S1 = overclaim 段落。
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
        assert len(section_ids) == 1, "smoke 期望单 theme section（无 gaps/conflicts）"

        # 真实 ResearchTask → Stage 5 run。
        task_id = await _seed_research_task(sessionmaker)
        request = Stage5WorkflowRequest(
            task_id=task_id,
            company_id=company_id,
            research_question=_QUESTION,
            analysis_as_of=_ANALYSIS_AS_OF,
            synthesis_result_id=synth_result.synthesis_result_id,
        )
        revision_model = create_revision_writer_model(settings)
        deps = _stage5_deps(sessionmaker, settings, revision_model)

        # 真实 LangGraph + AsyncPostgresSaver + 真实生产适配器。
        connection_uri = to_postgres_connection_uri(settings.database_url)
        checkpoint_manager = LangGraphCheckpointManager(connection_uri)
        await checkpoint_manager.setup()
        try:
            runner = Stage5WorkflowRunner(sessionmaker, checkpoint_manager, deps)
            run = await runner.create_stage5_run(request)
            run_id = run.run_id
            print(f"provider = {settings.llm_provider}")
            print(f"revision_writer_model_id = {revision_model.model_id}")
            start = time.perf_counter()
            result = await runner.execute_stage5(run.run_id, request)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
        finally:
            await checkpoint_manager.close()

        terminal = result.get("terminal")
        run_status = (await runner.get_run(run.run_id)).status.value
        revisions = result.get("revisions") or []
        print(f"latency_ms = {elapsed_ms}")
        print(f"stage5_terminal = {terminal}")
        print(f"run_status = {run_status}")
        print(f"final_route = {result.get('route')}")
        print(f"revision_round = {result.get('revision_round')}")
        print(f"revision_count = {len(revisions)}")
        for rev in revisions:
            print(
                f"revision[{rev['section_id']}] round={rev['revision_round']} "
                f"trigger={rev['trigger_type']}"
            )

        # 真实修订已落库：revised draft writer 身份（spec F）。
        async with sessionmaker() as session:
            rev_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT ds.writer_name, ds.writer_version, ds.writer_model_id, "
                            "r.revision_round, r.trigger_type FROM draft_sections ds "
                            "JOIN draft_section_revisions r "
                            "ON r.revised_draft_section_id = ds.draft_section_id "
                            "JOIN draft_sections src "
                            "ON src.draft_section_id = r.source_draft_section_id "
                            "JOIN reports rp ON rp.outline_id = src.outline_id "
                            "WHERE rp.outline_id IN "
                            "(SELECT outline_id FROM report_outlines WHERE company_id = :cid)"
                        ).bindparams(cid=company_id)
                    )
                )
                .mappings()
                .all()
            )
        for row in rev_rows:
            print(f"revision_writer_name = {row['writer_name']}")
            print(f"revision_writer_version = {row['writer_version']}")
            print(f"revision_writer_model_id = {row['writer_model_id']}")
            print(f"revision_trigger = {row['trigger_type']}")

        if not revisions:
            # 诊断：真实审计把 overclaim 判成了什么（research issue codes）——
            # 帮助区分「seed 场景设计不当」vs「审计路由回归」。
            async with sessionmaker() as session:
                issue_rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT ri.issue_type, ri.message FROM review_issues ri "
                                "JOIN report_audits a ON a.audit_id = ri.audit_id "
                                "JOIN report_check_results c ON c.check_result_id = "
                                "a.check_result_id "
                                "JOIN reports r ON r.report_id = c.report_id "
                                "JOIN report_outlines o ON o.outline_id = r.outline_id "
                                "WHERE o.company_id = :cid"
                            ).bindparams(cid=company_id)
                        )
                    )
                    .mappings()
                    .all()
                )
            codes = [row["issue_type"] for row in issue_rows]
            print(f"audit_issue_codes = {codes}")
            print("FAIL: 未产生任何修订（真实 DeepSeek 未进入 rewrite 分支）")
            return 1
        if any(row["writer_name"] != "evidence_bound_section_rewriter" for row in rev_rows):
            print("FAIL: 修订正文 writer 身份不是 evidence_bound_section_rewriter")
            return 1
        if terminal != STAGE5_TERMINAL_FINALIZE:
            print(
                f"FAIL: 真实 DeepSeek 未在 bounded loop 内消除 overclaim（terminal={terminal}，"
                "不重复刷模型）"
            )
            return 1
        if run_status != "completed":
            print(f"FAIL: run status 应为 completed，实际 = {run_status}")
            return 1
        smoke_ok = True
    finally:
        cleanup_ok = False
        try:
            await _cleanup(
                sessionmaker,
                company_id=company_id,
                artifact_ids=artifact_ids,
                run_id=run_id,
                task_id=task_id,
            )
            residual = await _residual_counts(
                sessionmaker,
                company_id=company_id,
                artifact_ids=artifact_ids,
                run_id=run_id,
                task_id=task_id,
            )
            cleanup_ok = all(count == 0 for count in residual.values())
            print(f"cleanup_success = {cleanup_ok}")
            if not cleanup_ok:
                print(f"residual_rows = {sum(residual.values())}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup_failure = {type(exc).__name__}: {exc}")
        await manager.dispose()
        shutil.rmtree(smoke_root, ignore_errors=True)
    if smoke_ok and cleanup_ok:
        print("OK: real DeepSeek Stage5 rewrite control loop smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
