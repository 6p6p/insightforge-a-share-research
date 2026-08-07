"""Stage-2 source pipeline E2E acceptance (stage 2E.3).

需要真实 PostgreSQL（127.0.0.1:5433）。零真实网络：新闻链用 httpx.MockTransport
+ FakeResolver，Macro 链用 httpx.MockTransport，公司 PDF 用纯 stdlib fixture；
conftest autouse guard 兜底拦截任何非回环真实请求。覆盖三条端到端链：

1. 公司 PDF：Company → application/pdf RawArtifact → SourceRecord（sse +
   annual_report + user_upload）→ ParsedSource（pdf_layout v2）→
   ParsedSourceBlocks（pdf_page locator）→ replay；
2. 新闻 HTML：GDELT Discovery Run/Candidate → Original Publisher verification
   （MockTransport）→ HTML RawArtifact → SourceRecord（xinhuanet +
   news_article + public_html）→ ParsedSource（html_dom v2）→ DOM locator →
   replay；
3. Macro：WorldBank MockTransport → 原始 JSON artifacts → MacroSeries /
   MacroDatasetSnapshot → MacroObservations → replay。

横切不变量：
- RawArtifact 内容寻址 + 字节不可变：文件 SHA-256 == content_sha256，且
  backend/store "重启"（重建 LocalRawArtifactStore 指向同一 raw_root）后仍可读；
- SourceRecord provenance（provider/document_type/acquisition_method snapshot）；
- parser / fingerprint replay：同 source + 同 raw + 同 version → replayed=True；
- duplicate writes = 0：同输入重复执行不新增任何行；
- PDF / HTML locator 可回到对应 Source/Artifact（parsed_source.artifact_id ==
  source.artifact_id，block 关联该 parsed_source）；
- GDELT 永不成为 SourceRecord：news discovery run 的 JSON artifact 不被任何
  SourceRecord 引用，source_records 无 gdelt 来源；
- Macro Snapshot 不是 Evidence；
- 不产生 Chunk / Evidence / Claim / Chroma 数据：Stage 3 相关表不存在
  （schema 层面不变量）。
"""

import hashlib
import io
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.acquisition.host_resolver import HostResolver
from app.acquisition.html_fetcher import SafeHtmlFetcher
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.news_discovery_candidate import NewsDiscoveryCandidateModel
from app.db.models.news_discovery_run import NewsDiscoveryRunModel
from app.db.models.news_source_verification import NewsSourceVerificationModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.macro.world_bank.client import REQUEST_LIMIT, WorldBankClient
from app.macro.world_bank.provider import WorldBankProvider
from app.repositories.company_repository import CompanyRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.macro_persistence_service import MacroPersistenceService
from app.services.news_original_source_service import NewsOriginalSourceService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.macro.world_bank.helpers import (
    QUERY,
    country_response,
    indicator_response,
    json_response,
    observation_row,
    observations_response,
)
from tests.pdf_fixtures import duplicate_line_across_pages_pdf

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"
_XINHUA_DOMAIN = "www.xinhuanet.com"
_SSE_URL = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
# 含两个 <p>，保证 html_dom v2 抽取到 2 个 block。
_HTML = (
    "<html><head><title>新闻标题</title></head>"
    "<body><article><p>第一段正文。</p><p>第二段正文。</p></article></body></html>"
).encode()

# 模块级捕获原始 __init__：_build_provider 在同一测试内被多次调用时，若逐次
# 从 WorldBankClient.__init__ 捕获会拿到上一次的 monkeypatch 代理。
_REAL_CLIENT_INIT = WorldBankClient.__init__

# Stage 3 才允许出现的表；当前 schema 必须不存在。
_STAGE3_TABLES = ("evidence_cards", "document_chunks", "claims", "reports", "audits")


class FakeResolver(HostResolver):
    async def resolve(self, hostname: str) -> list[str]:
        return ["93.184.216.34"]


