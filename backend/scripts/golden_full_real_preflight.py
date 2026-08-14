"""Golden INSIGHTFORGE_FULL real-model preflight（Stage 7 Final Closeout）。

验证对象 = full variant 的引擎：**生产顶层编排**（`ResearchOrchestrationRunner` +
真实 fulfillment / Stage4 / Stage5 / backflow + PG Checkpointer，顶层
`thread_id=orchestration_id`）。两个模式：

- `--mode real-planner`（默认）：**真实 DeepSeek planner** + 其余全真实。证明
  「真实模型运行 → 正常进入 manual/WAITING_HUMAN（waiting_manual）→ 人工补资料
  （真实 PDF 上传）→ 人工 action（`resume_after_source_acquisition`，同一
  orchestration / 顶层 thread / checkpoint 恢复）→ prep 重算 + 真实 fulfill 重试」。
  真实 planner 自由输出可能请求超出 seed 范围的 needs（记录为开环补料，不无限循环）。
- `--mode controlled-plan`：planner 输出受控（与 `_seed_worker_inputs` 匹配的
  payload，缺 annual_report），**其余全部真实**（真实 extractor / 5 analysts /
  synthesis / draft / audit / revision / backflow + 真实 BGE + 真实 Chroma）。
  证明「waiting_manual → 人工补资料（真实上传 + 真实提取证据卡）→ resume 同线程
  → Stage4 → Stage5（真实 audit 判定）→（若 human_review）awaiting_stage5 →
  人工 approve → completed → **最终 report / audit / citation 链路**」。

**人工确认机制不绕过**：所有人工 action 走生产 service API（脚本只代替人类调用；
human_review_requests / waiting_human 状态机 / 同线程恢复全部真实）。

用法（backend 目录，insightforge conda env，需真实 DEEPSEEK_API_KEY）：
    python -m scripts.golden_full_real_preflight [--mode real-planner|controlled-plan]
        [--keep] [--timeout 900]

退出码：0 = 验证通过；2 = 到达 terminal 但链路断言失败；3 = 超时 / blocker。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.domain.source_records import SourceDocumentType
from app.eval.benchmark.dataset import (
    _seed_company,
    _seed_document,
    _seed_financial_observation,
    _seed_macro,
    _seed_valuation_observation,
)
from app.eval.usage.collector import EvalLlmUsageCollector
from app.eval.variants import EvalVariantId
from app.financial.calculations.contracts import (
    CalculationCode,
    FinancialCalculationDraft,
    InputRole,
)
from app.financial.calculations.service import FinancialCalculationService
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_orchestration.contracts import (
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.execution_manager import ResearchOrchestrationExecutionManager
from app.research_orchestration.factory import create_research_orchestration_dependencies
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import ResearchOrchestrationService
from app.research_planning.contracts import ResearchPlanPayload
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_ingestion_service import SourceIngestionService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.pdf_fixtures import single_page_pdf
from tests.research_planning.fakes import FakeResearchPlannerModel

configure_asyncio_runtime()

QUESTION = "贵州茅台2023年基本面、宏观环境与估值水平综合如何？"
AS_OF = date(2025, 8, 1)
COLLECTOR_CASE_ID = "golden-full-real-preflight"
PEER_PE_BY_CODE = {"600502": "19", "600503": "17", "600504": "15"}

# 受控 plan（controlled-plan 模式）：请求全部已 seed 的输入 + annual_report
# （seed 缺它 → 预置 waiting_manual 人工环节）。
_CONTROLLED_PLAN = ResearchPlanPayload.model_validate(
    {
        "research_scope": ["business", "risk", "financial", "macro", "valuation"],
        "analysis_modules": [
            "business_event",
            "risk",
            "financial",
            "macro",
            "valuation",
        ],
        "document_needs": [
            {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"},
            {
                "need_code": "annual_docs",
                "purpose": "需要年度报告",
                "source_type": "annual_report",
                "period": "2023",
            },
        ],
        "financial_needs": [
            {
                "need_code": "fin_rev_change",
                "purpose": "需要营收绝对变化",
                "calculation_code": "absolute_change_cny",
                "metric_code": "revenue",
                "period": "2023",
            }
        ],
        "macro_needs": [
            {
                "need_code": "macro_pop",
                "purpose": "需要人口宏观数据",
                "topic_or_indicator": "Population, total",
            }
        ],
        "event_needs": [],
        "valuation_needs": [
            {"need_code": "val_pe", "purpose": "需要市盈率比较", "metric_code": "pe_ttm"}
        ],
        "research_focus": ["经营质量", "估值水平"],
    }
)


def _log(message: str) -> None:
    print(f"[preflight] {message}", flush=True)


# ------------------------------------------------------------------ cleanup / seed


async def _cleanup(sessionmaker) -> None:
    """与集成套件同序：orchestration/plan 层 → revision 层 → 公共层。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM research_orchestration_child_runs"))
        await session.execute(text("DELETE FROM research_orchestration_runs"))
        await session.execute(text("DELETE FROM research_plan_routes"))
        await session.execute(text("DELETE FROM research_plans"))
        await session.execute(text("DELETE FROM report_exports"))
        await session.execute(text("DELETE FROM research_backflow_plans"))
        await session.execute(text("DELETE FROM research_backflow_fulfillments"))
        await session.execute(text("DELETE FROM research_backflow_requests"))
        await session.execute(text("DELETE FROM draft_section_revisions"))
        await session.execute(text("DELETE FROM human_review_decisions"))
        await session.execute(text("DELETE FROM human_review_requests"))
        await session.execute(text("DELETE FROM report_review_actions"))
        await session.execute(text("DELETE FROM review_issues"))
        await session.execute(text("DELETE FROM report_audits"))
        await session.execute(text("DELETE FROM report_check_results"))
        await session.execute(text("DELETE FROM reports"))
        await session.execute(text("DELETE FROM workflow_events"))
        await session.execute(text("DELETE FROM workflow_runs"))
        await session.execute(text("DELETE FROM research_tasks"))
        await session.execute(text("DELETE FROM draft_sections"))
        await session.execute(text("DELETE FROM report_outlines"))
        await session.execute(text("DELETE FROM claim_synthesis_results"))
        await session.execute(text("DELETE FROM claim_synthesis_input_links"))
        await session.execute(text("DELETE FROM claim_synthesis_runs"))
        await session.execute(text("DELETE FROM claim_relative_valuation_comparison_links"))
        await session.execute(text("DELETE FROM relative_valuation_claim_profiles"))
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM macro_transmission_evidence_links"))
        await session.execute(text("DELETE FROM macro_transmission_chains"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
        await session.execute(text("DELETE FROM chunk_vector_indexes"))
        await session.execute(text("DELETE FROM document_chunks"))
        await session.execute(text("DELETE FROM chunk_sets"))
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM news_source_verifications"))
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


async def _seed_world_bank_provider(sessionmaker) -> None:
    from app.db.models.source_provider import SourceProviderModel
    from app.repositories.source_provider_repository import SourceProviderRepository

    async with sessionmaker() as session:
        existing = await SourceProviderRepository(session).get_by_key("world_bank")
        if existing is None:
            await SourceProviderRepository(session).upsert(
                SourceProviderModel(
                    provider_key="world_bank",
                    display_name="World Bank Open Data",
                    provider_type="international_organization",
                    authority_tier=1,
                    homepage_url="https://data.worldbank.org",
                    allowed_domains=["worldbank.org"],
                    capabilities=["macro_data", "document_download"],
                    acquisition_methods=["official_api"],
                    exchange_scope=[],
                    requires_api_key=False,
                    critical_claim_eligible=True,
                    enabled=True,
                )
            )
            await session.commit()


async def _seed_task(sessionmaker, company_query: str) -> UUID:
    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query=company_query,
                research_start_date=date(2023, 1, 1),
                # analysis_as_of = task.research_end_date（planning 派生）：必须与
                # seed 的 comparison.analysis_as_of 一致（跨集合严格 same-date）。
                research_end_date=AS_OF,
                modules=["company_profile"],
                questions=[QUESTION],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


async def _document_card(env: dict, *, statement: str, source_url: str) -> dict:
    """真实 HTML 链 → parse → chunk → EvidenceCard（research_question=QUESTION）。

    返回 {source_id, chunk_id, evidence_card_id}。
    """
    from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
    from app.repositories.document_chunk_repository import DocumentChunkRepository
    from app.services.chunking_service import ChunkingService
    from app.services.source_parsing_service import SourceParsingService

    seeded = await _seed_document(
        env,
        env["company_id"],
        document_type="news_article",
        title="贵州茅台公开信息",
        body_text=statement,
        source_url=source_url,
        published_at=datetime(2024, 6, 1, tzinfo=UTC),
    )
    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(
        seeded["source_id"]
    )
    chunked = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(
        parsed.parsed_source_id
    )
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(chunked.chunk_set_id)
    chunk = chunks[0]
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=QUESTION,
            evidence_statement=statement,
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=0,
            quote_end=min(30, len(chunk.text)),
            extractor_name="preflight-curator",
            extractor_version=1,
            extractor_model_id="curated",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    return {
        "source_id": seeded["source_id"],
        "chunk_id": chunk.chunk_id,
        "evidence_card_id": card.evidence_card_id,
    }


async def _macro_card(env: dict) -> dict:
    """macro snapshot（CapturedMacroFetch 离线）→ observation 卡。

    snapshot fetched_at 固定为确定性过去日期（<= analysis_as_of，no-lookahead）。
    """
    from app.evidence.contracts import EvidenceConfidence, MacroEvidenceDraft
    from app.repositories.macro_observation_repository import MacroObservationRepository
    from app.services.macro_evidence_service import MacroEvidenceService

    seeded = await _seed_macro(env)
    async with env["sessionmaker"]() as session:
        observations = await MacroObservationRepository(session).list_for_snapshot(
            seeded["snapshot_id"]
        )
        obs = observations[0]
        await session.execute(
            text(
                "UPDATE macro_dataset_snapshots SET fetched_at = :at WHERE snapshot_id = :sid"
            ).bindparams(at=datetime(2025, 6, 1, tzinfo=UTC), sid=seeded["snapshot_id"])
        )
        await session.commit()
    card = await MacroEvidenceService(env["sessionmaker"]).create_macro_card(
        MacroEvidenceDraft(
            company_id=env["company_id"],
            research_question=QUESTION,
            macro_observation_id=obs.observation_id,
            evidence_statement="中国人口总量宏观数据。",
            extractor_name="preflight-curator",
            extractor_version=1,
            extractor_model_id="curated",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    return {"evidence_card_id": card.evidence_card_id, "observation_id": obs.observation_id}


async def _revenue_calc(env: dict) -> dict:
    """2023/2022 营收观察 + absolute_change_cny calculation（prep 的 financial need）。"""
    cur = await _seed_financial_observation(
        env,
        env["company_id"],
        metric_code="revenue",
        period_end=date(2023, 12, 31),
        source_value_text="150560000000",
        raw_unit="yuan",
        statement="营业收入1505.60亿元，同比增长18.04%",
    )
    base = await _seed_financial_observation(
        env,
        env["company_id"],
        metric_code="revenue",
        period_end=date(2022, 12, 31),
        source_value_text="127554000000",
        raw_unit="yuan",
        statement="营业收入1275.54亿元",
    )
    result = await FinancialCalculationService(env["sessionmaker"]).create_calculation(
        FinancialCalculationDraft(
            company_id=env["company_id"],
            calculation_code=CalculationCode.ABSOLUTE_CHANGE_CNY,
            input_observation_ids={
                InputRole.CURRENT: cur["metric_observation_id"],
                InputRole.BASELINE: base["metric_observation_id"],
            },
        )
    )
    return {"calculation_id": result.calculation_id}


async def _valuation_comparison(env: dict, peers: list[dict]) -> dict:
    """target + peers PE_TTM observations（卡）+ comparison。"""
    target_pe = await _seed_valuation_observation(env, env["company_id"], value_text="21")
    peer_ids = []
    for peer in peers:
        obs = await _seed_valuation_observation(
            env, peer["company_id"], value_text=PEER_PE_BY_CODE[peer["security_code"]]
        )
        peer_ids.append(obs["valuation_observation_id"])
    comparison = await RelativeValuationComparisonService(env["sessionmaker"]).create_comparison(
        ComparisonDraft(
            target_company_id=env["company_id"],
            target_observation_id=target_pe["valuation_observation_id"],
            peer_observation_ids=tuple(peer_ids),
            analysis_as_of=AS_OF,
        )
    )
    return {"comparison_id": comparison.comparison_id}


async def _extract_annual_card(env: dict, source_id, settings) -> dict:
    """补料：真实链 parse → chunk → index（生产 BGE + 共享 collection）→ 真实
    extractor 提取 annual_report 证据卡。"""
    from app.services.evidence_extraction_service import EvidenceExtractionService

    from app.llm.factory import create_evidence_extraction_model
    from app.rag.embedding.bge import BGEProvider
    from app.rag.index.service import VectorIndexService
    from app.rag.retrieval.contracts import RetrievalQuery
    from app.rag.retrieval.service import RetrievalService
    from app.services.chunking_service import ChunkingService
    from app.services.source_parsing_service import SourceParsingService
    from app.vectorstore.client import ChromaManager

    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(
        source_id
    )
    chunked = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(
        parsed.parsed_source_id
    )
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    embedding = BGEProvider()
    index = VectorIndexService(env["sessionmaker"], embedding, chroma)
    await index.index_chunk_set(chunked.chunk_set_id, force_rebuild=True)
    retrieval = RetrievalService(env["sessionmaker"], embedding, chroma)
    hits = await retrieval.retrieve(
        RetrievalQuery(
            company_id=env["company_id"],
            query_text=QUESTION,
            top_k=1,
            source_ids=[source_id],
        )
    )
    if not hits:
        raise RuntimeError("annual_report 检索未命中")
    extraction = await EvidenceExtractionService(
        env["sessionmaker"],
        create_evidence_extraction_model(settings),
    ).extract_from_hit(QUESTION, hits[0])
    if not extraction.relevant or not extraction.evidence_card_ids:
        raise RuntimeError("annual_report 证据提取未命中（real extractor not_relevant）")
    return list(extraction.evidence_card_ids)


# ------------------------------------------------------------------ orchestration helpers


async def _orchestration_row(sessionmaker, orchestration_id: UUID) -> dict:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT orchestration_id, task_id, research_plan_id, attempt_no, status, "
                        "current_phase, error_code FROM research_orchestration_runs "
                        "WHERE orchestration_id = :oid"
                    ).bindparams(oid=orchestration_id)
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise RuntimeError(f"orchestration row missing: {orchestration_id}")
    return dict(row)


async def _wait_for(
    sessionmaker,
    orchestration_id: UUID,
    predicate,
    *,
    timeout_seconds: int,
    message: str,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = await _orchestration_row(sessionmaker, orchestration_id)
        if predicate(last):
            return last
        await asyncio.sleep(3)
    raise TimeoutError(f"{message}（timeout={timeout_seconds}s，最后状态={last}）")


def _phase_label(row: dict) -> str:
    return f"{row['status']}/{row['current_phase'] or '-'}"


async def _count(sessionmaker, table: str) -> int:
    async with sessionmaker() as session:
        value = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return int(value)


# ------------------------------------------------------------------ verification


async def _verify_final_chain(
    sessionmaker, task_id: UUID, orchestration_id: UUID, company_id: UUID
) -> dict:
    """最终链路验证：orchestration 终态 + 同线程恢复 + report/audit/citation 闭合。"""
    checks: dict[str, object] = {}
    row = await _orchestration_row(sessionmaker, orchestration_id)
    checks["orchestration_status"] = row["status"]
    checks["orchestration_phase"] = row["current_phase"]
    checks["orchestration_error_code"] = row["error_code"]
    checks["orchestration_attempt_no"] = row["attempt_no"]
    checks["single_orchestration"] = await _count(sessionmaker, "research_orchestration_runs")

    async with sessionmaker() as session:
        child_rows = (
            (
                await session.execute(
                    text(
                        "SELECT stage, attempt_no, workflow_run_id FROM "
                        "research_orchestration_child_runs WHERE orchestration_id = :oid "
                        "ORDER BY stage, attempt_no"
                    ).bindparams(oid=orchestration_id)
                )
            )
            .mappings()
            .all()
        )
    checks["child_runs"] = [dict(r) for r in child_rows]
    # 人工 action（resume / approve）后必须**同一 run 链**恢复：stage4/stage5 各
    # 恰好 1 个 child（attempt 1），不新建 run（spec N：child thread_id=run_id，
    # 顶层 orchestration 用自己的 checkpoint thread；"同线程" = child run 不换）。
    by_stage: dict[str, list[int]] = {}
    for child in child_rows:
        by_stage.setdefault(child["stage"], []).append(child["attempt_no"])
    checks["child_runs_by_stage"] = by_stage
    checks["resume_same_run"] = by_stage.get("stage4") == [1] and by_stage.get("stage5") == [1]
    async with sessionmaker() as session:
        run_rows = (
            (
                await session.execute(
                    text(
                        "SELECT graph_name, status, thread_id FROM workflow_runs "
                        "WHERE task_id = :tid ORDER BY graph_name"
                    ).bindparams(tid=task_id)
                )
            )
            .mappings()
            .all()
        )
    checks["workflow_runs"] = [dict(r) for r in run_rows]
    checks["all_children_completed"] = all(r["status"] == "completed" for r in run_rows)
    checks["child_thread_ids"] = sorted({r["thread_id"] for r in run_rows})

    checks["claim_count"] = await _count(sessionmaker, "claims")
    checks["synthesis"] = (
        await _count(sessionmaker, "claim_synthesis_runs"),
        await _count(sessionmaker, "claim_synthesis_results"),
    )
    checks["report_count"] = await _count(sessionmaker, "reports")
    checks["audit_count"] = await _count(sessionmaker, "report_audits")
    checks["evidence_count"] = await _count(sessionmaker, "evidence_cards")

    async with sessionmaker() as session:
        report_rows = (
            (
                await session.execute(
                    text(
                        "SELECT r.report_id, r.report_payload FROM reports r "
                        "JOIN report_outlines o ON o.outline_id = r.outline_id "
                        "JOIN claim_synthesis_results sr "
                        "ON sr.synthesis_result_id = o.synthesis_result_id "
                        "JOIN claim_synthesis_runs s ON s.synthesis_id = sr.synthesis_id "
                        "WHERE s.company_id = :cid"
                    ).bindparams(cid=company_id)
                )
            )
            .mappings()
            .all()
        )
    paragraph_claims: set[str] = set()
    paragraph_evidence: set[str] = set()
    for report in report_rows:
        payload = report["report_payload"]
        for section in payload.get("sections", []):
            for paragraph in section.get("paragraphs", []):
                paragraph_claims.update(paragraph.get("claim_ids", []))
                paragraph_evidence.update(paragraph.get("evidence_card_ids", []))
    async with sessionmaker() as session:
        claims_rows = (
            (
                await session.execute(
                    text("SELECT claim_id FROM claims WHERE claim_id = ANY(:ids)").bindparams(
                        ids=[UUID(c) for c in paragraph_claims]
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence_rows = (
            (
                await session.execute(
                    text(
                        "SELECT evidence_card_id, company_id, source_id, "
                        "quote_text IS NOT NULL AS has_quote "
                        "FROM evidence_cards WHERE evidence_card_id = ANY(:ids)"
                    ).bindparams(ids=[UUID(c) for c in paragraph_evidence])
                )
            )
            .mappings()
            .all()
        )
    checks["paragraph_claim_ids"] = sorted(paragraph_claims)
    checks["paragraph_evidence_ids"] = sorted(paragraph_evidence)
    checks["paragraph_claims_resolved"] = len(claims_rows) == len(paragraph_claims)
    checks["paragraph_evidence_resolved"] = len(evidence_rows) == len(paragraph_evidence)
    checks["paragraph_evidence_detail"] = [dict(r) for r in evidence_rows]

    async with sessionmaker() as session:
        links = (
            (
                await session.execute(
                    text(
                        "SELECT claim_id, evidence_card_id FROM claim_evidence_links "
                        "WHERE claim_id = ANY(:ids)"
                    ).bindparams(ids=[UUID(c) for c in paragraph_claims])
                )
            )
            .mappings()
            .all()
        )
    checks["claim_evidence_links"] = len(links)
    checks["claims_have_evidence_links"] = (
        all(any(str(link["claim_id"]) == c for link in links) for c in paragraph_claims)
        if paragraph_claims
        else False
    )
    return checks


# ------------------------------------------------------------------ main


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()

    def _needs_real_llm() -> bool:
        """real-planner 或任一组件模型为 real 时需要真实 key。"""
        if args.mode == "real-planner":
            return True
        return (
            any(
                getattr(args, name, "fake") == "real"
                for name in (
                    "financial_model",
                    "draft_model",
                    "claim_model",
                    "extractor_model",
                    "macro_model",
                    "synthesis_model",
                )
            )
            or args.audit_model == "real"
        )

    if _needs_real_llm() and (
        settings.deepseek_api_key is None or not settings.deepseek_api_key.get_secret_value()
    ):
        print(
            json.dumps(
                {"ok": False, "blocker": "DEEPSEEK_API_KEY 未配置（全受控模式可离线运行）"},
                ensure_ascii=False,
            )
        )
        return 3

    manager = DatabaseManager(
        database_url=settings.database_url, echo=False, connect_timeout_seconds=5
    )
    sessionmaker = manager.session_factory()
    raw_root = Path(settings.raw_storage_root) / "preflight_golden"
    raw_store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024 * 100)
    checkpoint = LangGraphCheckpointManager(to_postgres_connection_uri(settings.database_url))
    await checkpoint.setup()
    harness = None
    timeline: list[dict] = []
    try:
        await _cleanup(sessionmaker)
        await SourceRegistryService(sessionmaker).seed_defaults()
        await _seed_world_bank_provider(sessionmaker)

        company = await _seed_company(sessionmaker, "600519")
        peers = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
        task_id = await _seed_task(sessionmaker, "SSE:600519")
        env = {
            "sessionmaker": sessionmaker,
            "raw_store": raw_store,
            "company_id": company["company_id"],
            "target_company_id": company["company_id"],
            "peer_company_ids": peers,
        }
        _log(f"mode={args.mode} task={task_id} company={company['company_id']}")

        # 文档证据卡（business / risk 输入）。
        await _document_card(
            env,
            statement=(
                "贵州茅台是A股白酒行业龙头企业，2023年营业收入1505.60亿元，"
                "归母净利润747.34亿元，均保持增长，直销占比持续提升。"
            ),
            source_url="https://www.xinhuanet.com/2025/0601/0001.htm",
        )
        await _document_card(
            env,
            statement=(
                "白酒行业竞争加剧，高端产品收入占比提升，渠道库存合理，公司毛利率保持高位运行。"
            ),
            source_url="https://www.xinhuanet.com/2025/0602/0001.htm",
        )
        # financial（observation + calculation）、macro（snapshot + 卡）、valuation。
        await _revenue_calc(env)
        await _macro_card(env)
        await _valuation_comparison(env, peers)
        _log("seed done（缺 annual_report，预置 waiting_manual 人工环节）")

        # 生产装配（真实 DeepSeek + 真实 BGE + 真实 Chroma 共享 collection）。
        collector = EvalLlmUsageCollector(
            execution_spec_fingerprint="0" * 64,
            variant_id=EvalVariantId.INSIGHTFORGE_FULL,
            case_id=COLLECTOR_CASE_ID,
        )
        if args.mode == "controlled-plan":
            # planner 输出受控（payload 与 seed 匹配 + annual_report 缺失）；
            # 其余（fulfillment / extractor / stage4 / stage5 / audit）全部真实。
            plan_service = ResearchPlanningService(
                sessionmaker,
                FakeResearchPlannerModel(payload=_CONTROLLED_PLAN),
                CompanyIdentityService(sessionmaker),
            )
            router = ResearchSourceRouter(sessionmaker, plan_service)
            preparation = ResearchPreparationService(sessionmaker, plan_service, router)
            fulfillment = _make_fulfillment(
                settings,
                sessionmaker,
                collector,
                plan_service,
                router,
                preparation,
                extractor_model=args.extractor_model,
            )
            deps = _make_orchestration_deps(
                settings,
                sessionmaker,
                checkpoint,
                collector,
                plan_service,
                router,
                preparation,
                fulfillment,
                financial_model=args.financial_model,
                draft_model=args.draft_model,
                claim_model=args.claim_model,
                macro_model=args.macro_model,
                synthesis_model=args.synthesis_model,
                audit_model=args.audit_model,
            )
        else:
            deps = create_research_orchestration_dependencies(
                settings, sessionmaker, checkpoint, usage_observer=collector
            )
        runner = ResearchOrchestrationRunner(sessionmaker, checkpoint, deps)
        exec_manager = ResearchOrchestrationExecutionManager(runner)
        harness = exec_manager
        service = ResearchOrchestrationService(
            sessionmaker,
            deps.plan_service,
            stage5_runner=deps.stage5_runner,
            orchestration_runner=runner,
            execution_manager=exec_manager,
        )
        _log(
            f"deps wired（model policy {settings.llm_provider}:{settings.llm_model}，"
            f"planner={'real' if args.mode == 'real-planner' else 'controlled'}）"
        )

        t0 = time.monotonic()
        outcome = await service.prepare_orchestration_start(task_id)
        o1 = outcome.orchestration.orchestration_id
        timeline.append({"event": "orchestration_created", "orchestration_id": str(o1)})
        _log(f"orchestration={o1} scheduled={outcome.scheduled}")

        # ---- 阶段 1：等 waiting_human / terminal ----
        try:
            row = await _wait_for(
                sessionmaker,
                o1,
                lambda r: r["status"] in ("waiting_human", "completed", "failed"),
                timeout_seconds=args.timeout,
                message="等待 waiting_human / terminal",
            )
        except TimeoutError as exc:
            print(
                json.dumps(
                    {"ok": False, "blocker": str(exc), "timeline": timeline},
                    ensure_ascii=False,
                )
            )
            return 3
        timeline.append({"event": "phase1_terminal", "state": _phase_label(row)})
        _log(f"phase1: {_phase_label(row)}")

        # ---- 阶段 2：人工闭环循环 ----
        rounds = 0
        while row["status"] == OrchestrationStatus.WAITING_HUMAN.value and rounds < 6:
            rounds += 1
            phase = row["current_phase"]
            while exec_manager.is_scheduled(o1):
                await asyncio.sleep(1)
            projection = await service.get_orchestration(o1)
            _log(
                f"round{rounds}: {_phase_label(row)} "
                f"missing={projection.missing_need_codes} "
                f"manual_reason={projection.manual_reason}"
            )
            if phase == OrchestrationPhase.WAITING_MANUAL.value:
                _log(f"round{rounds}: waiting_manual → 人工补资料（真实 PDF 上传 + 提取卡）")
                period_year = 2023
                for code in projection.missing_need_codes or []:
                    match = re.search(r"annual_report[_\s]?(\d{4})", code)
                    if match:
                        period_year = int(match.group(1))
                        break
                pdf_bytes = single_page_pdf(title=f"贵州茅台{period_year}年年度报告")
                upload = await SourceIngestionService(sessionmaker, raw_store).ingest_upload(
                    company_id=company["company_id"],
                    provider_key="sse",
                    document_type=SourceDocumentType.ANNUAL_REPORT,
                    title=f"贵州茅台{period_year}年年度报告",
                    source_url="https://static.sse.com.cn/disclosure/listedinfo/announcement",
                    published_at=datetime(period_year + 1, 4, 30, tzinfo=UTC),
                    reporting_period_end=date(period_year, 12, 31),
                    external_document_id=None,
                    stream=BytesIO(pdf_bytes),
                )
                timeline.append(
                    {
                        "event": "human_upload",
                        "source_id": str(upload.record.source_id),
                        "replayed": upload.replayed,
                    }
                )
                _log(f"uploaded source={upload.record.source_id} replayed={upload.replayed}")
                # 补料需要证据卡（prep readiness）：真实链提取。
                try:
                    card_ids = await _extract_annual_card(env, upload.record.source_id, settings)
                    timeline.append({"event": "human_extract", "card_ids": card_ids})
                    _log(f"annual evidence cards={card_ids}")
                except RuntimeError as exc:
                    timeline.append({"event": "human_extract_failed", "detail": str(exc)})
                    _log(f"annual extract failed: {exc}")
                await service.resume_after_source_acquisition(o1)
                timeline.append({"event": "human_action_resume", "kind": "prepare"})
            elif phase == OrchestrationPhase.AWAITING_STAGE5.value:
                _log(f"round{rounds}: awaiting_stage5 → 人工 approve")
                await service.act_on_orchestration(
                    o1, "approve", comment="Golden preflight 人工确认"
                )
                timeline.append({"event": "human_action_approve"})
            elif phase == OrchestrationPhase.RESEARCH_BACKFLOW.value:
                _log(f"round{rounds}: research_backflow manual → resume（补资料语义）")
                await service.resume_after_source_acquisition(o1)
                timeline.append({"event": "human_action_resume", "kind": "supplemental_research"})
            else:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "blocker": f"未知 waiting_human phase: {phase}",
                            "timeline": timeline,
                        },
                        ensure_ascii=False,
                    )
                )
                return 3
            try:
                row = await _wait_for(
                    sessionmaker,
                    o1,
                    lambda r: r["status"] in ("waiting_human", "completed", "failed"),
                    timeout_seconds=args.timeout,
                    message=f"等待 round{rounds} 后 terminal",
                )
            except TimeoutError as exc:
                print(
                    json.dumps(
                        {"ok": False, "blocker": str(exc), "timeline": timeline},
                        ensure_ascii=False,
                    )
                )
                return 3
            timeline.append({"event": f"round{rounds}_after", "state": _phase_label(row)})
            _log(f"round{rounds} after: {_phase_label(row)}")

        if row["status"] == OrchestrationStatus.FAILED.value:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "blocker": f"orchestration failed: {row['error_code']}",
                        "timeline": timeline,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        if row["status"] != OrchestrationStatus.COMPLETED.value:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "blocker": f"未达 completed: {_phase_label(row)}",
                        "timeline": timeline,
                    },
                    ensure_ascii=False,
                )
            )
            return 2

        elapsed = time.monotonic() - t0
        checks = await _verify_final_chain(sessionmaker, task_id, o1, company["company_id"])
        per_component: dict[str, dict] = {}
        for r in collector.records():
            entry = per_component.setdefault(
                r.component_name,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
            entry["calls"] += 1
            entry["input_tokens"] += r.input_tokens or 0
            entry["output_tokens"] += r.output_tokens or 0
            entry["total_tokens"] += r.total_tokens or 0

        required_checks = [
            ("orchestration_status", "completed"),
            ("orchestration_attempt_no", 1),
            ("all_children_completed", True),
            ("resume_same_run", True),
            ("report_count", 1),
            ("audit_count", 1),
            ("paragraph_claims_resolved", True),
            ("paragraph_evidence_resolved", True),
            ("claims_have_evidence_links", True),
        ]
        failures = [name for name, expected in required_checks if checks.get(name) != expected]
        ok = not failures
        payload = {
            "ok": ok,
            "mode": args.mode,
            "model": f"{settings.llm_provider}:{settings.llm_model}",
            "question": QUESTION,
            "elapsed_seconds": round(elapsed, 1),
            "timeline": timeline,
            "checks": checks,
            "usage_per_component": per_component,
            "usage_total_tokens": sum(r.total_tokens or 0 for r in collector.records()),
            "usage_call_count": len(collector.records()),
            "failed_checks": failures,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if ok else 2
    finally:
        if harness is not None:
            await harness.close()
        await checkpoint.close()
        await manager.dispose()
        if not args.keep:
            try:
                await _cleanup(sessionmaker)
            except Exception:  # noqa: BLE001 — 清理失败不影响结果
                pass


def _make_fulfillment(
    settings,
    sessionmaker,
    collector,
    plan_service,
    router,
    preparation,
    *,
    extractor_model: str = "real",
):
    """受控模式：真实 fulfillment（复用注入的 plan_service/router/preparation）；
    `extractor_model="fake"` 仅替换 evidence extractor（真实 extractor 输出不可控
    ——evidence_statement 曾含 alias → draft InlineAliasLeak / check 失败，记录为
    真实剩余问题）。"""
    from app.llm.factory import create_evidence_extraction_model
    from app.rag.embedding.bge import BGEProvider
    from app.rag.index.service import VectorIndexService
    from app.rag.retrieval.service import RetrievalService
    from app.research_fulfillment.executors import (
        DocumentNeedExecutor,
        FinancialNeedExecutor,
        MacroNeedExecutor,
        SourceIndexBuilder,
        ValuationNeedExecutor,
    )
    from app.research_fulfillment.service import ResearchFulfillmentService
    from app.services.chunking_service import ChunkingService
    from app.vectorstore.client import ChromaManager

    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    embedding = BGEProvider()
    retrieval = RetrievalService(
        sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma
    )
    index_builder = SourceIndexBuilder(
        sessionmaker,
        ChunkingService(sessionmaker),
        VectorIndexService(sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma),
    )
    if extractor_model == "fake":
        from app.eval.benchmark.fakes import _E2eEvidenceModel

        extractor = _E2eEvidenceModel(
            observer=collector, provider="deepseek", model_id=settings.llm_model
        )
    else:
        extractor = create_evidence_extraction_model(settings, usage_observer=collector)
    document_executor = DocumentNeedExecutor(
        sessionmaker,
        retrieval,
        extractor,
        index_builder=index_builder,
    )
    return ResearchFulfillmentService(
        sessionmaker,
        plan_service,
        router,
        preparation,
        document_executor=document_executor,
        financial_executor=FinancialNeedExecutor(sessionmaker),
        macro_executor=MacroNeedExecutor(sessionmaker),
        valuation_executor=ValuationNeedExecutor(),
    )


def _stage4_deps(
    settings,
    sessionmaker,
    collector,
    *,
    financial_model: str,
    claim_model: str,
    macro_model: str,
    synthesis_model: str,
):
    """Stage4 deps：默认全真实；`financial_model` / `claim_model` / `macro_model`
    / `synthesis_model="fake"` 隔离对应 analyst。

    真实模型在严格结构化 policy 下已观测到的稳定冲突（preflight 记录为真实
    剩余问题，fake 仅用于隔离验证编排/人工/链路本身）：
    - financial analyst：statement 含数字字面量 →
      `FinancialAnalysisNumericLiteralForbidden`；
    - claim analyst（business/risk）：statement 内联 alias →
      draft 逐字复制后触发 `DraftSectionInlineAliasLeak`；
    - macro analyst：statement 含数字 → macro 卡（quote 恒 NULL）无法 grounding
      → `DraftSectionNumericGroundingError`；
    - synthesis analyst：summary / theme 自由文本内联 C-alias（"C1/C2/..."）→
      report 文本含 alias → `DraftSectionInlineAliasLeak`。
    """
    from app.analysis.claims.factory import create_claim_analysis_model
    from app.analysis.claims.service import ClaimAnalysisService
    from app.analysis.financial.factory import create_financial_analysis_model
    from app.analysis.financial.service import FinancialAnalysisService
    from app.analysis.macro.factory import create_macro_analysis_model
    from app.analysis.macro.service import MacroAnalysisService
    from app.analysis.synthesis.factory import create_synthesis_analysis_model
    from app.analysis.synthesis.service import SynthesisAnalysisService
    from app.analysis.valuation.factory import create_valuation_analysis_model
    from app.analysis.valuation.service import ValuationAnalysisService
    from app.stage4.dependencies import Stage4AnalysisDependencies
    from app.synthesis.service import SynthesisService
    from tests.analysis.claims.fakes import FakeClaimAnalysisModel
    from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
    from tests.analysis.macro.fakes import FakeMacroAnalysisModel
    from tests.integration.test_stage4_workflow import (
        _claim_decision,
        _financial_decision,
        _macro_decision,
    )

    if claim_model == "fake":
        claim_service = ClaimAnalysisService(
            sessionmaker, FakeClaimAnalysisModel(decision=_claim_decision())
        )
    else:
        claim_service = ClaimAnalysisService(
            sessionmaker, create_claim_analysis_model(settings, usage_observer=collector)
        )
    if financial_model == "fake":
        financial_service = FinancialAnalysisService(
            sessionmaker, FakeFinancialAnalysisModel(decision=_financial_decision())
        )
    else:
        financial_service = FinancialAnalysisService(
            sessionmaker, create_financial_analysis_model(settings, usage_observer=collector)
        )
    if macro_model == "fake":
        macro_service = MacroAnalysisService(
            sessionmaker, FakeMacroAnalysisModel(decision=_macro_decision())
        )
    else:
        macro_service = MacroAnalysisService(
            sessionmaker, create_macro_analysis_model(settings, usage_observer=collector)
        )
    if synthesis_model == "fake":
        from tests.analysis.synthesis.fakes import FakeSynthesisAnalysisModel
        from tests.integration.test_stage4_workflow import _synthesis_output

        synthesis_service = SynthesisAnalysisService(
            sessionmaker, FakeSynthesisAnalysisModel(output=_synthesis_output(5))
        )
    else:
        synthesis_service = SynthesisAnalysisService(
            sessionmaker, create_synthesis_analysis_model(settings, usage_observer=collector)
        )
    return Stage4AnalysisDependencies(
        sessionmaker=sessionmaker,
        claim_analysis_service=claim_service,
        financial_analysis_service=financial_service,
        macro_analysis_service=macro_service,
        valuation_analysis_service=ValuationAnalysisService(
            sessionmaker, create_valuation_analysis_model(settings, usage_observer=collector)
        ),
        synthesis_service=SynthesisService(sessionmaker),
        synthesis_analysis_service=synthesis_service,
    )


def _stage5_deps(settings, sessionmaker, collector, *, draft_model: str, audit_model: str):
    """Stage5 deps：默认全真实；`draft_model="fake"` 仅替换 draft writer；
    `audit_model="human-review"` 固定 human_review 判定（验证人工确认机制）。

    真实模型在严格结构化 policy 下已观测到的稳定冲突（preflight 记录为真实
    剩余问题，fake 仅用于隔离验证编排/人工/链路本身）：
    - draft writer：输出含未被引用的数字 →
      `DraftSectionNumericGroundingError`；
    - audit（真实判定不可控）：已观测 human_review（→ awaiting_stage5 闭环）与
      research_backflow + structured_data_refresh_required（→ D2 政策正确拒绝
      文档补料恢复）。
    """
    from app.audit.adapters import DeepSeekAuditModel
    from app.audit.service import ReportAuditService
    from app.draft_section.service import DraftSectionService
    from app.report.check_service import ReportCheckService
    from app.report.service import ReportService
    from app.report_outline.service import ReportOutlineService
    from app.research_backflow.service import ResearchBackflowService
    from app.review.service import ReviewActionService
    from app.revision.factory import create_revision_writer_model
    from app.revision.service import RevisionService
    from app.stage5.dependencies import Stage5WorkflowDependencies
    from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for

    if draft_model == "fake":
        draft_section_service = DraftSectionService(
            sessionmaker, FakeDraftSectionModel(decision_factory=valid_decision_for)
        )
    else:
        from app.draft_section.factory import create_draft_section_model

        draft_section_service = DraftSectionService(
            sessionmaker, create_draft_section_model(settings, usage_observer=collector)
        )
    outline_service = ReportOutlineService(sessionmaker)
    report_service = ReportService(sessionmaker, draft_section_service)
    check_service = ReportCheckService(sessionmaker, report_service)
    if audit_model == "human-review":
        # 受控 audit：固定 human_review 判定 → awaiting_stage5 → 人工 approve
        # （验证人工确认机制本身；真实 audit 判定不可控——已观测 human_review /
        # research_backflow / 见 preflight 记录）。
        from tests.audit.fakes import FakeAuditModel
        from tests.integration.test_report_audit_service import human_review_decision

        audit_service = ReportAuditService(
            sessionmaker,
            FakeAuditModel(decision_factory=human_review_decision),
            check_service,
        )
    else:
        audit_service = ReportAuditService(
            sessionmaker, DeepSeekAuditModel(settings, usage_observer=collector), check_service
        )
    review_action_service = ReviewActionService(sessionmaker, audit_service)
    revision_service = RevisionService(
        sessionmaker,
        model=create_revision_writer_model(settings, usage_observer=collector),
        draft_section_service=draft_section_service,
        check_service=check_service,
        review_action_service=review_action_service,
    )
    report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环
    research_backflow_service = ResearchBackflowService(
        sessionmaker, review_action_service, report_service
    )
    return Stage5WorkflowDependencies(
        sessionmaker=sessionmaker,
        report_outline_service=outline_service,
        draft_section_service=draft_section_service,
        report_service=report_service,
        report_check_service=check_service,
        report_audit_service=audit_service,
        review_action_service=review_action_service,
        revision_service=revision_service,
        research_backflow_service=research_backflow_service,
    )


def _make_orchestration_deps(
    settings,
    sessionmaker,
    checkpoint,
    collector,
    plan_service,
    router,
    preparation,
    fulfillment,
    *,
    financial_model: str = "real",
    draft_model: str = "real",
    claim_model: str = "real",
    macro_model: str = "real",
    synthesis_model: str = "real",
    audit_model: str = "real",
):
    """受控模式：真实 stage4/stage5/backflow + 受控 plan_service/router/preparation。

    `financial_model` / `draft_model` / `claim_model` / `macro_model` /
    `synthesis_model="fake"` 与 `audit_model="human-review"`：仅对应组件受控
    （见 `_stage4_deps` / `_stage5_deps`）。
    """
    from app.rag.embedding.bge import BGEProvider
    from app.rag.retrieval.service import RetrievalService
    from app.research_backflow.executor import ResearchBackflowExecutor
    from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
    from app.research_orchestration.service import ResearchOrchestrationChildService
    from app.stage4.runner import Stage4WorkflowRunner
    from app.stage5.runner import Stage5WorkflowRunner
    from app.synthesis.service import SynthesisService
    from app.vectorstore.client import ChromaManager

    stage4_runner = Stage4WorkflowRunner(
        sessionmaker,
        checkpoint,
        _stage4_deps(
            settings,
            sessionmaker,
            collector,
            financial_model=financial_model,
            claim_model=claim_model,
            macro_model=macro_model,
            synthesis_model=synthesis_model,
        ),
    )
    stage5_runner = Stage5WorkflowRunner(
        sessionmaker,
        checkpoint,
        _stage5_deps(
            settings,
            sessionmaker,
            collector,
            draft_model=draft_model,
            audit_model=audit_model,
        ),
    )
    child_service = ResearchOrchestrationChildService(sessionmaker, stage4_runner, stage5_runner)
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    embedding = BGEProvider()
    retrieval = RetrievalService(
        sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma
    )
    index_builder = _make_index_builder(sessionmaker, embedding, chroma)
    from app.llm.factory import create_evidence_extraction_model

    backflow_executor = ResearchBackflowExecutor(
        sessionmaker,
        retrieval,
        create_evidence_extraction_model(settings, usage_observer=collector),
        index_builder=index_builder,
    )
    return ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=plan_service,
        router=router,
        preparation=preparation,
        fulfillment=fulfillment,
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
        backflow_service=stage5_runner.dependencies.research_backflow_service,
        backflow_executor=backflow_executor,
    )


def _make_index_builder(sessionmaker, embedding, chroma):
    from app.rag.index.service import VectorIndexService
    from app.research_fulfillment.executors import SourceIndexBuilder
    from app.services.chunking_service import ChunkingService

    return SourceIndexBuilder(
        sessionmaker,
        ChunkingService(sessionmaker),
        VectorIndexService(sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden INSIGHTFORGE_FULL real-model preflight")
    parser.add_argument(
        "--mode",
        choices=("real-planner", "controlled-plan"),
        default="real-planner",
        help=(
            "real-planner=真实 planner（机制验证）；"
            "controlled-plan=受控 plan + 其余真实（全链路闭环）"
        ),
    )
    parser.add_argument(
        "--financial-model",
        choices=("real", "fake"),
        default="real",
        help=(
            "controlled-plan 模式下 financial analyst 的模型：real=真实（默认，"
            "可能触发 numeric-literal policy 拒绝——记录真实剩余问题）；"
            "fake=固定决策（仅隔离验证编排/人工/链路）"
        ),
    )
    parser.add_argument(
        "--draft-model",
        choices=("real", "fake"),
        default="real",
        help=(
            "controlled-plan 模式下 draft writer 的模型：real=真实（默认，可能触发"
            "numeric-grounding policy 拒绝——记录真实剩余问题）；fake=固定决策"
        ),
    )
    parser.add_argument(
        "--claim-model",
        choices=("real", "fake"),
        default="real",
        help=(
            "controlled-plan 模式下 business/risk claim analyst 的模型：real=真实"
            "（默认，statement 可能内联 alias → draft InlineAliasLeak——记录真实"
            "剩余问题）；fake=固定决策"
        ),
    )
    parser.add_argument(
        "--extractor-model",
        choices=("real", "fake"),
        default="real",
        help=(
            "controlled-plan 模式下 evidence extractor 的模型：real=真实（默认，"
            "输出不可控——evidence_statement 曾含 alias → draft InlineAliasLeak/"
            "check 失败，记录真实剩余问题）；fake=固定输出"
        ),
    )
    parser.add_argument(
        "--macro-model",
        choices=("real", "fake"),
        default="real",
        help=(
            "controlled-plan 模式下 macro analyst 的模型：real=真实（默认，"
            "statement 含数字 → macro 卡无 quote 无法 grounding → draft "
            "NumericGroundingError——记录真实剩余问题）；fake=固定决策"
        ),
    )
    parser.add_argument(
        "--synthesis-model",
        choices=("real", "fake"),
        default="real",
        help=(
            "controlled-plan 模式下 synthesis analyst 的模型：real=真实（默认，"
            "summary/theme 自由文本内联 C-alias → draft InlineAliasLeak——记录真实"
            "剩余问题）；fake=固定输出"
        ),
    )
    parser.add_argument(
        "--audit-model",
        choices=("real", "human-review"),
        default="real",
        help=(
            "controlled-plan 模式下 audit 的模型：real=真实（默认，判定不可控——"
            "已观测 human_review / research_backflow structured refresh）；"
            "human-review=固定 human_review 判定（验证人工 approve 闭环）"
        ),
    )
    parser.add_argument("--keep", action="store_true", help="保留共享 PG 数据与产物")
    parser.add_argument("--timeout", type=int, default=900, help="每阶段等待超时（秒）")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
