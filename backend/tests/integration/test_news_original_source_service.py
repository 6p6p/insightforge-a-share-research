"""E2E integration tests for NewsOriginalSourceService (stage 2D.2A, §二十四-二十七).

需要真实 PostgreSQL（127.0.0.1:5433）。全部 MockTransport + 假 DNS resolver
（零真实网络，conftest autouse guard 兜底）。覆盖：
- §二十四 E2E：Candidate → Original Publisher → SafeHtmlFetcher → HTML
  RawArtifact → SourceRecord → Verification → candidate verified 全链路；
- §二十五 replay / 并发：同 candidate 二次 verify 无网络请求 replayed=True；
  并发 verify 只产生一个 source_record / 一条 verification；多个 Candidate
  命中同一 final_url 共享 source_record 但各有一条 verification；
- §二十六 Network Guard：未注入 transport 的真实请求被测试 guard 拦截；
- §二十七 no-Evidence / 结构不变量：published_at=NULL（seen_at 永不映射）、
  title_origin=discovery_candidate、只新增 artifact/source/verification 行、
  run 原始归档不受影响；
- 错误路径：Candidate 不存在 / domain 不匹配 / 发布者不支持。
"""

import asyncio
import hashlib
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
from app.db.models.news_discovery_candidate import NewsDiscoveryCandidateModel
from app.db.models.news_discovery_run import NewsDiscoveryRunModel
from app.db.models.news_source_verification import NewsSourceVerificationModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.news.errors import (
    NewsCandidateNotFound,
    NewsOriginalSourceIntegrityError,
    NewsPublisherUnsupported,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.news_discovery_candidate_repository import (
    NewsDiscoveryCandidateRepository,
)
from app.repositories.news_source_verification_repository import (
    NewsSourceVerificationRepository,
)
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.news_original_source_service import NewsOriginalSourceService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"
_XINHUA_DOMAIN = "www.xinhuanet.com"
_HTML = b"<html><head><title>News</title></head><body>content</body></html>"


class FakeResolver(HostResolver):
    async def resolve(self, hostname: str) -> list[str]:
        return ["93.184.216.34"]


def _html_router() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"location": _XINHUA_URL},
            )
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


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
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    # 确保 11 个默认 Provider（含 xinhuanet）存在（upsert，不破坏其他测试）。
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


async def _seed_candidate(
    sessionmaker,
    company_id,
    *,
    normalized_url: str = _XINHUA_URL,
    title: str = "新闻标题",
    domain: str = _XINHUA_DOMAIN,
    rank: int = 1,
) -> tuple:
    """创建一个 discovery run + 一个 unverified candidate（run 用 dummy JSON artifact）。

    每个 run 使用基于 URL/rank 的唯一原始内容，避免多个 run 共享同一
    content_sha256 违反 uq_raw_artifacts_content_sha256。
    """
    dummy = json.dumps({"url": normalized_url, "rank": rank}, ensure_ascii=True).encode()
    digest = hashlib.sha256(dummy).hexdigest()
    async with sessionmaker() as session:
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
            company_id=company_id,
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
            rank=rank,
            title=title,
            discovered_url=normalized_url,
            normalized_url=normalized_url,
            url_sha256=hashlib.sha256(normalized_url.encode()).hexdigest(),
            domain=domain,
            seen_at=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
            verification_status="unverified",
        )
        session.add(candidate)
        await session.commit()
        return run.discovery_run_id, candidate.candidate_id


def _service(
    env: dict,
    transport: httpx.AsyncBaseTransport | None = None,
) -> NewsOriginalSourceService:
    return NewsOriginalSourceService(
        env["sessionmaker"],
        env["raw_store"],
        SafeHtmlFetcher(transport=transport or _html_router(), resolver=FakeResolver()),
    )


async def _get_verification(env: dict, candidate_id):
    async with env["sessionmaker"]() as session:
        return await NewsSourceVerificationRepository(session).get_by_candidate_id(candidate_id)


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


async def _all_rows(env: dict) -> dict:
    async with env["sessionmaker"]() as session:
        runs = (await session.execute(select(NewsDiscoveryRunModel))).scalars().all()
        candidates = (await session.execute(select(NewsDiscoveryCandidateModel))).scalars().all()
        verifications = (await session.execute(select(NewsSourceVerificationModel))).scalars().all()
        artifacts = (await session.execute(select(RawArtifactModel))).scalars().all()
        source_records = (await session.execute(select(SourceRecordModel))).scalars().all()
    return {
        "runs": runs,
        "candidates": candidates,
        "verifications": verifications,
        "artifacts": artifacts,
        "source_records": source_records,
    }


