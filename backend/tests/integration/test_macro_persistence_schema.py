"""Integration tests for macro snapshot persistence schema (stage 2C.2A).

需要真实 PostgreSQL（127.0.0.1:5433）。覆盖：
- migration 0009 四表
- RawArtifact 媒体类型 CHECK（PDF/JSON 允许、其他拒绝）
- 各模型 FK / UNIQUE / CHECK 约束
- Artifact Link role/page 组合、response_status、NULLS NOT DISTINCT 唯一性
- NUMERIC 大数精度、value/is_missing、normalized date/year、decimal_scale
- ON DELETE CASCADE（snapshot 删除联动）/ RESTRICT（raw_artifact / provider）
- 并发 get_or_create（asyncio.gather）
- Snapshot / Observation / Artifact Link 稳定排序
- 测试数据精确清理、不访问真实网络（conftest autouse guard）、不连 Chroma、
  原始归档只写 tmp_path（不写真实 .data/raw）。

Artifact Link 只能引用 JSON RawArtifact 由 2C.2B 的 PersistenceService 保证
（不用 DB 触发器），本阶段只验证 link 表自身约束，并在 ADR-0012 记录该边界。
"""

import asyncio
import hashlib
import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.session import DatabaseManager
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_series_repository import MacroSeriesRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.storage.raw_store import LocalRawArtifactStore

pytestmark = pytest.mark.integration

configure_asyncio_runtime()

_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_JSON_BODY = (
    b'{"page": 1, "pages": 1, "per_page": 1000, "total": 1,'
    b' "rows": [{"indicator": {"id": "SP.POP.TOTL"}, "value": 123}]}'
)
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


def _provider(provider_key: str, **overrides: object) -> SourceProviderModel:
    defaults: dict = {
        "provider_key": provider_key,
        "display_name": provider_key,
        "provider_type": "international_organization",
        "authority_tier": 1,
        "homepage_url": "https://www.worldbank.org",
        "allowed_domains": ["worldbank.org"],
        "capabilities": ["macro_data"],
        "acquisition_methods": ["official_api"],
        "exchange_scope": [],
        "requires_api_key": False,
        "critical_claim_eligible": False,
        "enabled": True,
    }
    defaults.update(overrides)
    return SourceProviderModel(**defaults)


def _fingerprint() -> str:
    return uuid4().hex + uuid4().hex


def _json_bytes(page: int) -> bytes:
    return json.dumps({"page": page, "pages": 1, "rows": [{"value": page}]}).encode()


def _series(**overrides: object) -> MacroSeriesModel:
    defaults: dict = {
        "series_id": uuid4(),
        "provider_key": "world_bank",
        "source_id": "2",
        "external_indicator_id": "SP.POP.TOTL",
        "geography_type": "country",
        "geography_code": "CHN",
        "frequency": "annual",
    }
    defaults.update(overrides)
    return MacroSeriesModel(**defaults)


def _snapshot(series_id, **overrides: object) -> MacroDatasetSnapshotModel:
    defaults: dict = {
        "snapshot_id": uuid4(),
        "series_id": series_id,
        "snapshot_fingerprint": _fingerprint(),
        "requested_country_code": "CHN",
        "query_start_year": 2020,
        "query_end_year": 2024,
        "source_id_snapshot": "2",
        "indicator_name": "Population, total",
        "indicator_unit": "",
        "source_name": "World Development Indicators",
        "source_note": "",
        "source_organization": "World Bank",
        "topics_snapshot": ["economic"],
        "provider_country_id": "CHN",
        "iso2_code": "CN",
        "iso3_code": "CHN",
        "geography_name": "China",
        "region_name": None,
        "income_level_name": None,
        "page": 1,
        "pages": 1,
        "per_page": 1000,
        "provider_total": 5,
        "provider_last_updated": None,
        "fetched_at": datetime.now(UTC),
        "request_count": 3,
        "acquisition_method": "official_api",
        "authority_tier_snapshot": 1,
        "critical_claim_eligible_snapshot": True,
        "provider_capabilities_snapshot": ["macro_data"],
        "status": "available",
    }
    defaults.update(overrides)
    return MacroDatasetSnapshotModel(**defaults)


