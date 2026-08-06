"""Integration tests for company identity and source registry."""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import CompanyIdentityAmbiguous
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.source_provider import SourceProviderModel
from app.db.session import DatabaseManager
from app.repositories.company_repository import CompanyRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.source_registry.url_policy import is_url_allowed

pytestmark = pytest.mark.integration

configure_asyncio_runtime()


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
        "provider_type": "exchange",
        "authority_tier": 1,
        "homepage_url": "https://example.org",
        "allowed_domains": ["example.org"],
        "capabilities": [],
        "acquisition_methods": ["official_web_page"],
        "exchange_scope": [],
        "requires_api_key": False,
        "critical_claim_eligible": False,
        "enabled": True,
    }
    defaults.update(overrides)
    return SourceProviderModel(**defaults)


def _company(provider_key: str, **overrides: object) -> CompanyModel:
    defaults: dict = {
        "company_id": uuid4(),
        "exchange": "SSE",
        "security_code": "123456",
        "identity_key": "SSE:123456",
        "board": "sse_main",
        "official_name": "测试公司",
        "short_name": "测试",
        "listing_status": "listed",
        "identity_source_provider_key": provider_key,
        "identity_source_url": "https://example.org",
    }
    defaults.update(overrides)
    return CompanyModel(**defaults)


_DEFAULT_PROVIDER_KEYS = ("sse", "szse", "bse", "cninfo", "csrc", "nbs", "fred", "world_bank")


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        placeholders = ",".join(f"'{key}'" for key in _DEFAULT_PROVIDER_KEYS)
        await session.execute(
            text(f"DELETE FROM source_providers WHERE provider_key NOT IN ({placeholders})")
        )
        await session.commit()


@pytest.mark.asyncio
async def test_new_tables_exist(database, sessionmaker) -> None:
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('source_providers','companies','company_aliases')"
            )
        )
        tables = {row[0] for row in result}
    assert {"source_providers", "companies", "company_aliases"} <= tables


