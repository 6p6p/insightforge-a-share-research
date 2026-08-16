"""Financial auto extraction integration tests (F1 Financial Intelligence).

真实 PostgreSQL：seed 公司 + annual report source（真实 parse）→
StatementLineExtractionProvider → FinancialExtractionService（provenance
校验）→ FinancialExtractionEvidenceService + FinancialMetricService（卡 +
observation 落库）→ FinancialNeedExecutor 缺 observation 自动提取 →
确定性计算 RESOLVED。

全程 0 LLM / 0 Web；数字全部来自 seeded 行文本 token。
"""

from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.financial.extraction.contracts import FinancialExtractionRequest
from app.financial.extraction.evidence import FinancialExtractionEvidenceService
from app.financial.extraction.ingestion import FinancialExtractionIngestionService
from app.financial.extraction.service import FinancialExtractionService
from app.financial.extraction.statement_provider import StatementLineExtractionProvider
from app.research_fulfillment.contracts import FulfillmentStatus
from app.research_fulfillment.executors import FinancialNeedExecutor
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.research_fulfillment_helpers import _make_context, _make_entry, _make_need
from tests.integration.test_research_planning_service import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


_STATEMENT_LINES = [
    "营业收入 45,678,901.23 43,210,987.65",
    "营业成本 30,000,000.00 28,000,000.00",
    "归属于上市公司股东的净利润 8,000,000.00 7,000,000.00",
    "经营活动产生的现金流量净额 9,500,000.00 8,200,000.00",
    "资产总计 100,000,000.00 90,000,000.00",
    "负债合计 40,000,000.00 38,000,000.00",
    "归属于上市公司股东的权益 60,000,000.00 52,000,000.00",
]


async def _seed_annual_report_pdf(env) -> tuple:
    """seed 真实 PDF annual report source（PDF 字节 → parse → blocks）。"""
    from app.domain.source_records import SourceDocumentType
    from app.services.source_ingestion_service import SourceIngestionService
    from app.services.source_parsing_service import SourceParsingService

    pdf_bytes = _build_statement_pdf(_STATEMENT_LINES)
    import io

    ingestion = SourceIngestionService(env["sessionmaker"], env["raw_store"])
    result = await ingestion.ingest_upload(
        company_id=env["company_id"],
        provider_key="eastmoney",
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title="2024年年度报告",
        source_url=None,
        published_at=datetime(2025, 4, 30, tzinfo=UTC),
        reporting_period_end=date(2024, 12, 31),
        external_document_id=None,
        stream=io.BytesIO(pdf_bytes),
    )
    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(
        result.record.source_id
    )
    return result.record.source_id, parsed.parsed_source_id


def _build_statement_pdf(lines: list[str]) -> bytes:
    """用 reportlab 生成确定性 PDF（含中文科目行文本；内置 CID 字体）。"""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("STSong-Light", 12)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


async def _count(env, table: str) -> int:
    async with env["sessionmaker"]() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


def _make_services(env) -> tuple:
    provider = StatementLineExtractionProvider(env["sessionmaker"])
    extraction_service = FinancialExtractionService(env["sessionmaker"], provider)
    ingestion = FinancialExtractionIngestionService(
        env["sessionmaker"],
        FinancialExtractionEvidenceService(env["sessionmaker"]),
    )
    return provider, extraction_service, ingestion


async def test_full_extraction_to_observation_chain(env) -> None:
    """提取 → 校验 → 证据卡（financial_extraction origin）→ observation 落库。"""
    source_id, parsed_id = await _seed_annual_report_pdf(env)
    provider, extraction_service, ingestion = _make_services(env)

    result = await extraction_service.extract(
        FinancialExtractionRequest(
            company_id=env["company_id"],
            parsed_source_id=parsed_id,
            reporting_period_end=date(2024, 12, 31),
        )
    )

    assert result.accepted_count >= 12  # 7 科目 × 2 期（部分可能被校验拒绝，断言 >= 10）
    summary = await ingestion.ingest(
        research_question="分析公司财务表现",
        source_id=source_id,
        extraction=result,
    )
    assert summary.cards_created > 0
    assert summary.observations_created > 0
    assert await _count(env, "evidence_cards") == summary.cards_created
    assert await _count(env, "financial_metric_observations") == summary.observations_created
    # origin 正确 + tier 继承报告来源（eastmoney Tier-3）。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT origin_type, authority_tier_snapshot, provider_key FROM evidence_cards"
                )
            )
        ).all()
    assert all(row[0] == "financial_extraction" for row in rows)
    assert all(row[1] == 3 for row in rows)
    assert all(row[2] == "eastmoney" for row in rows)