def _html_router() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": _XINHUA_URL})
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


def _macro_router(request: httpx.Request) -> httpx.Response:
    """确定性 Macro MockTransport：indicator + country + 单页 observations。"""
    path = request.url.path
    if path == "/v2/indicator/SP.POP.TOTL":
        return json_response(indicator_response())
    if path == "/v2/country/CHN":
        return json_response(country_response())
    if "/v2/country/CHN/indicator/" in path:
        rows = [
            observation_row(year, value=1400000000 + index)
            for index, year in enumerate(range(QUERY.start_year, QUERY.end_year + 1))
        ]
        return json_response(
            observations_response(page=1, pages=1, per_page=1000, total=len(rows), rows=rows)
        )
    raise AssertionError(f"unexpected path {path}")


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
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
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
    """统一 env：company + 全部默认 Provider（upsert），供三条链共享。"""
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code="600519",
                identity_key="SSE:600519",
                board="sse_main",
                official_name="测试公司",
                short_name="测试",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "raw_root": raw_root,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


# ---------------------------------------------------------------- 服务构造


def _parsing_service(env: dict) -> SourceParsingService:
    return SourceParsingService(env["sessionmaker"], env["raw_store"])


def _news_service(env: dict) -> NewsOriginalSourceService:
    return NewsOriginalSourceService(
        env["sessionmaker"],
        env["raw_store"],
        SafeHtmlFetcher(transport=_html_router(), resolver=FakeResolver()),
    )


def _macro_service(env: dict) -> MacroPersistenceService:
    return MacroPersistenceService(env["sessionmaker"], env["raw_store"])


def _build_provider(
    sessionmaker, transport: httpx.AsyncBaseTransport, monkeypatch
) -> WorldBankProvider:
    """构造 WorldBankProvider：向 WorldBankClient 注入 MockTransport。"""

    def _patched_init(
        self,
        *,
        allowed_domains: list[str],
        timeout: httpx.Timeout | None = None,
        request_limit: int = REQUEST_LIMIT,
    ) -> None:
        _REAL_CLIENT_INIT(
            self,
            allowed_domains=allowed_domains,
            transport=transport,
            timeout=timeout,
            request_limit=request_limit,
        )

    monkeypatch.setattr(WorldBankClient, "__init__", _patched_init)
    return WorldBankProvider(sessionmaker)


# ---------------------------------------------------------------- 数据读取


async def _all_parsed(env: dict) -> tuple:
    async with env["sessionmaker"]() as session:
        parsed = (await session.execute(select(ParsedSourceModel))).scalars().all()
        blocks = (await session.execute(select(ParsedSourceBlockModel))).scalars().all()
    return parsed, blocks


async def _all_news(env: dict) -> dict:
    async with env["sessionmaker"]() as session:
        runs = (await session.execute(select(NewsDiscoveryRunModel))).scalars().all()
        candidates = (await session.execute(select(NewsDiscoveryCandidateModel))).scalars().all()
        verifications = (await session.execute(select(NewsSourceVerificationModel))).scalars().all()
        sources = (await session.execute(select(SourceRecordModel))).scalars().all()
    return {
        "runs": runs,
        "candidates": candidates,
        "verifications": verifications,
        "sources": sources,
    }


async def _all_macro(env: dict) -> dict:
    async with env["sessionmaker"]() as session:
        series = (await session.execute(select(MacroSeriesModel))).scalars().all()
        snapshots = (await session.execute(select(MacroDatasetSnapshotModel))).scalars().all()
        artifacts = (await session.execute(select(RawArtifactModel))).scalars().all()
        links = (await session.execute(select(MacroSnapshotArtifactModel))).scalars().all()
        observations = (await session.execute(select(MacroObservationModel))).scalars().all()
    return {
        "series": series,
        "snapshots": snapshots,
        "artifacts": artifacts,
        "links": links,
        "observations": observations,
    }


