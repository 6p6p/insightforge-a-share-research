"""FinancialCalculationService integration tests (stage 4B.2B, spec Q).

需要真实 PostgreSQL（127.0.0.1:5433）。Observation 行用
`compute_metric_fingerprint` 生成 64-hex 指纹直接插入（满足全部 CK 约束，
镜像 migration 0020 guard 的 seed 模式）；company / evidence_card 用真实
`_seed_document_claim` 服务链。**零 Chroma / 零 LLM / 零 Claim 生成 / 零 Report
/ 零 Audit**。

覆盖：
- 创建：absolute_change / yoy / margin → result_value / result_unit / inputs
  绑定（Calculation → Observation → EvidenceCard → Source）；
- replay：同 fingerprint 复用同一行 / 并发 → 1；输入值变化 → 新 calculation，
  旧行保留；
- integrity：篡改 result_value → FinancialCalculationIntegrityError，**不自动
  repair**；
- 拒绝：observation 缺失 / company 不一致 / scope 不一致 / metric_code 不匹配 /
  period 不匹配 / baseline 非正 / 分母为 0 / result 超 NUMERIC(38,12)；
- FK 行为：删除 calculation 级联删除 inputs；
- 边界：不创建 Claim / Report；Service 只持有 sessionmaker。
"""

import asyncio
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.session import DatabaseManager
from app.financial.calculations.contracts import (
    CalculationCode,
    FinancialCalculationDraft,
    InputRole,
)
from app.financial.calculations.errors import (
    FinancialCalculationCompanyMismatch,
    FinancialCalculationGrowthBaseNotPositive,
    FinancialCalculationInputMismatch,
    FinancialCalculationIntegrityError,
    FinancialCalculationObservationNotFound,
    FinancialCalculationPeriodMismatch,
    FinancialCalculationScopeMismatch,
    FinancialCalculationStorageRangeError,
    FinancialCalculationZeroDenominator,
)
from app.financial.calculations.service import FinancialCalculationService
from app.financial.contracts import FINANCIAL_METRIC_SCHEMA_VERSION, compute_metric_fingerprint
from tests.integration.test_migration_0018_downgrade_guard import _seed_document_claim

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


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
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
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
    await _cleanup(sessionmaker)
    seeded = await _seed_document_claim(get_settings().database_url, tmp_path / "raw")
    card_id = UUID(seeded["evidence_card_id"])
    async with sessionmaker() as session:
        company_id = (
            await session.execute(
                text(
                    "SELECT company_id FROM evidence_cards WHERE evidence_card_id = :eid"
                ).bindparams(eid=card_id)
            )
        ).scalar_one()
    yield {
        "sessionmaker": sessionmaker,
        "company_id": company_id,
        "evidence_card_id": card_id,
    }
    await _cleanup(sessionmaker)


async def _insert_observation(
    env: dict,
    *,
    metric_code: str,
    scope: str = "consolidated",
    period_start: date | None,
    period_end: date,
    period_kind: str,
    normalized: str,
    raw_unit: str = "yuan",
) -> UUID:
    """直接插入一行满足全部 CK 约束的 FinancialMetricObservation（fingerprint 用
    生产函数生成；镜像 migration 0020 guard 的 seed 模式）。"""
    company_id = env["company_id"]
    card_id = env["evidence_card_id"]
    fingerprint = compute_metric_fingerprint(
        metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
        company_id=company_id,
        source_evidence_card_id=card_id,
        metric_code=metric_code,
        statement_scope=scope,
        period_start=period_start,
        period_end=period_end,
        period_kind=period_kind,
        source_value_text="123",
        raw_value=Decimal(normalized),
        raw_unit=raw_unit,
        normalized_value_cny=Decimal(normalized),
    )
    obs_id = uuid4()
    async with env["sessionmaker"]() as session:
        session.add(
            FinancialMetricObservationModel(
                metric_observation_id=obs_id,
                company_id=company_id,
                source_evidence_card_id=card_id,
                metric_code=metric_code,
                statement_scope=scope,
                period_start=period_start,
                period_end=period_end,
                period_kind=period_kind,
                source_value_text="123",
                raw_value=Decimal(normalized),
                raw_unit=raw_unit,
                normalized_value_cny=Decimal(normalized),
                metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
                metric_fingerprint=fingerprint,
            )
        )
        await session.commit()
    return obs_id


async def _annual_revenue_pair(env: dict) -> dict:
    """2024 / 2023 全年营收观察（duration，consecutive annual）。"""
    current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="12000000000",
    )
    baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="10000000000",
    )
    return {InputRole.CURRENT: current, InputRole.BASELINE: baseline}


