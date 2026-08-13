"""EvaluationReplayRehydrator integration tests (stage 7B.1.4B.1).

在**独立临时 PostgreSQL 数据库**（`insightforge_eval_replay_*`，真实
`alembic upgrade head` → 0045）中验证隔离运行时复现：

- (O) happy path：frozen bundle → rehydrate → 精确 ID 复现（company / provider /
  raw_artifact / source_record）+ 字段 fidelity + `SourceParsingService.parse_source`
  + `ChunkingService.chunk_parsed_source` 端到端 + **共享库未被触碰**；
- (P) schema violation：frozen 值通过契约但违反目标 schema CHECK（exchange='NASDAQ'）
  → `EvalReplayIntegrityError`。

全程 0 LLM / 0 Chroma / 0 network。需要真实 PostgreSQL（127.0.0.1:5433）且账号
有 CREATEDB 权限。finally 恢复 settings 缓存并 DROP 临时库。
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.errors import EvalReplayIntegrityError
from app.eval.replay import EvaluationReplayRehydrator
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.replay_bundle import (
    CASE_ID,
    CASE_VERSION,
    COMPANY_ID,
    DOC_HTML,
    RAW_ARTIFACT_ID,
    SOURCE_RECORD_ID,
    build_replay_bundle,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


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


def _create_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')


def _drop_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _temp_url(base: str, db_name: str) -> str:
    return base.rsplit("/", 1)[0] + f"/{db_name}"


async def _upgrade_head(temp_url: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def _get(sessionmaker, model, pk):
    async with sessionmaker() as session:
        return await session.get(model, pk)


# ---------------------------------------------------------------- happy path（spec O）


async def test_replay_rehydrates_into_isolated_db_and_replays_document(
    monkeypatch, tmp_path
) -> None:
    """frozen bundle → rehydrate → 精确 ID + 字段 fidelity + parse + chunk + 隔离。"""
    shared_url = get_settings().database_url
    bundle_root = tmp_path / "bundle"
    spec = build_replay_bundle(bundle_root)

    temp_db = f"insightforge_eval_replay_{uuid4().hex[:12]}"
    temp_url = _temp_url(shared_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    try:
        await _upgrade_head(temp_url)
        iso_sessionmaker = iso_manager.session_factory()
        loader = EvaluationBundleLoader(bundle_root)
        rehydrator = EvaluationReplayRehydrator(iso_sessionmaker, iso_store, loader)

        rehydrated = await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        # (1) 精确 ID：company / source_record / raw_artifact 原样复现。
        assert rehydrated.company_id == COMPANY_ID
        assert rehydrated.provider_keys == ("sse", "xinhuanet")
        assert len(rehydrated.documents) == 1
        doc = rehydrated.documents[0]
        assert doc.source_record_id == SOURCE_RECORD_ID
        assert doc.raw_artifact_id == RAW_ARTIFACT_ID
        assert doc.content_sha256 == spec.document_sha256
        assert doc.byte_size == len(DOC_HTML)
        assert doc.media_type == "text/html"
        assert doc.storage_key.startswith("sha256/")

        # (2) Company：frozen 语义字段 + replay_v1 脚手架。
        company = await _get(iso_sessionmaker, CompanyModel, COMPANY_ID)
        assert company.security_code == "600519"
        assert company.official_name == "公司600519"
        assert company.short_name == "600519"
        assert company.exchange == "SSE"
        assert company.board == "sse_main"
        assert company.identity_key == "SSE:600519"
        assert company.listing_status == "unknown"

        # (3) Aliases：frozen alias 原样落库。
        async with iso_sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(CompanyAliasModel).where(CompanyAliasModel.company_id == COMPANY_ID)
                    )
                )
                .scalars()
                .all()
            )
        assert sorted({row.alias for row in rows}) == ["茅台股份", "贵州茅台"]

        # (4) Provider：frozen-exact + replay_v1 脚手架（不读 DEFAULT_PROVIDERS）。
        async with iso_sessionmaker() as session:
            providers = {
                p.provider_key: p
                for p in (await session.execute(select(SourceProviderModel))).scalars().all()
            }
        assert set(providers) == {"sse", "xinhuanet"}
        xinhuanet = providers["xinhuanet"]
        assert xinhuanet.display_name == "新华网"
        assert xinhuanet.enabled is True
        assert xinhuanet.capabilities == ["news_article"]
        assert xinhuanet.provider_type == "general_web"
        assert xinhuanet.authority_tier == 4

        # (5) RawArtifact：精确 artifact_id + 真实 storage_key/byte_size/media_type。
        raw = await _get(iso_sessionmaker, RawArtifactModel, RAW_ARTIFACT_ID)
        assert raw.content_sha256 == spec.document_sha256
        assert raw.storage_key == doc.storage_key
        assert raw.byte_size == len(DOC_HTML)
        assert raw.media_type == "text/html"

        # (6) SourceRecord：精确 source_id + frozen-exact 字段。
        source = await _get(iso_sessionmaker, SourceRecordModel, SOURCE_RECORD_ID)
        assert source.company_id == COMPANY_ID
        assert source.provider_key == "xinhuanet"
        assert source.artifact_id == RAW_ARTIFACT_ID
        assert source.document_type == "news_article"
        assert source.title == "研究新闻"
        assert source.source_url == "https://www.xinhuanet.com/2026/0809/0001.htm"
        assert source.authority_tier_snapshot == 3
        assert source.critical_claim_eligible_snapshot is False
        assert source.provider_capabilities_snapshot == ["news_article"]
        assert source.acquired_at == spec.snapshot.document_sources[0].acquired_at

        # (7) derived artifact 未被 seed：parse + chunk 由 caller 走真实 pipeline 重建。
        parsing = SourceParsingService(iso_sessionmaker, iso_store)
        parsed = await parsing.parse_source(SOURCE_RECORD_ID)
        assert parsed.source_id == SOURCE_RECORD_ID
        assert parsed.block_count >= 1
        chunked = await ChunkingService(iso_sessionmaker).chunk_parsed_source(
            parsed.parsed_source_id
        )
        assert chunked.chunk_count >= 1

        # (8) 隔离：共享库不出现任何 frozen ID（replay 只写隔离库）。
        shared_manager = DatabaseManager(
            database_url=shared_url, echo=False, connect_timeout_seconds=5
        )
        try:
            shared_sessionmaker = shared_manager.session_factory()
            assert await _get(shared_sessionmaker, CompanyModel, COMPANY_ID) is None
            assert await _get(shared_sessionmaker, RawArtifactModel, RAW_ARTIFACT_ID) is None
            assert await _get(shared_sessionmaker, SourceRecordModel, SOURCE_RECORD_ID) is None
        finally:
            await shared_manager.dispose()
    finally:
        await iso_manager.dispose()
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------- schema violation（spec P）


async def test_replay_schema_violation_raises_integrity_error(monkeypatch, tmp_path) -> None:
    """frozen exchange 通过契约但违反目标 CHECK → EvalReplayIntegrityError。"""
    shared_url = get_settings().database_url
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root, company_exchange="NASDAQ")

    temp_db = f"insightforge_eval_replay_{uuid4().hex[:12]}"
    temp_url = _temp_url(shared_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    try:
        await _upgrade_head(temp_url)
        rehydrator = EvaluationReplayRehydrator(
            iso_manager.session_factory(), iso_store, EvaluationBundleLoader(bundle_root)
        )
        with pytest.raises(EvalReplayIntegrityError):
            await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
    finally:
        await iso_manager.dispose()
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------- create-or-verify（spec A–I）


@asynccontextmanager
async def _isolated_target(monkeypatch, tmp_path):
    """创建独立临时 PG + 升级 head，yield (sessionmaker, raw_store)，finally 清理。"""
    shared_url = get_settings().database_url
    temp_db = f"insightforge_eval_replay_{uuid4().hex[:12]}"
    temp_url = _temp_url(shared_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    try:
        await _upgrade_head(temp_url)
        yield iso_manager.session_factory(), iso_store
    finally:
        await iso_manager.dispose()
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


async def _counts(sessionmaker) -> dict:
    """按模型统计行数（用于 idempotency / rollback 断言）。"""
    models = {
        "company": CompanyModel,
        "provider": SourceProviderModel,
        "raw": RawArtifactModel,
        "source": SourceRecordModel,
        "alias": CompanyAliasModel,
    }
    out: dict = {}
    async with sessionmaker() as session:
        for name, model in models.items():
            out[name] = len((await session.execute(select(model))).scalars().all())
    return out


async def test_replay_twice_idempotent_no_duplicate_rows(monkeypatch, tmp_path) -> None:
    """spec I (1)(2)(7)：同 bundle 同 target DB 重放两次成功，行数不变，alias 不重复。"""
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sm, store):
        rehydrator = EvaluationReplayRehydrator(sm, store, EvaluationBundleLoader(bundle_root))
        first = await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
        second = await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        # 语义等价：精确 ID + documents 完全一致。
        assert first == second
        assert first.company_id == COMPANY_ID
        assert first.provider_keys == ("sse", "xinhuanet")

        # 行数不变：company=1, provider=2, raw=1, source=1, alias=2（无重复）。
        assert await _counts(sm) == {
            "company": 1,
            "provider": 2,
            "raw": 1,
            "source": 1,
            "alias": 2,
        }


async def test_replay_provider_mismatch_rejected(monkeypatch, tmp_path) -> None:
    """spec I (3)：已有 provider 同 key 但 display_name 不同 → reject，不覆盖。"""
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sm, store):
        rehydrator = EvaluationReplayRehydrator(sm, store, EvaluationBundleLoader(bundle_root))
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        async with sm() as session:
            provider = await session.get(SourceProviderModel, "xinhuanet")
            provider.display_name = "被篡改的新华网"
            await session.commit()

        with pytest.raises(EvalReplayIntegrityError):
            await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        # 未被静默覆盖：display_name 仍是篡改值（replay 不写回）。
        async with sm() as session:
            provider = await session.get(SourceProviderModel, "xinhuanet")
            assert provider.display_name == "被篡改的新华网"


async def test_replay_company_mismatch_rejected(monkeypatch, tmp_path) -> None:
    """spec I (4)：已有 company 同 ID 但 official_name 不同 → reject。"""
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sm, store):
        rehydrator = EvaluationReplayRehydrator(sm, store, EvaluationBundleLoader(bundle_root))
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        async with sm() as session:
            company = await session.get(CompanyModel, COMPANY_ID)
            company.official_name = "被篡改的公司全称"
            await session.commit()

        with pytest.raises(EvalReplayIntegrityError):
            await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)


async def test_replay_raw_artifact_mismatch_rejected(monkeypatch, tmp_path) -> None:
    """spec I (5)：已有 raw_artifact 同 ID 但 content_sha256 不同 → reject，不覆盖。"""
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sm, store):
        rehydrator = EvaluationReplayRehydrator(sm, store, EvaluationBundleLoader(bundle_root))
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        tampered_sha = "0" * 64
        async with sm() as session:
            raw = await session.get(RawArtifactModel, RAW_ARTIFACT_ID)
            raw.content_sha256 = tampered_sha
            await session.commit()

        with pytest.raises(EvalReplayIntegrityError):
            await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        # 未被覆盖。
        async with sm() as session:
            raw = await session.get(RawArtifactModel, RAW_ARTIFACT_ID)
            assert raw.content_sha256 == tampered_sha


async def test_replay_source_record_mismatch_rejected(monkeypatch, tmp_path) -> None:
    """spec I (6)：已有 source_record 同 ID 但 title 不同 → reject。"""
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sm, store):
        rehydrator = EvaluationReplayRehydrator(sm, store, EvaluationBundleLoader(bundle_root))
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        async with sm() as session:
            source = await session.get(SourceRecordModel, SOURCE_RECORD_ID)
            source.title = "被篡改的标题"
            await session.commit()

        with pytest.raises(EvalReplayIntegrityError):
            await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)


async def test_replay_semantic_conflict_leaves_no_partial_rows(monkeypatch, tmp_path) -> None:
    """spec I (8) / F：raw 冲突（entity 4）→ 事务回滚，provider/company/alias 无残留。

    预置一个同 artifact_id 但 content_sha256 不同的 raw_artifact，rehydrate 在
    raw verify 处失败 → 之前已创建的 provider/company/alias 全部回滚（无 partial
    rows），仅保留预置的那条 raw（独立事务提交）。
    """
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sm, store):
        # 预置 mismatching raw（独立事务，提交后持久化）。
        async with sm() as session:
            session.add(
                RawArtifactModel(
                    artifact_id=RAW_ARTIFACT_ID,
                    content_sha256="0" * 64,
                    storage_key="sha256/00/" + "0" * 64,
                    byte_size=10,
                    media_type="text/html",
                )
            )
            await session.commit()

        rehydrator = EvaluationReplayRehydrator(sm, store, EvaluationBundleLoader(bundle_root))
        with pytest.raises(EvalReplayIntegrityError):
            await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)

        counts = await _counts(sm)
        assert counts["provider"] == 0
        assert counts["company"] == 0
        assert counts["alias"] == 0
        assert counts["source"] == 0
        assert counts["raw"] == 1  # 仅预置的那条 mismatching raw
