"""E2E integration tests for NewsDiscoveryPersistenceService (stage 2D.1, §十八 E/F).

需要真实 PostgreSQL（127.0.0.1:5433）。覆盖：
- discover_and_persist A-H 全链路：raw artifact 归档 → run → candidates；
- run 字段与候选契约（rank/url_sha256/domain/verification_status=unverified）；
- fingerprint 由持久化行重算一致；
- replay 幂等（同响应二次持久化 replayed=True，同 run_id）；
- 跨次获取相同字节 → 同 fingerprint → replay；
- 并发 discover_and_persist 只产生一个 run / 一套 candidates；
- replay 完整性检查失败抛 NewsDiscoveryIntegrityError；
- DB 层异常包装为 NewsDiscoveryPersistenceFailed（事务回滚）；
- Run 不直接成为 Source：不产生任何 source_records 行。

全部 MockTransport（不访问真实 GDELT），原始归档只写 tmp_path，不连 Chroma。
conftest autouse guard 阻止任何非回环真实网络。
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.news_discovery_candidate import NewsDiscoveryCandidateModel
from app.db.models.news_discovery_run import NewsDiscoveryRunModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryQuery
from app.news.errors import (
    NewsDiscoveryIntegrityError,
    NewsDiscoveryPersistenceFailed,
)
from app.news.fingerprint import build_query_fingerprint
from app.news.gdelt.provider import GdeltNewsDiscoveryProvider
from app.repositories.company_repository import CompanyRepository
from app.repositories.news_discovery_candidate_repository import (
    NewsDiscoveryCandidateRepository,
)
from app.repositories.news_discovery_run_repository import NewsDiscoveryRunRepository
from app.services.news_discovery_service import NewsDiscoveryPersistenceService
from app.storage.raw_store import LocalRawArtifactStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_DEFAULT_PROVIDER_KEYS = (
    "sse",
    "szse",
    "bse",
    "cninfo",
    "csrc",
    "nbs",
    "fred",
    "world_bank",
)


def _router(request: httpx.Request) -> httpx.Response:
    """确定性 MockTransport：固定 endpoint 的 GDELT artlist 响应。"""
    assert request.url.path == "/api/v2/doc/doc"
    payload = {
        "articles": [
            {
                "url": "https://news.example.com/a?utm=1",
                "title": "First headline",
                "seendate": "20260801050000",
                "language": "English",
                "sourcecountry": "United States",
            },
            {
                "url": "https://news.example.com/b",
                "title": "Second headline",
                "seendate": "20260802050000",
            },
        ]
    }
    return httpx.Response(200, json=payload, headers={"content-type": "application/json"})


def _provider(transport: httpx.AsyncBaseTransport) -> GdeltNewsDiscoveryProvider:
    return GdeltNewsDiscoveryProvider(transport=transport)


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
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        placeholders = ",".join(f"'{key}'" for key in _DEFAULT_PROVIDER_KEYS)
        await session.execute(
            text(f"DELETE FROM source_providers WHERE provider_key NOT IN ({placeholders})")
        )
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
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


def _query(company_id, **overrides: object) -> NewsDiscoveryQuery:
    values: dict = {
        "company_id": company_id,
        "query_text": "Kweichow Moutai",
        "start_at": datetime(2026, 8, 1, tzinfo=UTC),
        "end_at": datetime(2026, 8, 6, 12, 30, 45, tzinfo=UTC),
        "max_results": 10,
    }
    values.update(overrides)
    return NewsDiscoveryQuery(**values)


def _service(env: dict) -> NewsDiscoveryPersistenceService:
    return NewsDiscoveryPersistenceService(env["sessionmaker"], env["raw_store"])


async def _all_rows(env: dict) -> dict:
    async with env["sessionmaker"]() as session:
        runs = (await session.execute(select(NewsDiscoveryRunModel))).scalars().all()
        candidates = (await session.execute(select(NewsDiscoveryCandidateModel))).scalars().all()
        artifacts = (await session.execute(select(RawArtifactModel))).scalars().all()
        source_records = (await session.execute(select(SourceRecordModel))).scalars().all()
    return {
        "runs": runs,
        "candidates": candidates,
        "artifacts": artifacts,
        "source_records": source_records,
    }


# ---------------------------------------------------------- 全链路持久化


async def test_persist_full_chain(env) -> None:
    service = _service(env)
    result = await service.discover_and_persist(
        _provider(httpx.MockTransport(_router)),
        _query(env["company_id"]),
    )

    assert result.replayed is False
    assert result.result_count == 2
    assert result.candidate_count == 2
    assert len(result.query_fingerprint) == 64

    rows = await _all_rows(env)
    assert len(rows["runs"]) == 1
    assert len(rows["candidates"]) == 2
    assert len(rows["artifacts"]) == 1
    assert len(rows["source_records"]) == 0  # Run 不直接成为 Source

    run = rows["runs"][0]
    assert run.discovery_run_id == result.discovery_run_id
    assert run.company_id == env["company_id"]
    assert run.engine == NewsDiscoveryEngine.GDELT_DOC.value
    assert run.query_text == "Kweichow Moutai"
    assert run.max_results == 10
    assert run.result_count == 2
    assert run.request_count == 1
    assert run.response_status == 200
    assert run.final_hostname == "api.gdeltproject.org"
    assert run.content_type == "application/json"
    assert run.status == "available"
    assert len(run.raw_content_sha256) == 64
    assert run.query_fingerprint == result.query_fingerprint

    # 原始响应归档：内容寻址文件，文件哈希 = content_sha256。
    artifact = rows["artifacts"][0]
    assert artifact.media_type == "application/json"
    assert artifact.storage_key.startswith("sha256/")
    assert artifact.storage_key.endswith(".json")
    path = env["raw_root"] / artifact.storage_key
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.content_sha256

    # fingerprint 可由持久化行重算一致。
    recomputed = build_query_fingerprint(
        NewsDiscoveryEngine.GDELT_DOC,
        _query(env["company_id"]),
        run.raw_content_sha256,
    )
    assert run.query_fingerprint == recomputed

    # 候选契约：rank/url_sha256/domain/verification_status。
    candidates = sorted(rows["candidates"], key=lambda c: c.rank)
    assert [c.rank for c in candidates] == [1, 2]
    assert [c.title for c in candidates] == ["First headline", "Second headline"]
    assert [c.domain for c in candidates] == ["news.example.com", "news.example.com"]
    assert all(len(c.url_sha256) == 64 for c in candidates)
    assert candidates[0].normalized_url == "https://news.example.com/a?utm=1"
    assert candidates[0].source_language == "English"
    assert candidates[0].source_country == "United States"
    assert candidates[1].source_language is None
    assert all(c.verification_status == "unverified" for c in candidates)


# ---------------------------------------------------------- 幂等 replay


async def test_replay_same_query_and_response_idempotent(env) -> None:
    service = _service(env)
    provider = _provider(httpx.MockTransport(_router))
    query = _query(env["company_id"])

    first = await service.discover_and_persist(provider, query)
    second = await service.discover_and_persist(provider, query)

    assert first.replayed is False
    assert second.replayed is True
    assert second.discovery_run_id == first.discovery_run_id
    assert second.query_fingerprint == first.query_fingerprint
    assert second.candidate_count == first.candidate_count

    rows = await _all_rows(env)
    assert len(rows["runs"]) == 1
    assert len(rows["candidates"]) == 2
    assert len(rows["artifacts"]) == 1


async def test_refetch_same_bytes_replays(env) -> None:
    # 两次独立 provider 获取相同原始字节 → 相同 fingerprint → 第二次 replay。
    service = _service(env)
    query = _query(env["company_id"])

    first = await service.discover_and_persist(_provider(httpx.MockTransport(_router)), query)
    second = await service.discover_and_persist(_provider(httpx.MockTransport(_router)), query)

    assert first.replayed is False
    assert second.replayed is True
    assert second.discovery_run_id == first.discovery_run_id
    rows = await _all_rows(env)
    assert len(rows["runs"]) == 1


async def test_concurrent_persist_single_run(env) -> None:
    service = _service(env)
    query = _query(env["company_id"])
    provider = _provider(httpx.MockTransport(_router))

    results = await asyncio.gather(
        service.discover_and_persist(provider, query),
        service.discover_and_persist(provider, query),
    )

    created = [r for r in results if not r.replayed]
    replayed = [r for r in results if r.replayed]
    assert len(created) == 1
    assert len(replayed) == 1
    assert created[0].discovery_run_id == replayed[0].discovery_run_id

    rows = await _all_rows(env)
    assert len(rows["runs"]) == 1
    assert len(rows["candidates"]) == 2
    assert len(rows["artifacts"]) == 1


# ---------------------------------------------------------- 错误路径


async def test_replay_integrity_error_on_tampered_candidates(env) -> None:
    service = _service(env)
    query = _query(env["company_id"])
    first = await service.discover_and_persist(_provider(httpx.MockTransport(_router)), query)
    assert first.replayed is False

    # 篡改：删除一条候选后重放 → 候选数 != result_count → 不自动修复。
    async with env["sessionmaker"]() as session:
        await session.execute(text("DELETE FROM news_discovery_candidates WHERE rank = 1"))
        await session.commit()

    with pytest.raises(NewsDiscoveryIntegrityError) as exc:
        await service.discover_and_persist(_provider(httpx.MockTransport(_router)), query)
    assert exc.value.code == "news_discovery_integrity_error"


async def test_persistence_failed_wraps_db_error(env) -> None:
    service = _service(env)

    async def _boom(self, candidates):
        raise SQLAlchemyError("injected db failure")

    import app.repositories.news_discovery_candidate_repository as repo_mod

    original = repo_mod.NewsDiscoveryCandidateRepository.bulk_create
    repo_mod.NewsDiscoveryCandidateRepository.bulk_create = _boom
    try:
        with pytest.raises(NewsDiscoveryPersistenceFailed) as exc:
            await service.discover_and_persist(
                _provider(httpx.MockTransport(_router)),
                _query(env["company_id"]),
            )
        assert exc.value.code == "news_discovery_persistence_failed"
    finally:
        repo_mod.NewsDiscoveryCandidateRepository.bulk_create = original

    # 原子性：candidate 写入失败 → run + artifact 行一并回滚。
    rows = await _all_rows(env)
    assert len(rows["runs"]) == 0
    assert len(rows["candidates"]) == 0
    assert len(rows["artifacts"]) == 0


# ---------------------------------------------------------- Repository 查询


async def test_run_repository_query_helpers(env) -> None:
    service = _service(env)
    query = _query(env["company_id"])
    result = await service.discover_and_persist(_provider(httpx.MockTransport(_router)), query)

    async with env["sessionmaker"]() as session:
        run_repo = NewsDiscoveryRunRepository(session)
        by_id = await run_repo.get_by_id(result.discovery_run_id)
        by_fp = await run_repo.get_by_fingerprint(result.query_fingerprint)
        assert by_id is not None and by_id.discovery_run_id == result.discovery_run_id
        assert by_fp is not None and by_fp.query_fingerprint == result.query_fingerprint
        listed = await run_repo.list_for_company(env["company_id"], limit=10, offset=0)
        assert [r.discovery_run_id for r in listed] == [result.discovery_run_id]
        assert await run_repo.count_for_company(env["company_id"]) == 1

        cand_repo = NewsDiscoveryCandidateRepository(session)
        candidates = await cand_repo.list_for_run(result.discovery_run_id)
        assert [c.rank for c in candidates] == [1, 2]
        assert await cand_repo.count_for_run(result.discovery_run_id) == 2
