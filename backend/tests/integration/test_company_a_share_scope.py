"""A-share company-scope structural guarantee tests (stage 4C.2A Gate / spec B).

证明 A 股范围由 **companies 表 DB CHECK（migration 0007）+ 领域枚举**
（`app/domain/companies.py` 的 ExchangeCode / MarketBoard）在结构层完整保证：
任何能写入 `companies` 的 Company 行都必须是 A 股公司（exchange ∈
{SSE, SZSE, BSE}、board ∈ {sse_main, star, szse_main, chinext, bse}、
exchange↔board 一致、6 位代码、identity_key = exchange:code）。

因此 target / peer 公司只需以 `company_id` 引用真实 Company 行，就自动继承
A 股范围——**不需要在 comparison / claim Service 重复加 A 股判定逻辑**。

覆盖：
- 合法 A 股公司（SSE 主板）可以进入 comparison（走真实服务，target + 3 peers）；
- 非法 exchange（NASDAQ）无法形成有效 Company（ck_companies_exchange 拒绝）；
- 非法 board（SSE + otc）无法形成有效 Company（ck_companies_board / 一致性拒绝）；
- exchange↔board 不一致（SZSE + sse_main）无法形成有效 Company
  （ck_companies_exchange_board_consistency 拒绝）。

**零 Chroma / 零 LLM / 零 Claim / 零 Report 表**。
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import (
    ComparisonDraft,
    ValuationMetricCode,
    ValuationMetricDraft,
)
from app.valuation.observation_service import ValuationObservationService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台当前市盈率水平？"
_URL = "https://www.xinhuanet.com/2026/0809/0002.htm"
_SOURCE_TITLE = "估值新闻"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_METRIC_AS_OF = date(2026, 8, 7)
_ANALYSIS_AS_OF = date(2026, 8, 10)

# 与 comparison 测试文件错开的安全代码段（601xxx，互不冲突）。
_TARGET_VALUE = "15.3"
_PEER_VALUES = ["14.2", "15.0", "16.0"]


def _fin_html(paragraph: str) -> bytes:
    body = f"<p>{paragraph}</p>"
    return (
        "<html><head><title>估值新闻</title></head><body><article>"
        + body
        + "</article></body></html>"
    ).encode()


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


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
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


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    target_company_id = await _seed_company(sessionmaker, "601398")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "target_company_id": target_company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_company(sessionmaker, code: str) -> UUID:
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code=code,
                identity_key=f"SSE:{code}",
                board="sse_main",
                official_name=f"公司{code}",
                short_name=code,
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    return company_id


async def _try_insert_company(sessionmaker, **overrides) -> str | None:
    """尝试插入一条 Company 行；返回被违反的约束名（成功插入返回 None）。

    若成功插入则回滚（不落库），保证测试不留数据。
    """
    company_id = uuid4()
    base = dict(
        company_id=company_id,
        exchange="SSE",
        security_code="601399",
        identity_key="SSE:601399",
        board="sse_main",
        official_name="测试公司",
        short_name="测试",
        listing_status="listed",
        identity_source_provider_key="sse",
        identity_source_url="https://www.sse.com.cn",
    )
    base.update(overrides)
    # identity_key 必须与 exchange/security_code 自洽，否则先触发 identity_key CHECK，
    # 无法单独观测 exchange/board 约束。
    base["identity_key"] = f"{base['exchange']}:{base['security_code']}"
    async with sessionmaker() as session:
        try:
            await CompanyRepository(session).create(CompanyModel(**base))
            await session.commit()
            return None
        except IntegrityError as exc:
            await session.rollback()
            diag = getattr(exc.orig, "diag", None)
            return getattr(diag, "constraint_name", None) or str(exc.orig)


async def _seed_observation(env: dict, company_id: UUID, value_text: str) -> dict:
    html = _fin_html(f"2026年公司市盈率{value_text}倍，估值水平合理")
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
            title=_SOURCE_TITLE,
            published_at=_PUBLISHED_AT,
            reporting_period_end=None,
            source_url=_URL + f"?uid={uuid4().hex[:8]}",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsing = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsing.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    chunk = next(c for c in chunks if value_text in c.text)
    idx = chunk.text.index(value_text)
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="市盈率为" + chunk.text[idx : idx + len(value_text)] + "倍",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + len(value_text),
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await ValuationObservationService(env["sessionmaker"]).create_observation(
        ValuationMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=ValuationMetricCode.PE_TTM,
            metric_as_of=_METRIC_AS_OF,
            source_value_text=value_text,
        )
    )
    return {
        "company_id": company_id,
        "valuation_observation_id": obs.valuation_observation_id,
    }


# ---------------------------------------------------------------- 合法 A 股公司可进 comparison


async def test_legal_a_share_company_can_enter_comparison(env) -> None:
    """SSE 主板 target + 3 个 SSE peer → 真实服务成功创建 comparison。"""
    refs = {"target": await _seed_observation(env, env["target_company_id"], _TARGET_VALUE)}
    refs["peers"] = []
    for i, value in enumerate(_PEER_VALUES):
        company_id = await _seed_company(env["sessionmaker"], f"6014{2 + i:02d}")
        refs["peers"].append(await _seed_observation(env, company_id, value))

    result = await RelativeValuationComparisonService(env["sessionmaker"]).create_comparison(
        ComparisonDraft(
            target_company_id=refs["target"]["company_id"],
            target_observation_id=refs["target"]["valuation_observation_id"],
            peer_observation_ids=tuple(
                p["valuation_observation_id"] for p in refs["peers"]
            ),
            analysis_as_of=_ANALYSIS_AS_OF,
        )
    )
    assert result.replayed is False
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM relative_valuation_comparisons "
                    "WHERE comparison_id = :cid"
                ).bindparams(cid=result.comparison_id)
            )
        ).scalar_one()
        peer_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM relative_valuation_comparison_peers "
                    "WHERE comparison_id = :cid"
                ).bindparams(cid=result.comparison_id)
            )
        ).scalar_one()
    assert rows == 1
    assert peer_count == 3


# ---------------------------------------------------------------- 非法 exchange / board 被 DB CHECK 拒绝


async def test_non_a_share_exchange_cannot_form_company(sessionmaker) -> None:
    """exchange=NASDAQ 无法写入 companies（ck_companies_exchange 拒绝）。"""
    violated = await _try_insert_company(sessionmaker, exchange="NASDAQ", board="sse_main")
    assert violated == "ck_companies_exchange"


async def test_non_a_share_board_cannot_form_company(sessionmaker) -> None:
    """exchange=SSE + board=otc 无法写入 companies（ck_companies_board 拒绝）。"""
    violated = await _try_insert_company(sessionmaker, exchange="SSE", board="otc")
    assert violated == "ck_companies_board"


async def test_exchange_board_mismatch_cannot_form_company(sessionmaker) -> None:
    """SZSE + sse_main（不一致组合）无法写入（ck_companies_exchange_board_consistency 拒绝）。"""
    violated = await _try_insert_company(sessionmaker, exchange="SZSE", board="sse_main")
    assert violated == "ck_companies_exchange_board_consistency"


async def test_legal_company_row_is_never_rejected(sessionmaker) -> None:
    """合法 SSE 主板 + 6 位代码 + 自洽 identity_key 可写入并回滚（证明不是无条件拒绝）。"""
    violated = await _try_insert_company(
        sessionmaker,
        exchange="SSE",
        board="sse_main",
        security_code="601400",
    )
    assert violated is None