def _draft(company_id: UUID, code: CalculationCode, obs: dict) -> FinancialCalculationDraft:
    return FinancialCalculationDraft(
        company_id=company_id,
        calculation_code=code,
        input_observation_ids={role: oid for role, oid in obs.items()},
    )


async def _calc_rows(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM financial_calculations"))
            ).scalar_one()
        )


async def _input_rows(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM financial_calculation_inputs"))
            ).scalar_one()
        )


async def _result(env: dict, calculation_id: UUID) -> tuple[Decimal, str]:
    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                text(
                    "SELECT result_value, result_unit FROM financial_calculations "
                    "WHERE calculation_id = :cid"
                ).bindparams(cid=calculation_id)
            )
        ).one()
        return Decimal(str(row[0])), str(row[1])


# ---------------------------------------------------------------- 创建


async def test_absolute_change_cny(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    result = await service.create_calculation(
        _draft(env["company_id"], CalculationCode.ABSOLUTE_CHANGE_CNY, obs)
    )

    assert result.replayed is False
    value, unit = await _result(env, result.calculation_id)
    assert value == Decimal("2000000000")
    assert unit == "cny"
    assert await _calc_rows(env["sessionmaker"]) == 1
    assert await _input_rows(env["sessionmaker"]) == 2


async def test_yoy_growth_rate_ratio_form(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    result = await service.create_calculation(
        _draft(env["company_id"], CalculationCode.YOY_GROWTH_RATE, obs)
    )

    value, unit = await _result(env, result.calculation_id)
    assert value == Decimal("0.2")  # 存 0.2，不存 20
    assert unit == "ratio"


async def test_gross_margin(env) -> None:
    revenue = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="10000000000",
    )
    operating_cost = await _insert_observation(
        env,
        metric_code="operating_cost",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="6000000000",
    )
    service = FinancialCalculationService(env["sessionmaker"])
    result = await service.create_calculation(
        _draft(
            env["company_id"],
            CalculationCode.GROSS_MARGIN,
            {InputRole.REVENUE: revenue, InputRole.OPERATING_COST: operating_cost},
        )
    )
    value, unit = await _result(env, result.calculation_id)
    assert value == Decimal("0.4")
    assert unit == "ratio"


async def test_inputs_bound_to_observations(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    result = await service.create_calculation(
        _draft(env["company_id"], CalculationCode.ABSOLUTE_CHANGE_CNY, obs)
    )
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT input_role, metric_observation_id "
                    "FROM financial_calculation_inputs "
                    "WHERE calculation_id = :cid ORDER BY input_role"
                ).bindparams(cid=result.calculation_id)
            )
        ).all()
    assert {r[0]: UUID(str(r[1])) for r in rows} == dict(obs)


# ---------------------------------------------------------------- replay


async def test_replay_same_draft_returns_same_row(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    draft = _draft(env["company_id"], CalculationCode.YOY_GROWTH_RATE, obs)

    first = await service.create_calculation(draft)
    second = await service.create_calculation(draft)

    assert first.replayed is False
    assert second.replayed is True
    assert second.calculation_id == first.calculation_id
    assert second.calculation_fingerprint == first.calculation_fingerprint
    assert await _calc_rows(env["sessionmaker"]) == 1
    assert await _input_rows(env["sessionmaker"]) == 2


async def test_input_change_creates_new_calculation_keeps_old(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    draft = _draft(env["company_id"], CalculationCode.ABSOLUTE_CHANGE_CNY, obs)
    first = await service.create_calculation(draft)

    # baseline 数值变化（新 observation 行）→ 新 fingerprint → 新 calculation。
    new_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="9000000000",
    )
    changed = {**obs, InputRole.BASELINE: new_baseline}
    second = await service.create_calculation(
        _draft(env["company_id"], CalculationCode.ABSOLUTE_CHANGE_CNY, changed)
    )

    assert second.replayed is False
    assert second.calculation_id != first.calculation_id
    assert await _calc_rows(env["sessionmaker"]) == 2
    assert await _input_rows(env["sessionmaker"]) == 4


async def test_concurrent_create_ends_with_one_row(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    draft = _draft(env["company_id"], CalculationCode.YOY_GROWTH_RATE, obs)

    first, second = await asyncio.gather(
        service.create_calculation(draft), service.create_calculation(draft)
    )

    assert await _calc_rows(env["sessionmaker"]) == 1
    assert await _input_rows(env["sessionmaker"]) == 2
    assert first.calculation_id == second.calculation_id
    assert sorted([first.replayed, second.replayed]) == [False, True]


# ---------------------------------------------------------------- integrity


async def test_tampered_result_replay_integrity_error_no_repair(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    draft = _draft(env["company_id"], CalculationCode.ABSOLUTE_CHANGE_CNY, obs)
    result = await service.create_calculation(draft)

    # 篡改既有行的 result_value。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_calculations SET result_value = 1 WHERE calculation_id = :cid"
            ).bindparams(cid=result.calculation_id)
        )
        await session.commit()

    with pytest.raises(FinancialCalculationIntegrityError):
        await service.create_calculation(draft)

    # 不自动 repair：篡改值仍在。
    value, _ = await _result(env, result.calculation_id)
    assert value == Decimal("1")


# ---------------------------------------------------------------- 拒绝


async def test_observation_not_found(env) -> None:
    service = FinancialCalculationService(env["sessionmaker"])
    draft = FinancialCalculationDraft(
        company_id=env["company_id"],
        calculation_code=CalculationCode.ABSOLUTE_CHANGE_CNY,
        input_observation_ids={InputRole.CURRENT: uuid4(), InputRole.BASELINE: uuid4()},
    )
    with pytest.raises(FinancialCalculationObservationNotFound):
        await service.create_calculation(draft)


async def test_company_mismatch(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationCompanyMismatch):
        await service.create_calculation(_draft(uuid4(), CalculationCode.YOY_GROWTH_RATE, obs))


async def test_scope_mismatch(env) -> None:
    obs = await _annual_revenue_pair(env)
    parent_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        scope="parent",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="10000000000",
    )
    obs[InputRole.BASELINE] = parent_baseline
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationScopeMismatch):
        await service.create_calculation(
            _draft(env["company_id"], CalculationCode.YOY_GROWTH_RATE, obs)
        )


