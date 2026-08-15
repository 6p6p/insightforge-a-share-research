"""Integration regression: Company Master bootstrap self-heal (V1.1 一致性修复).

覆盖真实 production bug：snapshot marker 存在但 companies/company_aliases 为空
时，bootstrap 错误返回 `replayed=true` 且不自愈。修复后规则（marker 与**实际
数据状态**联合判定）：

- Case A：marker 不存在 + 数据缺失 → 首次导入 + 记录 marker；
- Case B：marker 存在 + 数据完整 → replay（0 写）；
- Case C：marker 存在 + 数据缺失（空表）→ **一致性恢复**（repair=True，
  re-import insert-only，marker 不重复）；
- Case D：数据非空（即使与 snapshot count 不一致）→ 尊重现有数据：
  不 DELETE、不覆盖已有 CompanyIdentity、自定义行保留。

在**独立临时 PostgreSQL 数据库**中验证；registry 按生产顺序 seed；全程不触碰
主库，finally 恢复 settings 缓存并 DROP 临时库。
"""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.companies.master.snapshot import load_bundled_snapshot
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.services.company_master_service import CompanyMasterBootstrapService
from app.services.source_registry_service import SourceRegistryService

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


@pytest_asyncio.fixture(scope="module")
async def env(tmp_path_factory):
    """临时 fresh DB：alembic head + registry seed（生产顺序）。"""
    settings = get_settings()
    shared = settings.database_url
    temp_db = f"insightforge_master_{uuid4().hex[:10]}"
    temp_url = shared.rsplit("/", 1)[0] + f"/{temp_db}"
    parts = _parse_db_url(shared)
    with psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname="postgres",
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{temp_db}"')
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = temp_url
    get_settings.cache_clear()
    try:
        await asyncio.to_thread(command.upgrade, Config(str(ALEMBIC_INI)), "head")
    finally:
        if previous_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_env
        get_settings.cache_clear()

    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=10)
    try:
        sessionmaker = manager.session_factory()
        await SourceRegistryService(sessionmaker).seed_defaults()
        yield {"sessionmaker": sessionmaker}
    finally:
        await manager.dispose()
        with psycopg.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"],
            dbname="postgres",
            autocommit=True,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE)')


async def _counts(sessionmaker) -> tuple[int, int, int]:
    async with sessionmaker() as session:
        companies = int(
            (await session.execute(text("SELECT count(*) FROM companies"))).scalar_one()
        )
        aliases = int(
            (await session.execute(text("SELECT count(*) FROM company_aliases"))).scalar_one()
        )
        markers = int(
            (
                await session.execute(text("SELECT count(*) FROM company_master_snapshots"))
            ).scalar_one()
        )
    return companies, aliases, markers


