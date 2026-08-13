"""E2E integration tests for MacroPersistenceService (stage 2C.2B).

需要真实 PostgreSQL（127.0.0.1:5433）。覆盖 §十六：
- MockTransport → fetch_with_capture → persist_captured_fetch 全链路；
- 单事务写入 series / snapshot / raw_artifacts / links / observations；
- 原始 JSON 字节归档内容寻址（sha256=文件哈希）、fingerprint 持久化可重算；
- replay 幂等（同 captured 二次持久化 replayed=True，不重复写）；
- 跨次获取字节稳定 → 同 fingerprint → replay；
- 并发 persist（asyncio.gather）只产生一个 snapshot / 一套 links+observations；
- replay 完整性检查失败抛 MacroSnapshotIntegrityError；
- 校验失败不落任何数据；DB 层异常包装为 MacroPersistenceFailed。

不访问真实 World Bank（httpx.MockTransport），原始归档只写 tmp_path，
不连 Chroma。conftest autouse guard 阻止任何非回环真实网络。
"""

import asyncio
import hashlib
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.session import DatabaseManager
from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.macro.errors import (
    MacroArtifactConflict,
    MacroCaptureInvalid,
    MacroPersistenceFailed,
    MacroSnapshotIntegrityError,
)
from app.macro.fingerprint import (
    FingerprintArtifact,
    build_macro_snapshot_fingerprint,
)
from app.macro.world_bank.client import REQUEST_LIMIT, WorldBankClient
from app.macro.world_bank.provider import WorldBankProvider
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.macro_persistence_service import MacroPersistenceService
from app.storage.raw_store import LocalRawArtifactStore
from tests.macro.world_bank.helpers import (
    QUERY,
    country_response,
    indicator_response,
    json_response,
    observation_row,
    observations_response,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

# 模块级捕获原始 __init__：_build_provider 在同一测试内被多次调用时，
# 若逐次从 WorldBankClient.__init__ 捕获会拿到上一次的 monkeypatch 代理，
# 嵌套签名（无 transport）导致 TypeError。
_REAL_CLIENT_INIT = WorldBankClient.__init__

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
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
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
    async with sessionmaker() as session:
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
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "raw_root": raw_root,
    }
    await _cleanup(sessionmaker)


def _router(request: httpx.Request) -> httpx.Response:
    """确定性 MockTransport 路由：indicator + country + 单页 observations。"""
    path = request.url.path
    if path == "/v2/indicator/SP.POP.TOTL":
        return json_response(indicator_response())
    if path == "/v2/country/CHN":
        return json_response(country_response())
    if "/v2/country/CHN/indicator/" in path:
        page = int(request.url.params["page"])
        rows = [
            observation_row(year, value=1400000000 + index)
            for index, year in enumerate(range(2020, 2025))
        ]
        return json_response(
            observations_response(page=page, pages=1, per_page=1000, total=len(rows), rows=rows)
        )
    raise AssertionError(f"unexpected path {path}")


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


def _service(env: dict) -> MacroPersistenceService:
    return MacroPersistenceService(env["sessionmaker"], env["raw_store"])


async def _all_rows(env: dict) -> dict:
    """读取持久化后的全量行，返回 {表: [行]}。"""
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


async def _fetch_captured(env: dict, monkeypatch) -> object:
    provider = _build_provider(env["sessionmaker"], httpx.MockTransport(_router), monkeypatch)
    return await provider.fetch_with_capture(QUERY)


# ---------------------------------------------------------- 全链路持久化