async def test_input_metric_code_mismatch(env) -> None:
    revenue = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="10000000000",
    )
    wrong_cost = await _insert_observation(
        env,
        metric_code="operating_profit",  # 期望 operating_cost
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="6000000000",
    )
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationInputMismatch):
        await service.create_calculation(
            _draft(
                env["company_id"],
                CalculationCode.GROSS_MARGIN,
                {InputRole.REVENUE: revenue, InputRole.OPERATING_COST: wrong_cost},
            )
        )


async def test_yoy_period_mismatch_wrong_year(env) -> None:
    current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="12000000000",
    )
    baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2022, 1, 1),
        period_end=date(2022, 12, 31),
        period_kind="duration",
        normalized="10000000000",
    )
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationPeriodMismatch):
        await service.create_calculation(
            _draft(
                env["company_id"],
                CalculationCode.YOY_GROWTH_RATE,
                {InputRole.CURRENT: current, InputRole.BASELINE: baseline},
            )
        )


async def test_growth_base_not_positive(env) -> None:
    obs = await _annual_revenue_pair(env)
    zero_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="0",
    )
    obs[InputRole.BASELINE] = zero_baseline
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationGrowthBaseNotPositive):
        await service.create_calculation(
            _draft(env["company_id"], CalculationCode.YOY_GROWTH_RATE, obs)
        )


async def test_zero_denominator(env) -> None:
    zero_revenue = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="0",
    )
    operating_cost = await _insert_observation(
        env,
        metric_code="operating_cost",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="100",
    )
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationZeroDenominator):
        await service.create_calculation(
            _draft(
                env["company_id"],
                CalculationCode.GROSS_MARGIN,
                {InputRole.REVENUE: zero_revenue, InputRole.OPERATING_COST: operating_cost},
            )
        )


async def test_result_storage_range_rejected(env) -> None:
    current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="99999999999999999999999999",
    )
    baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="-99999999999999999999999999",
    )
    service = FinancialCalculationService(env["sessionmaker"])
    with pytest.raises(FinancialCalculationStorageRangeError):
        await service.create_calculation(
            _draft(
                env["company_id"],
                CalculationCode.ABSOLUTE_CHANGE_CNY,
                {InputRole.CURRENT: current, InputRole.BASELINE: baseline},
            )
        )


# ---------------------------------------------------------------- FK 行为


async def test_delete_calculation_cascades_inputs(env) -> None:
    obs = await _annual_revenue_pair(env)
    service = FinancialCalculationService(env["sessionmaker"])
    result = await service.create_calculation(
        _draft(env["company_id"], CalculationCode.ABSOLUTE_CHANGE_CNY, obs)
    )
    assert await _input_rows(env["sessionmaker"]) == 2

    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM financial_calculations WHERE calculation_id = :cid").bindparams(
                cid=result.calculation_id
            )
        )
        await session.commit()

    assert await _calc_rows(env["sessionmaker"]) == 0
    assert await _input_rows(env["sessionmaker"]) == 0