def _link(snapshot_id, artifact_id, **overrides: object) -> MacroSnapshotArtifactModel:
    defaults: dict = {
        "snapshot_artifact_id": uuid4(),
        "snapshot_id": snapshot_id,
        "artifact_id": artifact_id,
        "role": "indicator_metadata",
        "page": None,
        "response_status": 200,
        "final_hostname": "api.worldbank.org",
        "content_type": "application/json",
        "fetched_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return MacroSnapshotArtifactModel(**defaults)


def _observation(snapshot_id, period: str = "2020", **overrides: object) -> MacroObservationModel:
    defaults: dict = {
        "observation_id": uuid4(),
        "snapshot_id": snapshot_id,
        "period": period,
        "normalized_period_start": date(int(period), 1, 1),
        "period_semantics": "provider_year_label",
        "frequency": "annual",
        "value_numeric": None,
        "is_missing": True,
        "observation_status": None,
        "decimal_scale": None,
    }
    defaults.update(overrides)
    return MacroObservationModel(**defaults)


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
async def macro_env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider("world_bank"))
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "raw_root": raw_root,
        "provider_key": "world_bank",
    }
    await _cleanup(sessionmaker)


async def _create_series(env: dict, **overrides: object) -> MacroSeriesModel:
    series = _series(**overrides)
    async with env["sessionmaker"]() as session:
        repo = MacroSeriesRepository(session)
        row, _ = await repo.get_or_create(series)
        await session.commit()
        return row


async def _create_snapshot(env: dict, series_id, **overrides: object) -> MacroDatasetSnapshotModel:
    snapshot = _snapshot(series_id, **overrides)
    async with env["sessionmaker"]() as session:
        await MacroSnapshotRepository(session).create(snapshot)
        await session.commit()
        return snapshot


async def _create_artifact(env: dict, content: bytes, media_type: str) -> RawArtifactModel:
    store = env["raw_store"]
    if media_type == "application/json":
        stored = store.put_json_bytes(content)
    else:
        stored = store.put_pdf_stream(io.BytesIO(content))
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        await session.commit()
        assert artifact is not None
        return artifact


async def _assert_violation(session, model) -> None:
    session.add(model)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


# --------------------------------------------------------------- tables / ddl


@pytest.mark.asyncio
async def test_macro_persistence_tables_exist(database, sessionmaker) -> None:
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('macro_series','macro_dataset_snapshots','macro_snapshot_artifacts',"
                "'macro_observations')"
            )
        )
        tables = {row[0] for row in result}
    assert {
        "macro_series",
        "macro_dataset_snapshots",
        "macro_snapshot_artifacts",
        "macro_observations",
    } <= tables


# -------------------------------------------------- RawArtifact media type


@pytest.mark.asyncio
async def test_raw_artifact_accepts_pdf_and_json(macro_env) -> None:
    env = macro_env
    pdf = await _create_artifact(env, _PDF, "application/pdf")
    json_artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    assert pdf.media_type == "application/pdf"
    assert json_artifact.media_type == "application/json"


@pytest.mark.asyncio
async def test_raw_artifact_rejects_other_media_type(macro_env) -> None:
    env = macro_env
    async with env["sessionmaker"]() as session:
        await _assert_violation(
            session,
            RawArtifactModel(
                content_sha256="b" * 64,
                storage_key="sha256/ab/cd/y.bin",
                byte_size=10,
                media_type="application/octet-stream",
            ),
        )


# ------------------------------------------------------------- macro_series


@pytest.mark.asyncio
async def test_macro_series_requires_existing_provider(macro_env) -> None:
    env = macro_env
    async with env["sessionmaker"]() as session:
        await _assert_violation(session, _series(provider_key="ghost_provider"))