# ---------------------------------------------------------------- §二十四 E2E


async def test_verify_candidate_e2e(env) -> None:
    run_id, candidate_id = await _seed_candidate(env["sessionmaker"], env["company_id"])
    service = _service(env)
    result = await service.verify_candidate(candidate_id)

    # 结果摘要
    assert result.replayed is False
    assert result.provider_key == "xinhuanet"
    assert result.status_code == 200
    assert result.content_type == "text/html"
    assert result.redirect_count == 0
    assert result.final_url == _XINHUA_URL

    # candidate 置 verified
    async with env["sessionmaker"]() as session:
        cand = await NewsDiscoveryCandidateRepository(session).get_by_id(candidate_id)
        assert cand is not None and cand.verification_status == "verified"

    # Verification 行
    ver = await _get_verification(env, candidate_id)
    assert ver is not None
    assert ver.verification_id == result.verification_id
    assert ver.candidate_id == candidate_id
    assert ver.source_id == result.source_id
    assert ver.publisher_provider_key == "xinhuanet"
    assert ver.requested_url == _XINHUA_URL
    assert ver.final_url == _XINHUA_URL
    assert ver.final_hostname == _XINHUA_DOMAIN
    assert ver.http_status == 200
    assert ver.content_type == "text/html"
    assert ver.redirect_count == 0
    assert ver.title_origin == "discovery_candidate"

    # SourceRecord 契约
    src = await _get_source(env, result.source_id)
    assert src is not None
    assert src.company_id == env["company_id"]
    assert src.provider_key == "xinhuanet"
    assert src.document_type == "news_article"
    assert src.acquisition_method == "public_html"
    assert src.status == "available"
    assert src.source_url == _XINHUA_URL
    assert src.title == "新闻标题"
    assert src.published_at is None
    assert src.reporting_period_end is None
    assert src.external_document_id is None
    assert src.authority_tier_snapshot == 3
    assert src.critical_claim_eligible_snapshot is False
    assert src.provider_capabilities_snapshot == ["news_article"]
    assert src.acquired_at is not None

    # HTML RawArtifact：内容寻址落盘、字节不可变
    art = await _get_artifact(env, src.artifact_id)
    assert art is not None
    assert art.media_type == "text/html"
    assert art.storage_key.startswith("sha256/")
    assert art.storage_key.endswith(".html")
    path = env["raw_root"] / art.storage_key
    assert path.is_file()
    assert path.read_bytes() == _HTML
    assert hashlib.sha256(path.read_bytes()).hexdigest() == art.content_sha256
    assert art.artifact_id == result.artifact_id


# ---------------------------------------------------------------- §二十五 replay / 并发


async def test_replay_returns_same_without_network(env) -> None:
    _, candidate_id = await _seed_candidate(env["sessionmaker"], env["company_id"])
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    service = _service(env, transport=httpx.MockTransport(handler))

    first = await service.verify_candidate(candidate_id)
    assert first.replayed is False
    assert calls["n"] == 1

    second = await service.verify_candidate(candidate_id)
    assert second.replayed is True
    assert calls["n"] == 1  # replay 不再发起任何网络请求
    assert second.verification_id == first.verification_id
    assert second.source_id == first.source_id
    assert second.artifact_id == first.artifact_id


async def test_concurrent_verify_single_source_and_verification(env) -> None:
    _, candidate_id = await _seed_candidate(env["sessionmaker"], env["company_id"])
    service = _service(env)

    results = await asyncio.gather(
        service.verify_candidate(candidate_id),
        service.verify_candidate(candidate_id),
    )

    assert len({r.verification_id for r in results}) == 1
    assert len({r.source_id for r in results}) == 1
    assert len({r.artifact_id for r in results}) == 1

    rows = await _all_rows(env)
    assert len(rows["source_records"]) == 1
    assert len(rows["verifications"]) == 1
    # run 的 dummy JSON artifact + 一个共享 HTML artifact
    assert len(rows["artifacts"]) == 2


