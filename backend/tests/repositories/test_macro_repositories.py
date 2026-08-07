"""Unit tests for macro repositories (stage 2C.2A).

真实 SQL 行为（ON CONFLICT 并发去重、稳定排序、FK/UNIQUE/CHECK 约束强制）
由集成测试 test_macro_persistence_schema.py 覆盖；这里用桩 session 验证
方法契约：不 commit、空输入短路、add/flush 交互、查询返回类型。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_series_repository import MacroSeriesRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, *, row=None, rows=None, count=None) -> None:
        self._row = row
        self._rows = rows
        self._count = count

    def scalar_one(self):
        if self._count is not None:
            return self._count
        if self._row is None:
            raise AssertionError("expected a row, got none")
        return self._row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        if self._rows is not None:
            return self._rows
        return [] if self._row is None else [self._row]


class _SessionStub:
    def __init__(self, result: _Result | None = None) -> None:
        self.result = result or _Result()
        self.added: list = []
        self.add_all_calls: list = []
        self.execute_calls: list = []
        self.commit_calls = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    def add_all(self, objs) -> None:
        self.add_all_calls.append(list(objs))

    async def flush(self) -> None:
        pass

    async def execute(self, stmt):
        self.execute_calls.append(stmt)
        return self.result

    def commit(self) -> None:
        self.commit_calls += 1


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


def _snapshot(**overrides: object) -> MacroDatasetSnapshotModel:
    defaults: dict = {
        "snapshot_id": uuid4(),
        "series_id": uuid4(),
        "snapshot_fingerprint": "a" * 64,
        "requested_country_code": "CHN",
        "query_start_year": 2020,
        "query_end_year": 2024,
        "source_id_snapshot": "2",
        "indicator_name": "Population, total",
        "indicator_unit": "",
        "source_name": "World Development Indicators",
        "source_note": "",
        "source_organization": "",
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


def _link(**overrides: object) -> MacroSnapshotArtifactModel:
    defaults: dict = {
        "snapshot_artifact_id": uuid4(),
        "snapshot_id": uuid4(),
        "artifact_id": uuid4(),
        "role": "indicator_metadata",
        "page": None,
        "response_status": 200,
        "final_hostname": "api.worldbank.org",
        "content_type": "application/json",
        "fetched_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return MacroSnapshotArtifactModel(**defaults)


def _observation(**overrides: object) -> MacroObservationModel:
    defaults: dict = {
        "observation_id": uuid4(),
        "snapshot_id": uuid4(),
        "period": "2020",
        "normalized_period_start": None,  # date 由 SQLAlchemy 在 flush 时不需要；仅结构测试
        "period_semantics": "provider_year_label",
        "frequency": "annual",
        "value_numeric": None,
        "is_missing": True,
        "observation_status": None,
        "decimal_scale": None,
    }
    defaults.update(overrides)
    return MacroObservationModel(**defaults)


# -------------------------------------------------------- macro_series repo


async def test_series_create_adds_and_flushes() -> None:
    session = _SessionStub()
    repo = MacroSeriesRepository(session)
    series = _series()
    result = await repo.create(series)
    assert result is series
    assert session.added == [series]
    assert len(session.execute_calls) == 0


async def test_series_find_by_identity_queries_and_returns_none() -> None:
    session = _SessionStub(_Result(row=None))
    repo = MacroSeriesRepository(session)
    found = await repo.find_by_identity(
        provider_key="world_bank",
        source_id="2",
        external_indicator_id="SP.POP.TOTL",
        geography_type="country",
        geography_code="CHN",
        frequency="annual",
    )
    assert found is None
    assert len(session.execute_calls) == 1


async def test_series_get_by_id_returns_none_for_empty() -> None:
    repo = MacroSeriesRepository(_SessionStub(_Result(row=None)))
    assert await repo.get_by_id(uuid4()) is None


async def test_series_get_or_create_returns_created_flag() -> None:
    series = _series()
    # 返回行即刚插入的行（series_id 一致）→ created=True
    session = _SessionStub(_Result(row=series))
    repo = MacroSeriesRepository(session)
    row, created = await repo.get_or_create(series)
    assert created is True
    assert row.series_id == series.series_id


async def test_series_get_or_create_reuses_existing_row() -> None:
    series = _series()
    existing = _series()  # 不同 series_id → 复用既有行
    session = _SessionStub(_Result(row=existing))
    repo = MacroSeriesRepository(session)
    row, created = await repo.get_or_create(series)
    assert created is False
    assert row.series_id == existing.series_id
    assert row.series_id != series.series_id


async def test_series_repository_never_commits() -> None:
    session = _SessionStub(_Result(row=_series()))
    repo = MacroSeriesRepository(session)
    await repo.find_by_identity(
        provider_key="world_bank",
        source_id="2",
        external_indicator_id="SP.POP.TOTL",
        geography_type="country",
        geography_code="CHN",
        frequency="annual",
    )
    await repo.get_or_create(_series())
    assert session.commit_calls == 0


# ----------------------------------------------------- macro_snapshot repo


async def test_snapshot_create_adds_and_flushes() -> None:
    session = _SessionStub()
    repo = MacroSnapshotRepository(session)
    snapshot = _snapshot()
    result = await repo.create(snapshot)
    assert result is snapshot
    assert session.added == [snapshot]


async def test_snapshot_add_artifact_link_adds_and_flushes() -> None:
    session = _SessionStub()
    repo = MacroSnapshotRepository(session)
    link = _link()
    result = await repo.add_artifact_link(link)
    assert result is link
    assert session.added == [link]


async def test_snapshot_get_by_fingerprint_queries() -> None:
    session = _SessionStub(_Result(row=None))
    repo = MacroSnapshotRepository(session)
    assert await repo.get_by_fingerprint("b" * 64) is None
    assert len(session.execute_calls) == 1


async def test_snapshot_list_for_series_returns_empty_list() -> None:
    repo = MacroSnapshotRepository(_SessionStub(_Result(rows=[])))
    assert await repo.list_for_series(uuid4(), limit=10, offset=0) == []


async def test_snapshot_count_for_series_returns_zero() -> None:
    repo = MacroSnapshotRepository(_SessionStub(_Result(count=0)))
    assert await repo.count_for_series(uuid4()) == 0


async def test_snapshot_list_artifact_links_returns_empty_list() -> None:
    repo = MacroSnapshotRepository(_SessionStub(_Result(rows=[])))
    assert await repo.list_artifact_links(uuid4()) == []


async def test_snapshot_repository_never_commits() -> None:
    # count=0 同时满足 list（all()→[]）与 count（scalar_one()→0）
    session = _SessionStub(_Result(count=0))
    repo = MacroSnapshotRepository(session)
    await repo.list_for_series(uuid4(), limit=5, offset=0)
    await repo.count_for_series(uuid4())
    await repo.add_artifact_link(_link())
    assert session.commit_calls == 0


# -------------------------------------------------- macro_observation repo


async def test_observation_bulk_create_empty_returns_zero() -> None:
    session = _SessionStub()
    repo = MacroObservationRepository(session)
    assert await repo.bulk_create([]) == 0
    assert session.add_all_calls == []


async def test_observation_bulk_create_returns_count() -> None:
    session = _SessionStub()
    repo = MacroObservationRepository(session)
    observations = [_observation(period="2020"), _observation(period="2021")]
    assert await repo.bulk_create(observations) == 2
    assert session.add_all_calls == [observations]


async def test_observation_list_for_snapshot_returns_empty_list() -> None:
    repo = MacroObservationRepository(_SessionStub(_Result(rows=[])))
    assert await repo.list_for_snapshot(uuid4()) == []


async def test_observation_count_for_snapshot_returns_zero() -> None:
    repo = MacroObservationRepository(_SessionStub(_Result(count=0)))
    assert await repo.count_for_snapshot(uuid4()) == 0


async def test_observation_repository_never_commits() -> None:
    session = _SessionStub(_Result(count=0))
    repo = MacroObservationRepository(session)
    await repo.count_for_snapshot(uuid4())
    await repo.bulk_create([])
    assert session.commit_calls == 0