async def _wipe_master(sessionmaker) -> None:
    """清空 master 数据但**保留 marker**（模拟真实 bug 的 inconsistent state）。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


async def _wipe_everything(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.execute(text("DELETE FROM company_master_snapshots"))
        await session.commit()


async def _insert_custom_company(sessionmaker, *, code: str = "999999") -> None:
    """插入一家**非 snapshot** 的自定义公司（模拟用户合法数据，provider sse 已 seed）。"""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO companies (company_id, exchange, security_code, identity_key, "
                "board, official_name, short_name, listing_status, "
                "identity_source_provider_key, identity_source_url) "
                "VALUES (:id, 'SSE', :code, 'SSE:' || :code, 'sse_main', :name, :short, "
                "'listed', 'sse', 'https://www.sse.com.cn')"
            ).bindparams(
                id=uuid4(),
                code=code,
                name=f"自定义测试公司{code}",
                short=f"测试{code}",
            )
        )
        await session.commit()


# ================================================================== Case A


async def test_first_startup_imports_and_records_marker(env) -> None:
    """Case A：marker 不存在 + 空表 → 首次导入 → 5543 / aliases / marker=1。"""
    sessionmaker = env["sessionmaker"]
    await _wipe_everything(sessionmaker)
    result = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    assert result.replayed is False
    assert result.repair is False
    assert result.imported_companies > 5000
    companies, aliases, markers = await _counts(sessionmaker)
    assert companies > 5000
    assert aliases > companies
    assert markers == 1


# ================================================================== Case B


async def test_marker_with_complete_data_replays_without_writes(env) -> None:
    """Case B：marker 存在 + 数据完整 → replay（0 写）。"""
    sessionmaker = env["sessionmaker"]
    await _wipe_everything(sessionmaker)
    await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    companies, aliases, markers = await _counts(sessionmaker)

    result = await CompanyMasterBootstrapService(sessionmaker).import_snapshot(
        load_bundled_snapshot()
    )
    assert result.replayed is True
    assert result.repair is False
    assert result.imported_companies == 0
    after = await _counts(sessionmaker)
    assert after == (companies, aliases, markers)  # 0 duplicate writes


# ================================================================== Case C（核心回归）


async def test_marker_with_empty_master_self_heals(env) -> None:
    """Case C：marker 存在 + 空表（真实 bug 场景）→ 自动恢复，不再 replay。"""
    sessionmaker = env["sessionmaker"]
    await _wipe_everything(sessionmaker)
    await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    companies, aliases, markers = await _counts(sessionmaker)
    assert companies > 5000 and markers == 1

    # 模拟 production bug：marker 保留，但 master 数据被清空。
    await _wipe_master(sessionmaker)
    empty = await _counts(sessionmaker)
    assert empty == (0, 0, 1)

    result = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    assert result.replayed is False
    assert result.repair is True
    assert result.imported_companies > 5000
    recovered = await _counts(sessionmaker)
    assert recovered[0] > 5000  # companies 恢复
    assert recovered[1] > recovered[0]  # aliases 恢复
    assert recovered[2] == 1  # marker 不重复


# ================================================================== 重复启动


async def test_repeated_bootstrap_is_idempotent(env) -> None:
    """重复启动：数据完整时 bootstrap → skip；再次 import → replay。"""
    sessionmaker = env["sessionmaker"]
    await _wipe_everything(sessionmaker)
    await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    companies, aliases, markers = await _counts(sessionmaker)

    second = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    assert second.skipped is True
    assert second.repair is False
    assert await _counts(sessionmaker) == (companies, aliases, markers)


# ================================================================== Case D


async def test_existing_legal_identities_not_overwritten(env) -> None:
    """Case D：已有合法 company 数据不被 DELETE / 覆盖。

    - 非空部分数据（即使 count 与 snapshot 不一致）→ bootstrap skip，不 repair；
    - 自定义公司 + 用户修改的公司全称在非空路径与恢复路径均保留。
    """
    sessionmaker = env["sessionmaker"]
    await _wipe_everything(sessionmaker)
    await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    companies, aliases, markers = await _counts(sessionmaker)

    # 用户自定义公司 + 修改一家 snapshot 公司的全称（模拟合法数据演化）。
    await _insert_custom_company(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE companies SET official_name = '用户修改后的全称' "
                "WHERE security_code = '600519'"
            )
        )
        await session.commit()
    base_with_custom = companies + 1

    # 非空路径：即使 companies 少了 100 行（部分删除），bootstrap 也尊重现状，
    # 不 repair / 不补插 / 不覆盖。
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM companies WHERE security_code IN ('600000','600004')")
        )
        await session.commit()
    partial = await _counts(sessionmaker)
    assert 0 < partial[0] < base_with_custom

    result = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    assert result.skipped is True
    assert result.repair is False
    assert await _counts(sessionmaker) == partial  # 行数不变，未补插
    async with sessionmaker() as session:
        modified = (
            await session.execute(
                text("SELECT official_name FROM companies WHERE security_code = '600519'")
            )
        ).scalar_one()
    assert modified == "用户修改后的全称"  # 用户修改未被覆盖

    # 恢复路径（Case C 变体）：空表 + 用户已写入自定义公司 → 恢复补全 master，
    # 但自定义公司保留、不被删除。
    await _wipe_master(sessionmaker)
    await _insert_custom_company(sessionmaker, code="999998")
    result = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    assert result.repair is True
    recovered = await _counts(sessionmaker)
    assert recovered[0] == companies + 1  # master 恢复 + 自定义公司保留
    async with sessionmaker() as session:
        custom = (
            await session.execute(
                text("SELECT count(*) FROM companies WHERE security_code = '999998'")
            )
        ).scalar_one()
    assert custom == 1