async def test_two_candidates_same_final_url_share_source_separate_verifications(env) -> None:
    """Invariant H：多个 Candidate 命中同一 final_url → 共享 SourceRecord，各一条 Verification。"""
    redirect_url = "https://www.xinhuanet.com/redirect"
    _, c1 = await _seed_candidate(
        env["sessionmaker"], env["company_id"], normalized_url=_XINHUA_URL, title="标题A", rank=1
    )
    _, c2 = await _seed_candidate(
        env["sessionmaker"], env["company_id"], normalized_url=redirect_url, title="标题B", rank=2
    )
    service = _service(env)  # _html_router 将 /redirect 302 到 _XINHUA_URL

    r1 = await service.verify_candidate(c1)
    r2 = await service.verify_candidate(c2)

    assert r1.source_id == r2.source_id
    assert r1.final_url == r2.final_url == _XINHUA_URL
    assert r1.verification_id != r2.verification_id

    rows = await _all_rows(env)
    assert len(rows["source_records"]) == 1
    assert len(rows["verifications"]) == 2
    # 两个 run 各一个 dummy JSON + 共享的 HTML artifact
    assert len(rows["artifacts"]) == 3


# ---------------------------------------------------------------- §二十六 Network Guard


async def test_real_transport_blocked_by_network_guard(env) -> None:
    _, candidate_id = await _seed_candidate(env["sessionmaker"], env["company_id"])
    service = NewsOriginalSourceService(
        env["sessionmaker"],
        env["raw_store"],
        SafeHtmlFetcher(resolver=FakeResolver()),  # 未注入 MockTransport → 真实 httpx
    )
    with pytest.raises(AssertionError, match="real external HTTP is forbidden in tests"):
        await service.verify_candidate(candidate_id)


# ---------------------------------------------------------------- §二十七 no-Evidence / 结构


async def test_verify_creates_no_evidence_only_provenance(env) -> None:
    """verified 只表示溯源建立，不产生 Evidence；published_at 恒为 NULL。

    断言：verify 前后只有 artifact/source_record/verification 三类新增行 +
    candidate 状态翻转；run 的原始归档字节与 id 均不变；seen_at 永不映射到
    published_at。
    """
    run_id, candidate_id = await _seed_candidate(env["sessionmaker"], env["company_id"])
    async with env["sessionmaker"]() as session:
        before_run = (
            await session.execute(
                select(NewsDiscoveryRunModel).where(
                    NewsDiscoveryRunModel.discovery_run_id == run_id
                )
            )
        ).scalar_one()
        before_sha = before_run.raw_content_sha256
        before_artifact_id = before_run.raw_artifact_id

    result = await _service(env).verify_candidate(candidate_id)

    rows = await _all_rows(env)
    # 只有一条 candidate（无新 candidate）；新增一 HTML artifact + source + verification
    assert len(rows["candidates"]) == 1
    assert len(rows["source_records"]) == 1
    assert len(rows["verifications"]) == 1
    assert len(rows["artifacts"]) == 2

    src = rows["source_records"][0]
    ver = rows["verifications"][0]
    # 结构不变量
    assert src.published_at is None  # Invariant F：seen_at 永不映射 published_at
    assert src.source_url == _XINHUA_URL  # Invariant H：source_url 用 final URL
    assert ver.title_origin == "discovery_candidate"
    assert ver.requested_url == _XINHUA_URL  # discovery URL 保留在 Verification 溯源
    assert ver.verification_id == result.verification_id

    # run 原始归档不受影响
    async with env["sessionmaker"]() as session:
        after_run = (
            await session.execute(
                select(NewsDiscoveryRunModel).where(
                    NewsDiscoveryRunModel.discovery_run_id == run_id
                )
            )
        ).scalar_one()
    assert after_run.raw_content_sha256 == before_sha
    assert after_run.raw_artifact_id == before_artifact_id


# ---------------------------------------------------------------- 错误路径


async def test_candidate_not_found(env) -> None:
    service = _service(env)
    with pytest.raises(NewsCandidateNotFound) as exc:
        await service.verify_candidate(uuid4())
    assert exc.value.code == "news_candidate_not_found"


async def test_candidate_domain_mismatch_raises_integrity_error(env) -> None:
    _, candidate_id = await _seed_candidate(
        env["sessionmaker"],
        env["company_id"],
        domain="evil.example.com",  # 与 normalized_url hostname 不一致
    )
    service = _service(env)
    with pytest.raises(NewsOriginalSourceIntegrityError) as exc:
        await service.verify_candidate(candidate_id)
    assert exc.value.code == "news_original_source_integrity_error"


async def test_unsupported_publisher_raises(env) -> None:
    _, candidate_id = await _seed_candidate(
        env["sessionmaker"],
        env["company_id"],
        normalized_url="https://unknown.example.com/a.htm",
        title="未知域名",
        domain="unknown.example.com",
    )
    service = _service(env)
    with pytest.raises(NewsPublisherUnsupported) as exc:
        await service.verify_candidate(candidate_id)
    assert exc.value.code == "news_publisher_unsupported"