async def _count_parsed_sources(env: dict) -> int:
    async with env["sessionmaker"]() as session:
        rows = (await session.execute(select(ParsedSourceModel.parsed_source_id))).scalars().all()
        return len(rows)


async def _get_source(env: dict, source_id):
    async with env["sessionmaker"]() as session:
        return await SourceRecordRepository(session).get_by_id(source_id)


async def _get_artifact(env: dict, artifact_id):
    async with env["sessionmaker"]() as session:
        return (
            await session.execute(
                select(RawArtifactModel).where(RawArtifactModel.artifact_id == artifact_id)
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------- 种子


async def _seed_company_pdf_source(env: dict) -> tuple:
    """合法公司 PDF SourceRecord：sse provider + annual_report + user_upload。

    用跨页重复文本 fixture 顺带验证 2E.2 收口（跨页相同行全部保留）。
    返回 (source_id, artifact_id, storage_key, raw_bytes)。
    """
    pdf = duplicate_line_across_pages_pdf()
    stored = env["raw_store"].put_pdf_stream(io.BytesIO(pdf))
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
            company_id=env["company_id"],
            provider_key="sse",
            artifact_id=artifact.artifact_id,
            document_type="annual_report",
            title="季度报告",
            published_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
            source_url=_SSE_URL,
            acquisition_method="user_upload",
            status="available",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=["company_announcement", "document_download"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        return record.source_id, artifact.artifact_id, stored.storage_key, pdf


async def _seed_news_candidate(env: dict) -> tuple:
    """GDELT discovery run + unverified candidate（run 用 dummy JSON artifact）。"""
    dummy = json.dumps({"url": _XINHUA_URL, "rank": 1}, ensure_ascii=True).encode()
    digest = hashlib.sha256(dummy).hexdigest()
    async with env["sessionmaker"]() as session:
        artifact = RawArtifactModel(
            content_sha256=digest,
            storage_key=f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.json",
            byte_size=len(dummy),
            media_type="application/json",
        )
        session.add(artifact)
        await session.flush()
        run = NewsDiscoveryRunModel(
            discovery_run_id=uuid4(),
            company_id=env["company_id"],
            engine="gdelt_doc",
            query_text="Kweichow Moutai",
            query_start_at=datetime(2026, 8, 1, tzinfo=UTC),
            query_end_at=datetime(2026, 8, 6, 12, 30, 45, tzinfo=UTC),
            max_results=10,
            raw_artifact_id=artifact.artifact_id,
            raw_content_sha256=digest,
            result_count=1,
            request_count=1,
            response_status=200,
            final_hostname="api.gdeltproject.org",
            content_type="application/json",
            query_fingerprint=digest,
            status="available",
            fetched_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        candidate = NewsDiscoveryCandidateModel(
            candidate_id=uuid4(),
            discovery_run_id=run.discovery_run_id,
            rank=1,
            title="新闻标题",
            discovered_url=_XINHUA_URL,
            normalized_url=_XINHUA_URL,
            url_sha256=hashlib.sha256(_XINHUA_URL.encode()).hexdigest(),
            domain=_XINHUA_DOMAIN,
            seen_at=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
            verification_status="unverified",
        )
        session.add(candidate)
        await session.commit()
        return run.discovery_run_id, candidate.candidate_id, artifact.artifact_id


# ---------------------------------------------------------------- 链 1：公司 PDF


async def test_company_pdf_pipeline_e2e(env) -> None:
    source_id, artifact_id, storage_key, pdf = await _seed_company_pdf_source(env)
    service = _parsing_service(env)

    result = await service.parse_source(source_id)
    assert result.parser_name == "pdf_layout"
    assert result.parser_version == 2
    assert result.replayed is False
    assert result.block_count == 4  # Header / Dup / Dup / Body two

    # ParsedSource 快照：raw_content_sha256 == artifact 哈希，指纹 64 hex。
    source = await _get_source(env, source_id)
    assert source is not None and source.artifact_id == artifact_id
    parsed, blocks = await _all_parsed(env)
    assert len(parsed) == 1
    parsed_row = parsed[0]
    assert parsed_row.artifact_id == artifact_id
    assert parsed_row.raw_content_sha256 == result.raw_content_sha256
    assert len(result.raw_content_sha256) == 64
    assert hashlib.sha256(pdf).hexdigest() == result.raw_content_sha256
    assert len(result.parse_fingerprint) == 64
    assert parsed_row.block_count == 4

    # 跨页相同 "Dup" 全部保留（2E.2 收口）：page1/page2 各一个，locator 不同。
    assert len(blocks) == 4
    assert [b.ordinal for b in blocks] == [1, 2, 3, 4]
    dupes = [
        (b.locator["page_number"], b.locator["line_index"], b.text)
        for b in blocks
        if b.text == "Dup"
    ]
    assert dupes == [(1, 2, "Dup"), (2, 1, "Dup")]

    # locator 可回到对应 Source/Artifact：block → parsed_source → artifact。
    assert all(b.locator["type"] == "pdf_page" for b in blocks)
    assert all(b.parsed_source_id == parsed_row.parsed_source_id for b in blocks)

    # replay：同 source + 同 raw + 同 version → replayed=True，零新增行。
    replayed = await service.parse_source(source_id)
    assert replayed.replayed is True
    assert replayed.parsed_source_id == result.parsed_source_id
    assert replayed.parse_fingerprint == result.parse_fingerprint
    assert await _count_parsed_sources(env) == 1  # duplicate writes = 0

    # store "重启"（重建 store 指向同一 raw_root）后 artifact 仍可读且字节一致。
    restarted = LocalRawArtifactStore(root=env["raw_root"], max_bytes=1024 * 1024)
    assert restarted.exists(storage_key)
    assert restarted.open(storage_key).read() == pdf

    # RawArtifact 不可变：内容寻址 + 文件哈希 == 登记哈希。
    artifact = await _get_artifact(env, artifact_id)
    assert artifact is not None
    path = env["raw_root"] / artifact.storage_key
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.content_sha256


# ---------------------------------------------------------------- 链 2：新闻 HTML


async def test_news_html_pipeline_e2e(env) -> None:
    run_id, candidate_id, gdelt_artifact_id = await _seed_news_candidate(env)
    news_service = _news_service(env)

    verify = await news_service.verify_candidate(candidate_id)
    assert verify.replayed is False
    assert verify.provider_key == "xinhuanet"

    # SourceRecord provenance：新闻只能经 news_article + public_html。
    source = await _get_source(env, verify.source_id)
    assert source is not None
    assert source.provider_key == "xinhuanet"
    assert source.document_type == "news_article"
    assert source.acquisition_method == "public_html"
    assert source.authority_tier_snapshot == 3
    assert source.critical_claim_eligible_snapshot is False

    # HTML RawArtifact：内容寻址、字节不可变。
    html_artifact = await _get_artifact(env, verify.artifact_id)
    assert html_artifact is not None
    assert html_artifact.media_type == "text/html"
    path = env["raw_root"] / html_artifact.storage_key
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == html_artifact.content_sha256

    # 解析为 html_dom v2 → DOM locator。
    parsed_result = await _parsing_service(env).parse_source(verify.source_id)
    assert parsed_result.parser_name == "html_dom"
    assert parsed_result.parser_version == 2
    assert parsed_result.replayed is False
    parsed, blocks = await _all_parsed(env)
    assert len(parsed) == 1
    assert parsed[0].artifact_id == verify.artifact_id  # locator 可回到 Source/Artifact
    assert len(blocks) == 2  # 两个 <p>
    assert all(b.locator["type"] == "html_dom" for b in blocks)
    assert all(b.locator["xpath"].startswith("/") for b in blocks)
    assert all(b.locator["tag"] == "p" for b in blocks)

    # replay（verify + parse 各一次）：零新增 SourceRecord / Verification / ParsedSource。
    replay_verify = await news_service.verify_candidate(candidate_id)
    assert replay_verify.replayed is True
    replay_parse = await _parsing_service(env).parse_source(verify.source_id)
    assert replay_parse.replayed is True
    rows = await _all_news(env)
    assert len(rows["sources"]) == 1
    assert len(rows["verifications"]) == 1
    assert await _count_parsed_sources(env) == 1

    # GDELT 永不成为 SourceRecord：run 的 JSON artifact 不被任何 SourceRecord 引用，
    # source_records 无 gdelt 来源，全量 provider_key 只来自已验证的新闻发布者。
    assert all(s.artifact_id != gdelt_artifact_id for s in rows["sources"])
    assert {s.provider_key for s in rows["sources"]} == {"xinhuanet"}
    assert rows["runs"][0].raw_artifact_id == gdelt_artifact_id

    # store 重启后 HTML artifact 可读。
    restarted = LocalRawArtifactStore(root=env["raw_root"], max_bytes=1024 * 1024)
    assert restarted.open(html_artifact.storage_key).read() == _HTML


# ---------------------------------------------------------------- 链 3：Macro


async def test_macro_pipeline_e2e(env, monkeypatch) -> None:
    provider = _build_provider(env["sessionmaker"], httpx.MockTransport(_macro_router), monkeypatch)
    service = _macro_service(env)

    result = await service.fetch_and_persist(provider, QUERY)
    assert result.replayed is False
    assert result.artifact_count == 3
    assert result.observation_count == 5
    assert len(result.snapshot_fingerprint) == 64

    rows = await _all_macro(env)
    assert len(rows["series"]) == 1
    assert len(rows["snapshots"]) == 1
    assert len(rows["artifacts"]) == 3
    assert len(rows["links"]) == 3
    assert len(rows["observations"]) == 5

    series = rows["series"][0]
    assert (series.provider_key, series.source_id, series.external_indicator_id) == (
        "world_bank",
        "2",
        "SP.POP.TOTL",
    )
    assert series.geography_code == "CHN"
    assert series.frequency == "annual"

    # 原始 JSON artifact 内容寻址 + 字节不可变。
    for artifact in rows["artifacts"]:
        assert artifact.media_type == "application/json"
        path = env["raw_root"] / artifact.storage_key
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.content_sha256

    # replay：同 QUERY + 同 MockTransport → 同 fingerprint → replayed=True，零新增行。
    replay = await service.fetch_and_persist(provider, QUERY)
    assert replay.replayed is True
    assert replay.snapshot_fingerprint == result.snapshot_fingerprint
    rows2 = await _all_macro(env)
    assert len(rows2["series"]) == 1
    assert len(rows2["snapshots"]) == 1
    assert len(rows2["artifacts"]) == 3
    assert len(rows2["observations"]) == 5  # duplicate writes = 0

    # store 重启后 Macro raw JSON artifact 可读。
    restarted = LocalRawArtifactStore(root=env["raw_root"], max_bytes=1024 * 1024)
    assert restarted.exists(rows["artifacts"][0].storage_key)

    # Macro Snapshot 不是 Evidence：snapshot 行只属于 macro 持久化表。
    assert all(s.status == "available" for s in rows["snapshots"])
    assert all(s.fingerprint_version == 1 for s in rows["snapshots"])


# ---------------------------------------------------------------- 横切：Stage 3 表不存在


async def test_no_stage3_tables_or_evidence(env) -> None:
    """Chunk / Evidence / Claim / Report / Audit 表必须不存在（schema 不变量）。"""
    async with env["sessionmaker"]() as session:
        for table in _STAGE3_TABLES:
            row = await session.execute(text(f"SELECT to_regclass('public.{table}')"))
            assert row.scalar() is None, f"Stage 3 表 {table} 不应存在"