async def test_persist_full_chain(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    result = await service.persist_captured_fetch(captured)

    assert result.replayed is False
    assert result.artifact_count == 3
    assert result.observation_count == 5
    assert len(result.snapshot_fingerprint) == 64

    rows = await _all_rows(env)
    assert len(rows["series"]) == 1
    assert len(rows["snapshots"]) == 1
    assert len(rows["artifacts"]) == 3
    assert len(rows["links"]) == 3
    assert len(rows["observations"]) == 5

    series = rows["series"][0]
    assert series.series_id == result.series_id
    assert series.provider_key == "world_bank"
    assert series.source_id == "2"
    assert series.external_indicator_id == "SP.POP.TOTL"
    assert series.geography_type == "country"
    assert series.geography_code == "CHN"
    assert series.frequency == "annual"

    snapshot = rows["snapshots"][0]
    assert snapshot.snapshot_id == result.snapshot_id
    assert snapshot.snapshot_fingerprint == result.snapshot_fingerprint
    assert snapshot.fingerprint_version == 1
    assert snapshot.normalization_version == "world_bank_v1"
    assert snapshot.requested_country_code == "CHN"
    assert snapshot.pages == 1
    assert snapshot.status == "available"

    # 原始响应归档：3 个 JSON artifact，storage_key 内容寻址，文件哈希=sha256。
    for artifact in rows["artifacts"]:
        assert artifact.media_type == "application/json"
        assert artifact.storage_key.startswith("sha256/")
        assert artifact.storage_key.endswith(".json")
        path = env["raw_root"] / artifact.storage_key
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.content_sha256

    # link 角色/页码/响应元数据。
    roles = {link.role for link in rows["links"]}
    assert roles == {"indicator_metadata", "country_metadata", "observations_page"}
    for link in rows["links"]:
        assert link.final_hostname == "api.worldbank.org"
        assert link.response_status == 200
    obs_page = next(link for link in rows["links"] if link.role == "observations_page")
    assert obs_page.page == 1

    # 观测值全程 Decimal 精确（大数不丢精度）。
    assert [o.period for o in rows["observations"]] == ["2020", "2021", "2022", "2023", "2024"]
    assert [o.value_numeric for o in rows["observations"]] == [
        Decimal(str(1400000000 + i)) for i in range(5)
    ]
    assert all(o.decimal_scale == 0 for o in rows["observations"])
    assert all(o.frequency == "annual" for o in rows["observations"])

    # fingerprint 可由持久化的 artifact rows 重算一致。
    artifact_by_id = {a.artifact_id: a for a in rows["artifacts"]}
    fp_artifacts = tuple(
        FingerprintArtifact(
            role=MacroSnapshotArtifactRole(link.role),
            page=link.page,
            sha256=artifact_by_id[link.artifact_id].content_sha256,
            response_status=link.response_status,
            final_hostname=link.final_hostname,
            content_type="application/json",
        )
        for link in rows["links"]
    )
    recomputed = build_macro_snapshot_fingerprint(captured.result, fp_artifacts)
    assert snapshot.snapshot_fingerprint == recomputed


async def test_fetch_and_persist_end_to_end(env, monkeypatch) -> None:
    provider = _build_provider(env["sessionmaker"], httpx.MockTransport(_router), monkeypatch)
    service = _service(env)

    result = await service.fetch_and_persist(provider, QUERY)

    assert result.replayed is False
    assert result.observation_count == 5
    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 1


# ---------------------------------------------------------- 幂等 replay


async def test_replay_same_capture_idempotent(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    first = await service.persist_captured_fetch(captured)
    second = await service.persist_captured_fetch(captured)

    assert first.replayed is False
    assert second.replayed is True
    assert second.snapshot_fingerprint == first.snapshot_fingerprint
    assert second.snapshot_id == first.snapshot_id
    assert second.series_id == first.series_id
    assert second.artifact_count == 3
    assert second.observation_count == 5

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 1
    assert len(rows["links"]) == 3
    assert len(rows["observations"]) == 5
    assert len(rows["artifacts"]) == 3


async def test_refetch_same_bytes_replays(env, monkeypatch) -> None:
    # 两次独立获取，原始字节相同 → 相同 fingerprint → 第二次 replay。
    provider1 = _build_provider(env["sessionmaker"], httpx.MockTransport(_router), monkeypatch)
    captured1 = await provider1.fetch_with_capture(QUERY)
    service = _service(env)
    first = await service.persist_captured_fetch(captured1)

    provider2 = _build_provider(env["sessionmaker"], httpx.MockTransport(_router), monkeypatch)
    captured2 = await provider2.fetch_with_capture(QUERY)
    second = await service.persist_captured_fetch(captured2)

    assert first.replayed is False
    assert second.replayed is True
    assert second.snapshot_fingerprint == first.snapshot_fingerprint
    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 1


# ---------------------------------------------------------- 并发


async def test_concurrent_persist_single_snapshot(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    results = await asyncio.gather(
        service.persist_captured_fetch(captured),
        service.persist_captured_fetch(captured),
    )

    created = [r for r in results if not r.replayed]
    replayed = [r for r in results if r.replayed]
    assert len(created) == 1
    assert len(replayed) == 1
    assert created[0].snapshot_fingerprint == replayed[0].snapshot_fingerprint

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 1
    assert len(rows["series"]) == 1
    assert len(rows["links"]) == 3
    assert len(rows["observations"]) == 5


# ---------------------------------------------------------- 错误路径


async def test_replay_integrity_error_on_tampered_observations(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)
    first = await service.persist_captured_fetch(captured)
    assert first.replayed is False

    # 篡改：删除一条观测后重放 → 观测数不一致 → 不自动修复，抛 IntegrityError。
    async with env["sessionmaker"]() as session:
        await session.execute(text("DELETE FROM macro_observations WHERE period = '2020'"))
        await session.commit()

    with pytest.raises(MacroSnapshotIntegrityError) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_snapshot_integrity_error"


async def test_validation_error_persists_nothing(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    # 构造缺 country 元数据的非法捕获：校验先于任何文件/DB 写入。
    from app.macro.capture import CapturedMacroFetch

    invalid = CapturedMacroFetch(
        result=captured.result,
        responses=captured.responses[1:],  # 去掉 indicator_metadata
    )
    service = _service(env)
    with pytest.raises(MacroCaptureInvalid) as exc:
        await service.persist_captured_fetch(invalid)
    assert exc.value.code == "macro_capture_invalid"

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 0
    assert len(rows["series"]) == 0
    assert len(rows["artifacts"]) == 0
    assert list(env["raw_root"].rglob("*.json")) == []


async def test_persistence_failed_wraps_db_error(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    async def _boom(self, observations):
        raise SQLAlchemyError("injected db failure")

    monkeypatch.setattr(MacroObservationRepository, "bulk_create", _boom)
    with pytest.raises(MacroPersistenceFailed) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_persistence_failed"
    # 原子性（D：observation bulk_create 失败）：整个事务回滚。
    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 0
    assert len(rows["links"]) == 0
    assert len(rows["observations"]) == 0


# ---------------------------------------------------------- JSON-only 防线


async def test_pdf_artifact_not_reused_as_macro_json(env, monkeypatch) -> None:
    """既有同 SHA 的 RawArtifact 若 media_type=application/pdf，不得被复用为 Macro JSON。

    Service 必须拒绝 MacroArtifactConflict，不创建 Snapshot/Links/Observations。
    """
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    # 先让 raw_store 产生合法 JSON 的 stored descriptor，取得 content_sha256。
    stored = env["raw_store"].put_json_bytes(captured.responses[0].raw_bytes)

    # 数据库预先插入同 SHA、但 media_type=application/pdf 的既有行。
    async with env["sessionmaker"]() as session:
        session.add(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=f"sha256/ff/ff/{stored.content_sha256}.pdf",
                byte_size=stored.byte_size,
                media_type="application/pdf",
            )
        )
        await session.commit()

    with pytest.raises(MacroArtifactConflict) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_artifact_conflict"

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 0
    assert len(rows["links"]) == 0
    assert len(rows["observations"]) == 0


# ---------------------------------------------------------- 事务原子性故障注入


async def test_atomic_rollback_series_created_then_failure(env, monkeypatch) -> None:
    """A：Series get_or_create 之后、Snapshot 之前注入失败。

    单事务语义下 rollback 使 series 一并回滚；即使未来架构允许稳定身份
    Series 独立提交，稳定 identity Series 不携带 partial 数据，不违反原子性。
    """
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    async def _boom(self, artifact):
        raise SQLAlchemyError("injected failure after series creation")

    monkeypatch.setattr(RawArtifactRepository, "get_or_create", _boom)
    with pytest.raises(MacroPersistenceFailed) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_persistence_failed"

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 0
    assert len(rows["links"]) == 0
    assert len(rows["observations"]) == 0


async def test_atomic_rollback_snapshot_created_then_failure(env, monkeypatch) -> None:
    """B：Snapshot 插入之后、Artifact Link 写入前注入失败。"""
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)

    async def _boom(self, link):
        raise SQLAlchemyError("injected failure after snapshot creation")

    monkeypatch.setattr(MacroSnapshotRepository, "add_artifact_link", _boom)
    with pytest.raises(MacroPersistenceFailed) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_persistence_failed"

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 0
    assert len(rows["links"]) == 0
    assert len(rows["observations"]) == 0


async def test_atomic_rollback_partial_links_failure(env, monkeypatch) -> None:
    """C：Artifact Link 写到中途（第二条）失败 → 无 partial links。"""
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)
    original = MacroSnapshotRepository.add_artifact_link
    calls = 0

    async def _fail_second(self, link):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError("injected link failure at second link")
        return await original(self, link)

    monkeypatch.setattr(MacroSnapshotRepository, "add_artifact_link", _fail_second)
    with pytest.raises(MacroPersistenceFailed) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_persistence_failed"

    rows = await _all_rows(env)
    assert len(rows["snapshots"]) == 0
    assert len(rows["links"]) == 0
    assert len(rows["observations"]) == 0


# ---------------------------------------------------------- replay 数据损坏


async def test_replay_integrity_error_on_deleted_artifact_link(env, monkeypatch) -> None:
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)
    first = await service.persist_captured_fetch(captured)
    assert first.replayed is False

    # 篡改：删除一条 Artifact Link 后重放 → link 数不一致 → 不自动补回。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM macro_snapshot_artifacts WHERE role = 'indicator_metadata'")
        )
        await session.commit()

    with pytest.raises(MacroSnapshotIntegrityError) as exc:
        await service.persist_captured_fetch(captured)
    assert exc.value.code == "macro_snapshot_integrity_error"