@pytest.mark.asyncio
async def test_macro_series_identity_unique(macro_env) -> None:
    env = macro_env
    created = await _create_series(env)
    # 相同五元组、不同 series_id → 唯一约束
    async with env["sessionmaker"]() as session:
        await _assert_violation(session, _series(series_id=uuid4()))
    assert created.series_id is not None


@pytest.mark.asyncio
async def test_macro_series_check_constraints(macro_env) -> None:
    env = macro_env
    async with env["sessionmaker"]() as session:
        await _assert_violation(
            session, _series(external_indicator_id="bad id!", series_id=uuid4())
        )
        await _assert_violation(session, _series(geography_code="cn", series_id=uuid4()))
        await _assert_violation(session, _series(frequency="monthly", series_id=uuid4()))


@pytest.mark.asyncio
async def test_macro_series_get_or_create_creates_then_reuses(macro_env) -> None:
    env = macro_env
    series = _series()
    async with env["sessionmaker"]() as session:
        row, created = await MacroSeriesRepository(session).get_or_create(series)
        await session.commit()
    assert created is True
    assert row.series_id == series.series_id
    # 相同身份、不同 series_id → 复用既有行
    async with env["sessionmaker"]() as session:
        row2, created2 = await MacroSeriesRepository(session).get_or_create(_series())
        await session.commit()
    assert created2 is False
    assert row2.series_id == series.series_id


@pytest.mark.asyncio
async def test_macro_series_concurrent_get_or_create_keeps_single_row(macro_env) -> None:
    env = macro_env

    async def _run(series: MacroSeriesModel) -> bool:
        async with env["sessionmaker"]() as session:
            row, created = await MacroSeriesRepository(session).get_or_create(series)
            await session.commit()
            return created

    # 相同身份、不同 series_id：并发下唯一键保证恰好一个 created=True
    results = await asyncio.gather(_run(_series()), _run(_series()))
    assert sorted(results) == [False, True]
    async with env["sessionmaker"]() as session:
        result = await session.execute(text("SELECT count(*) FROM macro_series"))
        assert result.scalar_one() == 1


# --------------------------------------------------- macro_dataset_snapshots


@pytest.mark.asyncio
async def test_macro_dataset_snapshot_requires_series(macro_env) -> None:
    env = macro_env
    async with env["sessionmaker"]() as session:
        await _assert_violation(session, _snapshot(series_id=uuid4()))