async def test_extraction_locator_carries_page_number(env) -> None:
    """P8 Evidence Locator：证据卡 locator_refs 含 block_id + 真实 page_number（PDF
    解析链产生，非 LLM 猜测）。
    """
    import json

    source_id, parsed_id = await _seed_annual_report_pdf(env)
    provider, extraction_service, ingestion = _make_services(env)
    result = await extraction_service.extract(
        FinancialExtractionRequest(
            company_id=env["company_id"],
            parsed_source_id=parsed_id,
            reporting_period_end=date(2024, 12, 31),
        )
    )
    assert result.accepted_count > 0
    summary = await ingestion.ingest(
        research_question="分析公司财务表现",
        source_id=source_id,
        extraction=result,
    )
    assert summary.cards_created > 0
    async with env["sessionmaker"]() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT locator_refs FROM evidence_cards "
                        "WHERE origin_type='financial_extraction'",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "必须有 financial_extraction 证据卡"
    locators = [json.loads(r) if isinstance(r, str) else r for r in rows]
    for locator in locators:
        assert isinstance(locator, list) and len(locator) == 1
        entry = locator[0]
        assert entry["type"] == "financial_extraction"
        assert entry["block_id"]
        # PDF 解析链真实页码（非 None）。
        assert isinstance(entry["page_number"], int) and entry["page_number"] > 0


async def test_financial_evidence_provenance_resolves_with_page(env) -> None:
    """P8：financial_extraction 卡 verified provenance——block/page/line 真实；
    quote 切片契约成立。
    """
    from sqlalchemy import select

    from app.db.models.evidence_card import EvidenceCardModel
    from app.evidence.provenance_service import EvidenceProvenanceService

    source_id, parsed_id = await _seed_annual_report_pdf(env)
    provider, extraction_service, ingestion = _make_services(env)
    result = await extraction_service.extract(
        FinancialExtractionRequest(
            company_id=env["company_id"],
            parsed_source_id=parsed_id,
            reporting_period_end=date(2024, 12, 31),
        )
    )
    summary = await ingestion.ingest(
        research_question="分析公司财务表现",
        source_id=source_id,
        extraction=result,
    )
    assert summary.cards_created > 0
    async with env["sessionmaker"]() as session:
        card = (
            (
                await session.execute(
                    select(EvidenceCardModel)
                    .where(
                        EvidenceCardModel.origin_type == "financial_extraction",
                    )
                    .limit(1),
                )
            )
            .scalars()
            .first()
        )
        assert card is not None
        prov = await EvidenceProvenanceService.resolve(session, card)

    assert prov.origin_type == "financial_extraction"
    assert prov.source_id == source_id
    assert prov.parsed_source_id == parsed_id
    assert prov.block_id is not None
    assert prov.locator is not None
    # PDF 解析链真实页码 + 行号（非 None、非 LLM 猜测）。
    assert isinstance(prov.locator.page_number, int) and prov.locator.page_number > 0
    # context 必须含 quote。
    assert prov.quote_text and prov.quote_text in prov.context_text
    assert len(prov.context_text) <= 5000
    assert prov.media_type == "application/pdf"


async def test_extraction_chain_is_idempotent(env) -> None:
    """第二次 ingest → 全部 replay（0 新增写）。"""
    source_id, parsed_id = await _seed_annual_report_pdf(env)
    _, extraction_service, ingestion = _make_services(env)
    result = await extraction_service.extract(
        FinancialExtractionRequest(
            company_id=env["company_id"],
            parsed_source_id=parsed_id,
            reporting_period_end=date(2024, 12, 31),
        )
    )
    first = await ingestion.ingest(
        research_question="分析公司财务表现",
        source_id=source_id,
        extraction=result,
    )
    second = await ingestion.ingest(
        research_question="分析公司财务表现",
        source_id=source_id,
        extraction=result,
    )
    assert second.cards_created == 0
    assert second.observations_created == 0
    assert second.cards_replayed == first.cards_created
    assert second.observations_replayed == first.observations_created


async def test_financial_executor_auto_extracts_and_resolves(env) -> None:
    """缺 observation → executor 自动提取 → 确定性计算 RESOLVED。"""
    await _seed_annual_report_pdf(env)
    _, extraction_service, ingestion = _make_services(env)
    provider = StatementLineExtractionProvider(env["sessionmaker"])
    executor = FinancialNeedExecutor(
        env["sessionmaker"],
        extraction=ingestion,
        extraction_service=extraction_service,
        provider=provider,
    )
    from app.financial.calculations.contracts import CalculationCode
    from app.financial.contracts import MetricCode
    from app.research_planning.contracts import FinancialNeed
    from tests.integration.test_research_planning_service import _plan_payload

    payload = _plan_payload()
    payload = payload.model_copy(
        update={
            "financial_needs": [
                FinancialNeed(
                    need_code="revenue_change",
                    purpose="需要营收绝对变化",
                    calculation_code=CalculationCode.ABSOLUTE_CHANGE_CNY,
                    metric_code=MetricCode.REVENUE,
                )
            ],
        }
    )
    attempt = await executor.fulfill(
        context=_make_context(env, payload=payload),
        need=_make_need("revenue_change", need_kind="financial"),
        entry=_make_entry("revenue_change", need_kind="financial", provider_keys=("eastmoney",)),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert len(attempt.created_artifact_ids) == 1
    # 提取产生的 observation 已在库（供后续 preparation 使用）。
    assert await _count(env, "financial_metric_observations") >= 2