@pytest.mark.asyncio
async def test_seed_defaults_idempotent_and_preserves_unknown(database, sessionmaker) -> None:
    await _cleanup(sessionmaker)
    service = SourceRegistryService(sessionmaker)

    r1 = await service.seed_defaults()
    assert r1.inserted_or_updated == 8
    assert r1.total == 8

    async with sessionmaker() as session:
        repo = SourceProviderRepository(session)
        await repo.upsert(
            _provider("unknown_test_provider", provider_type="general_web", authority_tier=3)
        )
        await session.commit()

    r2 = await service.seed_defaults()
    assert r2.inserted_or_updated == 8
    assert r2.total == 9

    async with sessionmaker() as session:
        unknown = await SourceProviderRepository(session).get_by_key("unknown_test_provider")
    assert unknown is not None

    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_provider_constraints(database, sessionmaker) -> None:
    async with sessionmaker() as session:
        repo = SourceProviderRepository(session)
        with pytest.raises(IntegrityError):
            await repo.upsert(_provider("bad_tier", authority_tier=9))
        await session.rollback()
        with pytest.raises(IntegrityError):
            await repo.upsert(_provider("bad_type", provider_type="bogus"))
        await session.rollback()
        with pytest.raises(IntegrityError):
            bad = _provider("bad_json")
            bad.allowed_domains = {"a": 1}
            await repo.upsert(bad)
        await session.rollback()
        # provider_key 唯一：upsert 同 key 更新不冲突
        await repo.upsert(_provider("dup_key"))
        await repo.upsert(_provider("dup_key", display_name="updated"))
        await session.commit()
        fetched = await repo.get_by_key("dup_key")
        assert fetched.display_name == "updated"
    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_company_and_alias_constraints(database, sessionmaker) -> None:
    provider_key = "company_test_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    async with sessionmaker() as session:
        repo = CompanyRepository(session)
        # identity_key 与 exchange/code 不一致
        with pytest.raises(IntegrityError):
            await repo.create(
                _company(
                    provider_key,
                    exchange="SSE",
                    security_code="123456",
                    identity_key="SZSE:123456",
                )
            )
        await session.rollback()
        # security_code 非六位
        with pytest.raises(IntegrityError):
            await repo.create(
                _company(
                    provider_key,
                    security_code="12345",
                    identity_key="SSE:12345",
                )
            )
        await session.rollback()

        company = _company(provider_key, security_code="123456", identity_key="SSE:123456")
        await repo.create(company)
        await session.commit()
        company_id = company.company_id
        # UNIQUE(exchange, code)
        with pytest.raises(IntegrityError):
            await repo.create(
                _company(provider_key, security_code="123456", identity_key="SSE:123456")
            )
        await session.rollback()

        # Alias 唯一 (company_id, normalized_alias, type)
        alias1 = CompanyAliasModel(
            company_id=company_id,
            alias="测试简称",
            normalized_alias="测试简称",
            alias_type="short_name",
            source_provider_key=provider_key,
            source_url="https://example.org",
        )
        await repo.add_alias(alias1)
        with pytest.raises(IntegrityError):
            await repo.add_alias(
                CompanyAliasModel(
                    company_id=company_id,
                    alias="测试简称",
                    normalized_alias="测试简称",
                    alias_type="short_name",
                    source_provider_key=provider_key,
                    source_url="https://example.org",
                )
            )
        await session.rollback()
        # Alias 非法 type
        with pytest.raises(IntegrityError):
            await repo.add_alias(
                CompanyAliasModel(
                    company_id=company_id,
                    alias="x",
                    normalized_alias="x",
                    alias_type="bogus",
                    source_provider_key=provider_key,
                    source_url="https://example.org",
                )
            )
        await session.rollback()

    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_resolve_security_code_and_alias(database, sessionmaker) -> None:
    provider_key = "resolve_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    service = CompanyIdentityService(sessionmaker)
    sse = _company(provider_key, exchange="SSE", security_code="600519", identity_key="SSE:600519")
    szse = _company(
        provider_key,
        exchange="SZSE",
        security_code="600519",
        identity_key="SZSE:600519",
        board="szse_main",
    )
    async with sessionmaker() as session:
        repo = CompanyRepository(session)
        await repo.create(sse)
        await repo.create(szse)
        await repo.add_alias(
            CompanyAliasModel(
                company_id=sse.company_id,
                alias="贵州茅台",
                normalized_alias="贵州茅台",
                alias_type="official_name",
                source_provider_key=provider_key,
                source_url="https://example.org",
            )
        )
        await session.commit()

    # 六位代码多 exchange → ambiguous
    with pytest.raises(Exception) as exc_info:
        await service.resolve("600519")
    assert "ambiguous" in type(exc_info.value).__name__.lower()

    # 显式 exchange 精确解析
    result = await service.resolve("SSE:600519")
    assert result.company.exchange == "SSE"
    assert result.match_type.value == "identity_key"

    # Alias 精确解析
    result = await service.resolve("贵州茅台")
    assert result.company.company_id == sse.company_id
    assert result.match_type.value == "official_name"

    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_resolve_explicit_symbol_and_identity_key_same_company(
    database, sessionmaker
) -> None:
    # .SH 显式后缀与 SSE: 前缀解析到同一公司；match_type 分别为
    # explicit_symbol 与 identity_key（契约修复的核心断言）。
    provider_key = "explicit_symbol_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    company = _company(provider_key, security_code="600519", identity_key="SSE:600519")
    async with sessionmaker() as session:
        await CompanyRepository(session).create(company)
        await session.commit()

    service = CompanyIdentityService(sessionmaker)

    by_symbol = await service.resolve("600519.SH")
    assert by_symbol.company.company_id == company.company_id
    assert by_symbol.company.exchange == "SSE"
    assert by_symbol.match_type.value == "explicit_symbol"
    assert by_symbol.matched_value == "600519.sh"

    by_key = await service.resolve("SSE:600519")
    assert by_key.company.company_id == company.company_id
    assert by_key.match_type.value == "identity_key"
    assert by_key.matched_value == "SSE:600519"

    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_url_policy_against_seeded_domains(database, sessionmaker) -> None:
    await _cleanup(sessionmaker)
    service = SourceRegistryService(sessionmaker)
    await service.seed_defaults()

    async with sessionmaker() as session:
        cninfo = await SourceProviderRepository(session).get_by_key("cninfo")
        szse = await SourceProviderRepository(session).get_by_key("szse")

    assert is_url_allowed("https://static.cninfo.com.cn/example.pdf", cninfo.allowed_domains)
    assert is_url_allowed("https://disc.static.szse.cn/example.pdf", szse.allowed_domains)
    assert not is_url_allowed("https://evil-cninfo.com.cn", cninfo.allowed_domains)
    assert not is_url_allowed("https://cninfo.com.cn.evil.com", cninfo.allowed_domains)

    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_exchange_board_consistency_rejects_bad_combinations(database, sessionmaker) -> None:
    provider_key = "eb_bad_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    bad = [
        ("SSE", "600001", "chinext"),
        ("SSE", "600002", "bse"),
        ("SZSE", "000001", "star"),
        ("BSE", "430001", "sse_main"),
    ]
    async with sessionmaker() as session:
        repo = CompanyRepository(session)
        for exchange, code, board in bad:
            with pytest.raises(IntegrityError):
                await repo.create(
                    _company(
                        provider_key,
                        exchange=exchange,
                        security_code=code,
                        identity_key=f"{exchange}:{code}",
                        board=board,
                    )
                )
            await session.rollback()
    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_exchange_board_consistency_accepts_good_combinations(database, sessionmaker) -> None:
    provider_key = "eb_good_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    good = [
        ("SSE", "600001", "sse_main"),
        ("SSE", "688001", "star"),
        ("SZSE", "000001", "szse_main"),
        ("SZSE", "300001", "chinext"),
        ("BSE", "430001", "bse"),
    ]
    async with sessionmaker() as session:
        repo = CompanyRepository(session)
        for exchange, code, board in good:
            await repo.create(
                _company(
                    provider_key,
                    exchange=exchange,
                    security_code=code,
                    identity_key=f"{exchange}:{code}",
                    board=board,
                )
            )
        await session.commit()
    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_alias_same_company_multiple_rows_not_ambiguous(database, sessionmaker) -> None:
    provider_key = "alias_same_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    company = _company(provider_key, security_code="600519", identity_key="SSE:600519")
    async with sessionmaker() as session:
        repo = CompanyRepository(session)
        await repo.create(company)
        # 同一公司 official_name + short_name 使用相同 normalized_alias；
        # DB 唯一约束 (company_id, normalized_alias, alias_type) 允许两行。
        await repo.add_alias(
            CompanyAliasModel(
                company_id=company.company_id,
                alias="贵州茅台",
                normalized_alias="贵州茅台",
                alias_type="official_name",
                source_provider_key=provider_key,
                source_url="https://example.org",
            )
        )
        await repo.add_alias(
            CompanyAliasModel(
                company_id=company.company_id,
                alias="贵州茅台",
                normalized_alias="贵州茅台",
                alias_type="short_name",
                source_provider_key=provider_key,
                source_url="https://example.org",
            )
        )
        await session.commit()

    service = CompanyIdentityService(sessionmaker)
    result = await service.resolve("贵州茅台")

    assert result.company.company_id == company.company_id
    assert result.match_type.value == "official_name"

    await _cleanup(sessionmaker)


@pytest.mark.asyncio
async def test_alias_different_companies_ambiguous(database, sessionmaker) -> None:
    provider_key = "alias_diff_provider"
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(_provider(provider_key))
        await session.commit()

    first = _company(provider_key, security_code="600001", identity_key="SSE:600001")
    second = _company(
        provider_key,
        exchange="SZSE",
        security_code="600001",
        identity_key="SZSE:600001",
        board="szse_main",
        official_name="另一家公司",
    )
    async with sessionmaker() as session:
        repo = CompanyRepository(session)
        await repo.create(first)
        await repo.create(second)
        for company in (first, second):
            await repo.add_alias(
                CompanyAliasModel(
                    company_id=company.company_id,
                    alias="同名简称",
                    normalized_alias="同名简称",
                    alias_type="short_name",
                    source_provider_key=provider_key,
                    source_url="https://example.org",
                )
            )
        await session.commit()

    service = CompanyIdentityService(sessionmaker)
    with pytest.raises(CompanyIdentityAmbiguous):
        await service.resolve("同名简称")

    await _cleanup(sessionmaker)