@pytest.mark.asyncio
async def test_macro_dataset_snapshot_fingerprint_unique(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    await _create_snapshot(env, series.series_id, snapshot_fingerprint="f" * 64)
    async with env["sessionmaker"]() as session:
        await _assert_violation(
            session,
            _snapshot(series.series_id, snapshot_fingerprint="f" * 64),
        )


@pytest.mark.asyncio
async def test_macro_dataset_snapshot_check_constraints(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    sid = series.series_id
    async with env["sessionmaker"]() as session:
        await _assert_violation(session, _snapshot(sid, query_start_year=1959))
        await _assert_violation(session, _snapshot(sid, query_start_year=1960, query_end_year=2030))
        await _assert_violation(session, _snapshot(sid, provider_country_id="CN"))
        # iso2_code 列长 2：用长度合法但违反大写正则的值，触发 CHECK 而非 DataError
        await _assert_violation(session, _snapshot(sid, iso2_code="c1"))
        await _assert_violation(session, _snapshot(sid, status="failed"))
        await _assert_violation(session, _snapshot(sid, request_count=0))
        await _assert_violation(session, _snapshot(sid, page=2, pages=1))
        await _assert_violation(session, _snapshot(sid, acquisition_method="web_scrape"))


@pytest.mark.asyncio
async def test_macro_dataset_snapshot_roundtrip(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    async with env["sessionmaker"]() as session:
        repo = MacroSnapshotRepository(session)
        fetched = await repo.get_by_fingerprint(snapshot.snapshot_fingerprint)
        assert fetched is not None
        assert fetched.snapshot_id == snapshot.snapshot_id
        assert fetched.query_start_year == 2020
        assert fetched.requested_country_code == "CHN"
        assert fetched.provider_capabilities_snapshot == ["macro_data"]


# ----------------------------------------------- macro_snapshot_artifacts


@pytest.mark.asyncio
async def test_macro_snapshot_artifact_role_page_check(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    sid, aid = snapshot.snapshot_id, artifact.artifact_id
    async with env["sessionmaker"]() as session:
        # observations_page 必须带 page
        await _assert_violation(session, _link(sid, aid, role="observations_page", page=None))
        # 元数据角色 page 必须为空
        await _assert_violation(session, _link(sid, aid, role="indicator_metadata", page=1))
        # role 必须在枚举内
        await _assert_violation(session, _link(sid, aid, role="metadata", page=None))


@pytest.mark.asyncio
async def test_macro_snapshot_artifact_response_status_check(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    async with env["sessionmaker"]() as session:
        await _assert_violation(
            session, _link(snapshot.snapshot_id, artifact.artifact_id, response_status=199)
        )


@pytest.mark.asyncio
async def test_macro_snapshot_artifact_role_page_unique_nulls_not_distinct(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    a1 = await _create_artifact(env, _JSON_BODY, "application/json")
    a2 = await _create_artifact(env, _json_bytes(1), "application/json")
    sid = snapshot.snapshot_id
    async with env["sessionmaker"]() as session:
        await MacroSnapshotRepository(session).add_artifact_link(
            _link(sid, a1.artifact_id, role="indicator_metadata", page=None)
        )
        await session.commit()
        # 同 snapshot 同 role（page=NULL）只能一条：NULLS NOT DISTINCT 必须生效
        await _assert_violation(
            session,
            _link(sid, a2.artifact_id, role="indicator_metadata", page=None),
        )


# -------------------------------------------------------- macro_observations


@pytest.mark.asyncio
async def test_macro_observation_numeric_precision(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    big = Decimal("123456789012345678901234567890.123456789")
    observation = _observation(
        snapshot.snapshot_id,
        value_numeric=big,
        is_missing=False,
        decimal_scale=18,
    )
    async with env["sessionmaker"]() as session:
        repo = MacroObservationRepository(session)
        assert await repo.bulk_create([observation]) == 1
        await session.commit()
    async with env["sessionmaker"]() as session:
        rows = await MacroObservationRepository(session).list_for_snapshot(snapshot.snapshot_id)
    assert len(rows) == 1
    assert rows[0].value_numeric == big
    assert rows[0].decimal_scale == 18


@pytest.mark.asyncio
async def test_macro_observation_value_is_missing_check(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    sid = snapshot.snapshot_id
    async with env["sessionmaker"]() as session:
        await _assert_violation(
            session,
            _observation(sid, value_numeric=Decimal("1.5"), is_missing=True),
        )
        await _assert_violation(
            session,
            _observation(
                sid,
                period="2021",
                value_numeric=None,
                is_missing=False,
                decimal_scale=0,
            ),
        )


@pytest.mark.asyncio
async def test_macro_observation_normalized_date_check(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    async with env["sessionmaker"]() as session:
        # period=2020 但 normalized 日期不是当年 1 月 1 日
        await _assert_violation(
            session,
            _observation(
                snapshot.snapshot_id,
                normalized_period_start=date(2020, 6, 1),
                value_numeric=Decimal("1"),
                is_missing=False,
                decimal_scale=0,
            ),
        )
        # period 必须为 4 位数字
        await _assert_violation(
            session,
            _observation(
                snapshot.snapshot_id,
                period="202",
                normalized_period_start=date(2020, 1, 1),
                value_numeric=Decimal("1"),
                is_missing=False,
                decimal_scale=0,
            ),
        )


@pytest.mark.asyncio
async def test_macro_observation_decimal_scale_checks(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    sid = snapshot.snapshot_id
    async with env["sessionmaker"]() as session:
        # 缺失值不允许带 decimal_scale
        await _assert_violation(session, _observation(sid, is_missing=True, decimal_scale=2))
        # 真实值必须带非负 decimal_scale
        await _assert_violation(
            session,
            _observation(
                sid,
                period="2021",
                value_numeric=Decimal("1"),
                is_missing=False,
                decimal_scale=None,
            ),
        )
        await _assert_violation(
            session,
            _observation(
                sid,
                period="2022",
                value_numeric=Decimal("1"),
                is_missing=False,
                decimal_scale=-1,
            ),
        )


@pytest.mark.asyncio
async def test_macro_observation_snapshot_period_unique(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    present = _observation(
        snapshot.snapshot_id,
        value_numeric=Decimal("1"),
        is_missing=False,
        decimal_scale=0,
    )
    async with env["sessionmaker"]() as session:
        await MacroObservationRepository(session).bulk_create([present])
        await session.commit()
        await _assert_violation(
            session,
            _observation(
                snapshot.snapshot_id,
                value_numeric=Decimal("2"),
                is_missing=False,
                decimal_scale=0,
            ),
        )


# ------------------------------------------------------------ ON DELETE


@pytest.mark.asyncio
async def test_macro_snapshot_artifact_cascade_on_snapshot_delete(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    async with env["sessionmaker"]() as session:
        await MacroSnapshotRepository(session).add_artifact_link(
            _link(snapshot.snapshot_id, artifact.artifact_id)
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM macro_dataset_snapshots WHERE snapshot_id = :sid"),
            {"sid": snapshot.snapshot_id},
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        result = await session.execute(text("SELECT count(*) FROM macro_snapshot_artifacts"))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_macro_snapshot_artifact_restrict_on_raw_artifact_delete(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    async with env["sessionmaker"]() as session:
        await MacroSnapshotRepository(session).add_artifact_link(
            _link(snapshot.snapshot_id, artifact.artifact_id)
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("DELETE FROM raw_artifacts WHERE artifact_id = :aid"),
                {"aid": artifact.artifact_id},
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_macro_observation_cascade_on_snapshot_delete(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    async with env["sessionmaker"]() as session:
        await MacroObservationRepository(session).bulk_create(
            [
                _observation(
                    snapshot.snapshot_id,
                    value_numeric=Decimal("123.45"),
                    is_missing=False,
                    decimal_scale=2,
                )
            ]
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM macro_dataset_snapshots WHERE snapshot_id = :sid"),
            {"sid": snapshot.snapshot_id},
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        result = await session.execute(text("SELECT count(*) FROM macro_observations"))
        assert result.scalar_one() == 0


@pytest.mark.asyncio
async def test_macro_series_restrict_on_provider_delete(macro_env) -> None:
    env = macro_env
    await _create_series(env)
    async with env["sessionmaker"]() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("DELETE FROM source_providers WHERE provider_key = 'world_bank'")
            )
        await session.rollback()


# ---------------------------------------------------------------- ordering


@pytest.mark.asyncio
async def test_macro_snapshot_list_ordering(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    old = await _create_snapshot(
        env, series.series_id, fetched_at=datetime.now(UTC) - timedelta(days=2)
    )
    new = await _create_snapshot(env, series.series_id)
    async with env["sessionmaker"]() as session:
        rows = await MacroSnapshotRepository(session).list_for_series(
            series.series_id, limit=10, offset=0
        )
    assert [r.snapshot_id for r in rows] == [new.snapshot_id, old.snapshot_id]


@pytest.mark.asyncio
async def test_macro_observation_list_ordering(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    sid = snapshot.snapshot_id
    observations = [
        _observation(
            sid,
            period="2021",
            value_numeric=Decimal("3"),
            is_missing=False,
            decimal_scale=0,
        ),
        _observation(
            sid,
            period="2019",
            value_numeric=Decimal("1"),
            is_missing=False,
            decimal_scale=0,
        ),
        _observation(
            sid,
            period="2020",
            value_numeric=Decimal("2"),
            is_missing=False,
            decimal_scale=0,
        ),
    ]
    async with env["sessionmaker"]() as session:
        await MacroObservationRepository(session).bulk_create(observations)
        await session.commit()
    async with env["sessionmaker"]() as session:
        rows = await MacroObservationRepository(session).list_for_snapshot(sid)
    assert [r.period for r in rows] == ["2019", "2020", "2021"]


@pytest.mark.asyncio
async def test_macro_artifact_link_ordering(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    sid = snapshot.snapshot_id
    a1 = await _create_artifact(env, _json_bytes(1), "application/json")
    a2 = await _create_artifact(env, _json_bytes(2), "application/json")
    a3 = await _create_artifact(env, _json_bytes(3), "application/json")
    a4 = await _create_artifact(env, _json_bytes(4), "application/json")
    async with env["sessionmaker"]() as session:
        repo = MacroSnapshotRepository(session)
        await repo.add_artifact_link(_link(sid, a1.artifact_id, role="observations_page", page=2))
        await repo.add_artifact_link(_link(sid, a2.artifact_id, role="country_metadata", page=None))
        await repo.add_artifact_link(_link(sid, a3.artifact_id, role="observations_page", page=1))
        await repo.add_artifact_link(
            _link(sid, a4.artifact_id, role="indicator_metadata", page=None)
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        rows = await MacroSnapshotRepository(session).list_artifact_links(sid)
    assert [(r.role, r.page) for r in rows] == [
        ("country_metadata", None),
        ("indicator_metadata", None),
        ("observations_page", 1),
        ("observations_page", 2),
    ]


# -------------------------------------------------- store + DB integration


@pytest.mark.asyncio
async def test_macro_json_store_and_artifact_roundtrip(macro_env) -> None:
    env = macro_env
    artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    # store 落盘在 tmp_path，不在真实 .data/raw；按原始字节读回
    stored_key = artifact.storage_key
    assert env["raw_store"].exists(stored_key)
    with env["raw_store"].open(stored_key) as handle:
        assert handle.read() == _JSON_BODY
    # raw_artifacts 表记录与文件哈希一致
    async with env["sessionmaker"]() as session:
        fetched = await RawArtifactRepository(session).get_by_id(artifact.artifact_id)
        assert fetched is not None
        assert fetched.content_sha256 == hashlib.sha256(_JSON_BODY).hexdigest()


# ---------------------------------------------------------- exact cleanup


@pytest.mark.asyncio
async def test_cleanup_removes_all_macro_and_artifact_data(macro_env) -> None:
    env = macro_env
    series = await _create_series(env)
    snapshot = await _create_snapshot(env, series.series_id)
    artifact = await _create_artifact(env, _JSON_BODY, "application/json")
    async with env["sessionmaker"]() as session:
        repo = MacroSnapshotRepository(session)
        await repo.add_artifact_link(_link(snapshot.snapshot_id, artifact.artifact_id))
        await MacroObservationRepository(session).bulk_create(
            [
                _observation(
                    snapshot.snapshot_id,
                    value_numeric=Decimal("1"),
                    is_missing=False,
                    decimal_scale=0,
                )
            ]
        )
        await session.commit()
    await _cleanup(env["sessionmaker"])
    async with env["sessionmaker"]() as session:
        for table in (
            "macro_observations",
            "macro_snapshot_artifacts",
            "macro_dataset_snapshots",
            "macro_series",
            "raw_artifacts",
        ):
            result = await session.execute(text(f"SELECT count(*) FROM {table}"))
            assert result.scalar_one() == 0, f"{table} 未清空"