# ------------------------------------------- verify_snapshot_integrity 重算（7B.1.1B Part D）


async def test_verify_snapshot_integrity_recomputes_fingerprint(env, monkeypatch) -> None:
    """合法快照：verify_snapshot_integrity 重算 fingerprint 一致，返回 snapshot。"""
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)
    result = await service.persist_captured_fetch(captured)

    async with env["sessionmaker"]() as session:
        snapshot = await service.verify_snapshot_integrity(session, result.snapshot_id)

    assert snapshot is not None
    assert snapshot.snapshot_id == result.snapshot_id


async def test_verify_snapshot_integrity_rejects_tampered_observation_value(
    env, monkeypatch
) -> None:
    """篡改观测值（保留旧 fingerprint）→ 重算 fingerprint 不一致 → 拒绝。"""
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)
    result = await service.persist_captured_fetch(captured)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE macro_observations SET value_numeric = 999 WHERE period = '2024'")
        )
        await session.commit()

    async with env["sessionmaker"]() as session:
        with pytest.raises(MacroSnapshotIntegrityError, match="fingerprint mismatch"):
            await service.verify_snapshot_integrity(session, result.snapshot_id)


async def test_verify_snapshot_integrity_rejects_tampered_fingerprint(env, monkeypatch) -> None:
    """只篡改 snapshot_fingerprint（内容不变）→ 重算值 != persisted → 拒绝。"""
    captured = await _fetch_captured(env, monkeypatch)
    service = _service(env)
    result = await service.persist_captured_fetch(captured)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE macro_dataset_snapshots SET snapshot_fingerprint = :fp "
                "WHERE snapshot_id = :sid"
            ).bindparams(fp="0" * 64, sid=result.snapshot_id)
        )
        await session.commit()

    async with env["sessionmaker"]() as session:
        with pytest.raises(MacroSnapshotIntegrityError, match="fingerprint mismatch"):
            await service.verify_snapshot_integrity(session, result.snapshot_id)
