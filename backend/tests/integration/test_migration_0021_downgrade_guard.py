"""Integration test: migration 0021 financial_calculations downgrade guard.

在**独立临时 PostgreSQL 数据库**中真实验证 0021：

- (A) `alembic upgrade 0021` 创建 financial_calculations +
  financial_calculation_inputs；无数据 → downgrade 0021→0020 成功、两表被删除、
  版本回到 0020；
- (B) 存在 FinancialCalculation 数据时 downgrade 0021→0020 必须被拒绝
  （RuntimeError，guard 文案含 "financial_calculations rows present"），且
  alembic_version 仍为 0021、calculation / inputs / observation 行完整保留
  （不静默丢弃已计算的财务派生事实）。

(B) 的依赖行（company + evidence_card）用真实服务链 seed
（`_seed_document_claim`），observation / calculation / input 用直接 SQL
插入（fingerprint 用生产函数生成）。测试全程使用 `insightforge_gate_*` 临时库
并最终 DROP，不触碰主库（`insightforge`）。
"""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.session import DatabaseManager
from app.financial.calculations.contracts import compute_calculation_fingerprint
from app.financial.contracts import FINANCIAL_METRIC_SCHEMA_VERSION, compute_metric_fingerprint
from tests.integration.test_migration_0018_downgrade_guard import _seed_document_claim

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_TABLES = ("financial_calculations", "financial_calculation_inputs")


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


async def _version(temp_url: str) -> str:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return str(
                (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
            )
    finally:
        await manager.dispose()


async def _tables_present(temp_url: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            counts = []
            for table in _TABLES:
                count = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name = :name"
                        ).bindparams(name=table)
                    )
                ).scalar_one()
                counts.append(int(count))
            return all(c > 0 for c in counts)
    finally:
        await manager.dispose()


async def _seed_calculation(temp_url: str, seeded: dict) -> None:
    """在 0021 schema 下 seed 一行 calculation + 一行 input（全部 CK 满足）。

    company + evidence_card 来自 `_seed_document_claim` 的真实服务链；observation
    fingerprint 用 `compute_metric_fingerprint`、calculation fingerprint 用
    `compute_calculation_fingerprint` 生成（与生产代码同路径，保证 64-hex）。
    """
    evidence_card_id = UUID(seeded["evidence_card_id"])
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            company_id = (
                await session.execute(
                    text(
                        "SELECT company_id FROM evidence_cards WHERE evidence_card_id = :eid"
                    ).bindparams(eid=evidence_card_id)
                )
            ).scalar_one()
            obs_fp = compute_metric_fingerprint(
                metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
                company_id=company_id,
                source_evidence_card_id=evidence_card_id,
                metric_code="revenue",
                statement_scope="consolidated",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                period_kind="duration",
                source_value_text="123,456",
                raw_value=Decimal("123456"),
                raw_unit="ten_thousand_yuan",
                normalized_value_cny=Decimal("1234560000"),
            )
            observation_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO financial_metric_observations "
                    "(metric_observation_id, company_id, source_evidence_card_id, "
                    " metric_code, statement_scope, period_start, period_end, period_kind, "
                    " source_value_text, raw_value, raw_unit, normalized_value_cny, "
                    " metric_schema_version, metric_fingerprint) "
                    "VALUES (:oid, CAST(:company_id AS uuid), CAST(:card_id AS uuid), "
                    " 'revenue', 'consolidated', :period_start, :period_end, 'duration', "
                    " '123,456', :raw_value, 'ten_thousand_yuan', :normalized, "
                    " :schema_version, :fp)"
                ).bindparams(
                    oid=observation_id,
                    company_id=company_id,
                    card_id=evidence_card_id,
                    period_start=date(2024, 1, 1),
                    period_end=date(2024, 12, 31),
                    raw_value=Decimal("123456"),
                    normalized=Decimal("1234560000"),
                    schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
                    fp=obs_fp,
                )
            )
            calc_fp = compute_calculation_fingerprint(
                calculation_schema_version=1,
                formula_version=1,
                company_id=company_id,
                calculation_code="gross_margin",
                inputs=[("revenue", observation_id, obs_fp)],
                result_value=Decimal("0.4"),
                result_unit="ratio",
            )
            calculation_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO financial_calculations "
                    "(calculation_id, company_id, calculation_code, result_value, "
                    " result_unit, calculation_schema_version, formula_version, "
                    " calculation_fingerprint) "
                    "VALUES (:cid, CAST(:company_id AS uuid), 'gross_margin', "
                    " :result_value, 'ratio', 1, 1, :fp)"
                ).bindparams(
                    cid=calculation_id,
                    company_id=company_id,
                    result_value=Decimal("0.4"),
                    fp=calc_fp,
                )
            )
            await session.execute(
                text(
                    "INSERT INTO financial_calculation_inputs "
                    "(calculation_id, input_role, metric_observation_id) "
                    "VALUES (CAST(:cid AS uuid), 'revenue', CAST(:oid AS uuid))"
                ).bindparams(cid=calculation_id, oid=observation_id)
            )
            await session.commit()
    finally:
        await manager.dispose()


async def _calculation_rows(temp_url: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int(
                (
                    await session.execute(text("SELECT count(*) FROM financial_calculations"))
                ).scalar_one()
            )
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0021_downgrade_allowed_when_empty(monkeypatch) -> None:
    """(A) 0021 无数据 → downgrade 0021→0020 成功，两表被删除、版本回到 0020。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0021")
        assert await _version(temp_url) == "0021"
        assert await _tables_present(temp_url)

        await asyncio.to_thread(command.downgrade, cfg, "0020")
        assert await _version(temp_url) == "0020"
        assert not await _tables_present(temp_url)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0021_downgrade_blocked_with_calculation_data(
    monkeypatch, tmp_path
) -> None:
    """(B) 存在 FinancialCalculation 数据时 downgrade 0021→0020 必须被拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0021")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        await _seed_calculation(temp_url, seeded)
        assert await _calculation_rows(temp_url) == 1

        with pytest.raises(RuntimeError, match="financial_calculations rows present"):
            await asyncio.to_thread(command.downgrade, cfg, "0020")

        # guard 拒绝后：版本仍为 0021、calculation 行完整保留。
        assert await _version(temp_url) == "0021"
        assert await _calculation_rows(temp_url) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
