"""Reproducible Stage7 benchmark dataset builder (stage 7B.1.4D).

从一个**自包含 seed 源 PG**（curated 真实公开 A 股信息）materialize 出 frozen
evaluation bundle。dataset 可重复构建：同一代码 + 同一 curated 内容 → 同一
bundle（fingerprint 全确定）。

数据策略（真实公开信息，frozen as_of）：
- 贵州茅台（600519）公开年报 / 公告中的真实财务数字（营收 2023/2022、归母
  净利润 2023/2022）；
- 新华网 / 上交所风格的公开披露文本（内容 = 真实公开事实的简洁转述，标题 /
  URL 为 curated fixture 标识，`as_of=2025-08-01` 冻结）；
- 宏观：World Bank SP.POP.TOTL 中国人口（公开数据，MockTransport 离线回放）；
- 估值：PE_TTM 观测 + peer 比较（curated 公开市场数据近似值，明确标注近似）。

Cases：
- `moutai-business`（document-only，三路共享）：经营基本面问题；
- `moutai-financial`（document-only，三路共享）：财务表现问题；
- `moutai-full`（document + macro + financial + valuation）：FULL 扩展路径
  （single_rag / multi_stage_no_audit 按其 v1 契约 fail-fast）。

`build_benchmark_dataset(root)` 创建临时 seed DB（`insightforge_benchmark_seed_*`）
→ seed → materialize → 写 bundle → DROP seed DB。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
from alembic.config import Config

from alembic import command
from app.core.config import get_settings
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import StructuredArtifactType
from app.eval.materialization import (
    EvalCaseMaterializationSpec,
    EvaluationSnapshotMaterializer,
    StructuredArtifactSelection,
)
from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.financial.contracts import (
    FinancialMetricDraft,
    MetricCode,
    RawUnit,
    StatementScope,
)
from app.financial.service import FinancialMetricService
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft

BENCHMARK_DATASET_ID = "insightforge_a_share_benchmark"
BENCHMARK_DATASET_VERSION = 1
BENCHMARK_AS_OF = datetime(2025, 8, 1, tzinfo=UTC)
_AS_OF_DATE = date(2025, 8, 1)
_CASE_IDS = ("moutai-business", "moutai-financial", "moutai-full")

BACKEND_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


async def _upgrade_head() -> None:
    """临时 seed DB 需要完整 schema（alembic head）。"""
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


def _sha256_hex() -> str:
    return hashlib.sha256(uuid4().hex.encode()).hexdigest()


def _parse_db_url(url: str) -> dict:
    parsed = urlparse(url.replace("+psycopg", "", 1))
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
    }


def _admin_conn(db_name: str) -> psycopg.Connection:
    parts = _parse_db_url(get_settings().database_url)
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
    )


@asynccontextmanager
async def _temp_seed_db():
    """自包含临时 seed PG（alembic 由外部保证 head；用完 DROP）。"""
    shared = get_settings().database_url
    temp_db = f"insightforge_benchmark_seed_{uuid4().hex[:10]}"
    temp_url = shared.rsplit("/", 1)[0] + f"/{temp_db}"
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{temp_db}"')
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        yield manager.session_factory(), temp_url
    finally:
        await manager.dispose()
        with _admin_conn("postgres") as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE)')


# ------------------------------------------------------------------ seed helpers


async def _seed_company(sessionmaker, security_code: str = "600519") -> dict:
    from app.db.models.company import CompanyModel
    from app.repositories.company_repository import CompanyRepository

    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code=security_code,
                identity_key=f"SSE:{security_code}",
                board="sse_main",
                official_name=(
                    "贵州茅台酒股份有限公司"
                    if security_code == "600519"
                    else "白酒同业公司"
                ),
                short_name="贵州茅台" if security_code == "600519" else "同业公司",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    return {"company_id": company_id, "security_code": security_code}


async def _seed_document(
    env: dict,
    company_id,
    *,
    document_type: str,
    title: str,
    body_text: str,
    source_url: str,
    published_at: datetime,
    provider_key: str = "xinhuanet",
) -> dict:
    html = (
        "<html><head><title>研究材料</title></head><body><article>"
        f"<p>{body_text}</p></article></body></html>"
    ).encode()
    stored = env["raw_store"].put_html_bytes(html)
    capabilities = (
        ["news_article", "document_download"]
        if provider_key == "xinhuanet"
        else ["company_announcement", "document_download"]
    )
    async with env["sessionmaker"]() as session:
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
            provider_key=provider_key,
            artifact_id=artifact.artifact_id,
            document_type=document_type,
            title=title,
            published_at=published_at,
            reporting_period_end=None,
            source_url=source_url,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=1 if provider_key == "sse" else 3,
            critical_claim_eligible_snapshot=provider_key == "sse",
            provider_capabilities_snapshot=capabilities,
            acquired_at=published_at,
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    return {"source_id": source_id, "content_sha256": stored.content_sha256}


async def _seed_financial_observation(
    env: dict,
    company_id,
    *,
    metric_code: str,
    period_end: date,
    source_value_text: str,
    raw_unit: str,
    statement: str,
) -> dict:
    """真实 document → evidence → FinancialMetricObservation 链。"""
    year = period_end.year
    # body 必须包含 source_value_text 的完整数字 token（grammar exact-match）。
    html = (
        "<html><head><title>财务披露</title></head><body><article>"
        f"<p>{year}年财务数据显示：{statement}，披露数值为{source_value_text}元。</p>"
        "</article></body></html>"
    ).encode()
    stored = env["raw_store"].put_html_bytes(html)
    async with env["sessionmaker"]() as session:
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
            provider_key="sse",
            artifact_id=artifact.artifact_id,
            document_type="annual_report",
            title=f"{year}年年度报告",
            published_at=datetime(year + 1, 4, 30, tzinfo=UTC),
            reporting_period_end=period_end,
            source_url="https://static.sse.com.cn/disclosure/listedinfo/announcement",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=["company_announcement", "document_download"],
            acquired_at=datetime(year + 1, 4, 30, tzinfo=UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(
        source_id
    )
    chunked = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(
        parsed.parsed_source_id
    )
    from app.repositories.document_chunk_repository import DocumentChunkRepository

    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(chunked.chunk_set_id)
    chunk = chunks[0]
    quote_text = f"披露数值为{source_value_text}元"
    idx = chunk.text.index(quote_text)
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=f"{year}年公司经营与财务表现如何？",
            evidence_statement=statement,
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + len(quote_text),
            extractor_name="benchmark-curator",
            extractor_version=1,
            extractor_model_id="curated-public-data",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await FinancialMetricService(env["sessionmaker"]).create_observation(
        FinancialMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=MetricCode(metric_code),
            statement_scope=StatementScope.CONSOLIDATED,
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
            source_value_text=source_value_text,
            raw_unit=RawUnit(raw_unit),
        )
    )
    return {"metric_observation_id": obs.metric_observation_id}


async def _seed_macro(env: dict) -> dict:
    """World Bank SP.POP.TOTL 中国人口（公开数据；CapturedMacroFetch 离线构造）。"""
    import json

    from app.domain.macro_persistence import MacroSnapshotArtifactRole
    from app.domain.sources import AcquisitionMethod, SourceAuthorityTier, SourceCapability
    from app.macro.capture import CapturedMacroFetch, MacroRawJsonResponse
    from app.macro.contracts import (
        MacroFetchResult,
        MacroFrequency,
        MacroGeography,
        MacroGeographyType,
        MacroIndicator,
        MacroObservation,
        MacroPageInfo,
        MacroPeriodSemantics,
        MacroQuery,
        MacroTopic,
    )
    from app.services.macro_persistence_service import MacroPersistenceService

    query = MacroQuery(
        provider_key="world_bank",
        indicator_code="SP.POP.TOTL",
        country_code="CHN",
        start_year=2022,
        end_year=2024,
    )
    indicator = MacroIndicator(
        provider_key="world_bank",
        external_indicator_id="SP.POP.TOTL",
        name="Population, total",
        unit="",
        source_id="2",
        source_name="World Development Indicators",
        source_note="Total population is based on the de facto definition.",
        source_organization="World Bank",
        topics=(MacroTopic(topic_id="19", name="Population: Structure, growth & density"),),
    )
    geography = MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code="CHN",
        provider_country_id="CHN",
        iso2_code="CN",
        iso3_code="CHN",
        name="China",
        region_name="East Asia & Pacific",
        income_level_name="Upper middle income",
    )
    # 公开数据（World Bank 中国人口，单位：人）。
    population_by_year = {
        2022: Decimal("1412175000"),
        2023: Decimal("1410100000"),
        2024: Decimal("1408280000"),
    }
    observations = tuple(
        MacroObservation(
            provider_key="world_bank",
            external_indicator_id="SP.POP.TOTL",
            geography_code="CHN",
            period=str(year),
            normalized_period_start=date(year, 1, 1),
            frequency=MacroFrequency.ANNUAL,
            value=population_by_year[year],
            is_missing=False,
            period_semantics=MacroPeriodSemantics.PROVIDER_YEAR_LABEL,
            observation_status="",
        )
        for year in (2022, 2023, 2024)
    )
    result = MacroFetchResult(
        provider_key="world_bank",
        query=query,
        indicator=indicator,
        geography=geography,
        observations=observations,
        page_info=MacroPageInfo(
            page=1,
            pages=1,
            per_page=50,
            total=len(observations),
            last_updated="2025-01-01",
        ),
        fetched_at=datetime(2025, 6, 1, tzinfo=UTC),
        request_count=3,
        acquisition_method=AcquisitionMethod.OFFICIAL_API,
        authority_tier=SourceAuthorityTier.TIER_1,
        critical_claim_eligible=True,
        provider_capabilities=(SourceCapability.DOCUMENT_DOWNLOAD, SourceCapability.MACRO_DATA),
    )
    _FETCHED = datetime(2025, 6, 1, tzinfo=UTC)
    responses = (
        MacroRawJsonResponse(
            role=MacroSnapshotArtifactRole.INDICATOR_METADATA,
            page=None,
            response_status=200,
            final_hostname="api.worldbank.org",
            content_type="application/json",
            fetched_at=_FETCHED,
            raw_bytes=json.dumps(
                {"id": "SP.POP.TOTL", "name": "Population, total"}, sort_keys=True
            ).encode("utf-8"),
        ),
        MacroRawJsonResponse(
            role=MacroSnapshotArtifactRole.COUNTRY_METADATA,
            page=None,
            response_status=200,
            final_hostname="api.worldbank.org",
            content_type="application/json",
            fetched_at=_FETCHED,
            raw_bytes=json.dumps({"id": "CHN", "name": "China"}, sort_keys=True).encode("utf-8"),
        ),
        MacroRawJsonResponse(
            role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE,
            page=1,
            response_status=200,
            final_hostname="api.worldbank.org",
            content_type="application/json",
            fetched_at=_FETCHED,
            raw_bytes=json.dumps(
                {
                    "indicator": "SP.POP.TOTL",
                    "rows": [str(v) for v in population_by_year.values()],
                },
                sort_keys=True,
            ).encode("utf-8"),
        ),
    )
    captured = CapturedMacroFetch(result=result, responses=responses)
    persistence = MacroPersistenceService(env["sessionmaker"], env["raw_store"])
    persisted = await persistence.persist_captured_fetch(captured)
    return {"snapshot_id": persisted.snapshot_id}


async def _seed_valuation_observation(env: dict, company_id, *, value_text: str) -> dict:
    """curated PE_TTM 观测（公开市场数据近似值，as_of 冻结）。"""
    from app.valuation.contracts import ValuationMetricCode, ValuationMetricDraft
    from app.valuation.observation_service import ValuationObservationService

    html = (
        "<html><head><title>估值披露</title></head><body><article>"
        f"<p>2025年7月公司市盈率约{value_text}倍，处于历史相对合理区间。</p>"
        "</article></body></html>"
    ).encode()
    stored = env["raw_store"].put_html_bytes(html)
    async with env["sessionmaker"]() as session:
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
            title="市场估值观察",
            published_at=datetime(2025, 7, 15, tzinfo=UTC),
            reporting_period_end=None,
            source_url="https://www.xinhuanet.com/2025/0715/0001.htm",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime(2025, 7, 15, tzinfo=UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(
        source_id
    )
    chunked = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(
        parsed.parsed_source_id
    )
    from app.repositories.document_chunk_repository import DocumentChunkRepository

    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(chunked.chunk_set_id)
    chunk = chunks[0]
    quote_text = f"市盈率约{value_text}倍"
    idx = chunk.text.index(quote_text)
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question="公司当前估值水平如何？",
            evidence_statement=f"2025年7月市盈率约{value_text}倍",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + len(quote_text),
            extractor_name="benchmark-curator",
            extractor_version=1,
            extractor_model_id="curated-public-data",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await ValuationObservationService(env["sessionmaker"]).create_observation(
        ValuationMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=ValuationMetricCode.PE_TTM,
            metric_as_of=date(2025, 7, 15),
            source_value_text=value_text,
        )
    )
    return {"valuation_observation_id": obs.valuation_observation_id}


# ------------------------------------------------------------------ dataset build


async def build_benchmark_dataset(root: str | Path) -> dict:
    """构建 benchmark dataset（临时 seed DB → materialize → bundle）。"""
    root = Path(root)
    async with _temp_seed_db() as (sessionmaker, temp_url):
        # alembic / settings 都按 DATABASE_URL 解析：临时 seed DB 期间覆盖 env。
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = temp_url
        get_settings.cache_clear()
        try:
            await _upgrade_head()
            await _seed_and_materialize(sessionmaker, root)
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
            get_settings.cache_clear()
    return {
        "dataset_id": BENCHMARK_DATASET_ID,
        "dataset_version": BENCHMARK_DATASET_VERSION,
        "cases": list(_CASE_IDS),
        "root": str(root),
    }


async def _seed_and_materialize(sessionmaker, root: Path) -> None:
    raw_store = LocalRawArtifactStore(root=root / ".seed_raw", max_bytes=1024 * 1024 * 100)
    env = {"sessionmaker": sessionmaker, "raw_store": raw_store}
    await SourceRegistryService(sessionmaker).seed_defaults()
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

    company = await _seed_company(sessionmaker)
    company_id = company["company_id"]

    # 共享 document-only case 的文档（真实公开信息转述）。
    await _seed_document(
        env,
        company_id,
        document_type="news_article",
        title="贵州茅台经营基本面公开信息",
        body_text=(
            "贵州茅台是A股白酒行业龙头企业，主营酱香型白酒生产销售。"
            "公司拥有品牌壁垒与渠道优势，直销占比持续提升，"
            "2023年营业收入与归母净利润均保持增长。"
        ),
        source_url="https://www.xinhuanet.com/2025/0601/0001.htm",
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    await _seed_document(
        env,
        company_id,
        document_type="annual_report",
        title="贵州茅台2023年年度报告摘要",
        body_text=(
            "2023年公司实现营业收入1505.60亿元，同比增长18.04%；"
            "归母净利润747.34亿元，同比增长19.16%。"
        ),
        source_url="https://static.sse.com.cn/disclosure/listedinfo/announcement",
        published_at=datetime(2024, 4, 30, tzinfo=UTC),
        provider_key="sse",
    )
    await _seed_document(
        env,
        company_id,
        document_type="news_article",
        title="贵州茅台2022年财务数据公开信息",
        body_text=(
            "2022年公司实现营业收入1275.54亿元，归母净利润627.16亿元，经营现金流保持健康。"
        ),
        source_url="https://www.xinhuanet.com/2025/0602/0001.htm",
        published_at=datetime(2025, 6, 2, tzinfo=UTC),
    )
    await _seed_document(
        env,
        company_id,
        document_type="news_article",
        title="贵州茅台市场估值公开信息",
        body_text=("2025年7月贵州茅台滚动市盈率约21倍，处于近三年相对合理区间，股息率约3%。"),
        source_url="https://www.xinhuanet.com/2025/0716/0001.htm",
        published_at=datetime(2025, 7, 16, tzinfo=UTC),
    )

    # financial observations（curated 真实数字，元为单位）。
    fin_revenue_2023 = await _seed_financial_observation(
        env,
        company_id,
        metric_code="revenue",
        period_end=date(2023, 12, 31),
        source_value_text="150560000000",
        raw_unit="yuan",
        statement="营业收入1505.60亿元，同比增长18.04%",
    )
    fin_revenue_2022 = await _seed_financial_observation(
        env,
        company_id,
        metric_code="revenue",
        period_end=date(2022, 12, 31),
        source_value_text="127554000000",
        raw_unit="yuan",
        statement="营业收入1275.54亿元",
    )
    fin_profit_2023 = await _seed_financial_observation(
        env,
        company_id,
        metric_code="net_profit_parent",
        period_end=date(2023, 12, 31),
        source_value_text="74734000000",
        raw_unit="yuan",
        statement="归母净利润747.34亿元，同比增长19.16%",
    )
    fin_profit_2022 = await _seed_financial_observation(
        env,
        company_id,
        metric_code="net_profit_parent",
        period_end=date(2022, 12, 31),
        source_value_text="62716000000",
        raw_unit="yuan",
        statement="归母净利润627.16亿元",
    )

    # valuation（curated 近似值）：target + 3 个**不同** peer 公司（显式 peer 集合）。
    # 全部 peer PE 低于 target（fake analyst 判定 relative_high → premium 全正）。
    target_pe = await _seed_valuation_observation(env, company_id, value_text="21")
    peer_ids = []
    for security_code, value_text in (("600501", "19"), ("600502", "17"), ("600503", "15")):
        peer_company = await _seed_company(sessionmaker, security_code)
        peer_obs = await _seed_valuation_observation(
            env, peer_company["company_id"], value_text=value_text
        )
        peer_ids.append(peer_obs["valuation_observation_id"])
    comparison = await RelativeValuationComparisonService(sessionmaker).create_comparison(
        ComparisonDraft(
            target_company_id=company_id,
            target_observation_id=target_pe["valuation_observation_id"],
            peer_observation_ids=tuple(peer_ids),
            analysis_as_of=_AS_OF_DATE,
        )
    )

    # macro（World Bank 中国人口）。
    macro = await _seed_macro(env)

    # 全部 target 公司 source ids。
    from sqlalchemy import text as _text

    async with sessionmaker() as session:
        rows = (
            await session.execute(
                _text(
                    "SELECT source_id FROM source_records WHERE company_id = CAST(:cid AS uuid)"
                ).bindparams(cid=str(company_id))
            )
        ).all()
    document_source_ids = tuple(row[0] for row in rows)

    materializer = EvaluationSnapshotMaterializer(sessionmaker, raw_store)

    def _spec(
        case_id: str, question: str, *, structured=(), macro_ids=()
    ) -> EvalCaseMaterializationSpec:
        return EvalCaseMaterializationSpec(
            case_id=case_id,
            case_version=1,
            company_id=company_id,
            security_code="600519",
            research_question=question,
            analysis_as_of=BENCHMARK_AS_OF,
            tags=("benchmark",),
            document_source_ids=document_source_ids,
            macro_snapshot_ids=tuple(macro_ids),
            structured_artifacts=tuple(structured),
        )

    cases = [
        _spec(
            "moutai-business",
            "贵州茅台2023年经营基本面如何？",
        ),
        _spec(
            "moutai-financial",
            "贵州茅台2023年营收与利润较2022年如何变化？",
        ),
        _spec(
            "moutai-full",
            "贵州茅台2023年基本面、宏观环境与估值水平综合如何？",
            macro_ids=(macro["snapshot_id"],),
            structured=(
                StructuredArtifactSelection(
                    artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                    artifact_id=fin_revenue_2023["metric_observation_id"],
                ),
                StructuredArtifactSelection(
                    artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                    artifact_id=fin_revenue_2022["metric_observation_id"],
                ),
                StructuredArtifactSelection(
                    artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                    artifact_id=fin_profit_2023["metric_observation_id"],
                ),
                StructuredArtifactSelection(
                    artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                    artifact_id=fin_profit_2022["metric_observation_id"],
                ),
                StructuredArtifactSelection(
                    artifact_type=StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
                    artifact_id=target_pe["valuation_observation_id"],
                ),
                StructuredArtifactSelection(
                    artifact_type=StructuredArtifactType.RELATIVE_VALUATION_COMPARISON,
                    artifact_id=comparison.comparison_id,
                ),
            ),
        ),
    ]
    materialized = []
    for spec in cases:
        materialized.append(await materializer.materialize_case(spec))

    writer = EvaluationBundleWriter(root)
    for item in materialized:
        EvaluationSnapshotMaterializer.write_materialized(item, writer)
    manifest = EvaluationSnapshotMaterializer.assemble_dataset_manifest(
        BENCHMARK_DATASET_ID,
        BENCHMARK_DATASET_VERSION,
        materialized,
        description="InsightForge A股 benchmark：curated 真实公开信息，frozen as_of=2025-08-01",
    )
    writer.write_manifest(manifest)
