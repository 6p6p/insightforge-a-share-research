"""Tests for the company identity service."""

from uuid import uuid4

import pytest

from app.core.errors import CompanyIdentityAmbiguous, CompanyIdentityNotFound
from app.db.models.company import CompanyModel
from app.repositories.company_repository import CompanyRepository
from app.services.company_identity_service import CompanyIdentityService


def _company(**overrides: object) -> CompanyModel:
    defaults: dict = {
        "company_id": uuid4(),
        "exchange": "SSE",
        "security_code": "600519",
        "identity_key": "SSE:600519",
        "board": "sse_main",
        "official_name": "贵州茅台酒股份有限公司",
        "short_name": "贵州茅台",
        "listing_status": "listed",
        "identity_source_provider_key": "sse",
        "identity_source_url": "https://www.sse.com.cn",
    }
    defaults.update(overrides)
    return CompanyModel(**defaults)


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        pass


class _SessionMaker:
    def __init__(self) -> None:
        self.session = _Session()

    def __call__(self) -> _Session:
        return self.session


@pytest.mark.asyncio
async def test_resolve_identity_key(monkeypatch) -> None:
    company = _company()
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, identity_key):
        return company

    monkeypatch.setattr(CompanyRepository, "get_by_identity_key", fake)

    result = await service.resolve("SSE:600519")

    assert result.match_type.value == "identity_key"
    assert result.matched_value == "SSE:600519"
    assert result.company.company_id == company.company_id


@pytest.mark.asyncio
async def test_resolve_explicit_symbol_sse(monkeypatch) -> None:
    company = _company(exchange="SSE", security_code="600519", identity_key="SSE:600519")
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, identity_key):
        assert identity_key == "SSE:600519"
        return company

    monkeypatch.setattr(CompanyRepository, "get_by_identity_key", fake)

    result = await service.resolve("600519.SH")

    assert result.company.company_id == company.company_id
    assert result.company.exchange == "SSE"
    assert result.match_type.value == "explicit_symbol"
    # matched_value 采用规范化后的查询文本（NFKC + casefold）
    assert result.matched_value == "600519.sh"


@pytest.mark.asyncio
async def test_resolve_explicit_symbol_szse(monkeypatch) -> None:
    company = _company(
        exchange="SZSE",
        security_code="000001",
        identity_key="SZSE:000001",
        board="szse_main",
    )
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, identity_key):
        assert identity_key == "SZSE:000001"
        return company

    monkeypatch.setattr(CompanyRepository, "get_by_identity_key", fake)

    result = await service.resolve("000001.SZ")

    assert result.company.exchange == "SZSE"
    assert result.match_type.value == "explicit_symbol"


@pytest.mark.asyncio
async def test_resolve_explicit_symbol_bse(monkeypatch) -> None:
    company = _company(
        exchange="BSE",
        security_code="430047",
        identity_key="BSE:430047",
        board="bse",
    )
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, identity_key):
        assert identity_key == "BSE:430047"
        return company

    monkeypatch.setattr(CompanyRepository, "get_by_identity_key", fake)

    result = await service.resolve("430047.BJ")

    assert result.company.exchange == "BSE"
    assert result.match_type.value == "explicit_symbol"


@pytest.mark.asyncio
async def test_resolve_identity_key_not_found(monkeypatch) -> None:
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, identity_key):
        return None

    monkeypatch.setattr(CompanyRepository, "get_by_identity_key", fake)

    with pytest.raises(CompanyIdentityNotFound):
        await service.resolve("SSE:600519")


@pytest.mark.asyncio
async def test_resolve_security_code_unique(monkeypatch) -> None:
    company = _company()
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, code):
        return [company]

    monkeypatch.setattr(CompanyRepository, "find_by_security_code", fake)

    result = await service.resolve("600519")

    assert result.match_type.value == "security_code"
    assert result.company.exchange == "SSE"


@pytest.mark.asyncio
async def test_resolve_security_code_ambiguous(monkeypatch) -> None:
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, code):
        return [
            _company(),
            _company(
                exchange="SZSE",
                security_code="600519",
                identity_key="SZSE:600519",
                board="szse_main",
            ),
        ]

    monkeypatch.setattr(CompanyRepository, "find_by_security_code", fake)

    with pytest.raises(CompanyIdentityAmbiguous):
        await service.resolve("600519")


@pytest.mark.asyncio
async def test_resolve_no_prefix_inference(monkeypatch) -> None:
    # 数据库只有 SZSE:600519；查询 600519 命中 SZSE（不因 6 开头强制 SSE）
    company = _company(
        exchange="SZSE",
        security_code="600519",
        identity_key="SZSE:600519",
        board="szse_main",
    )
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, code):
        return [company]

    monkeypatch.setattr(CompanyRepository, "find_by_security_code", fake)

    result = await service.resolve("600519")

    assert result.company.exchange == "SZSE"


@pytest.mark.asyncio
async def test_resolve_alias_unique(monkeypatch) -> None:
    company = _company()
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, normalized):
        return [(company, "official_name")]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake)

    result = await service.resolve("贵州茅台")

    assert result.match_type.value == "official_name"
    assert result.matched_value == "贵州茅台"


@pytest.mark.asyncio
async def test_resolve_alias_ambiguous(monkeypatch) -> None:
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, normalized):
        return [
            (_company(), "short_name"),
            (
                _company(
                    exchange="SZSE",
                    security_code="600519",
                    identity_key="SZSE:600519",
                    board="szse_main",
                ),
                "short_name",
            ),
        ]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake)

    with pytest.raises(CompanyIdentityAmbiguous):
        await service.resolve("同名简称")


@pytest.mark.asyncio
async def test_resolve_alias_same_company_deduplicates_and_picks_official(
    monkeypatch,
) -> None:
    # 同一公司 official_name + short_name 使用相同 normalized_alias：
    # 按 distinct company_id 去重后唯一，不 ambiguous，match_type=official_name。
    company = _company()
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, normalized):
        return [
            (company, "short_name"),
            (company, "official_name"),
        ]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake)

    result = await service.resolve("贵州茅台")

    assert result.company.company_id == company.company_id
    assert result.match_type.value == "official_name"
    assert result.matched_value == "贵州茅台"


@pytest.mark.asyncio
async def test_resolve_alias_same_company_former_outranks_english(
    monkeypatch,
) -> None:
    # 同一公司 former_name + english_name 使用相同 normalized_alias：
    # 唯一命中，match_type=former_name（高于 english_name）。
    company = _company()
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, normalized):
        return [
            (company, "english_name"),
            (company, "former_name"),
        ]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake)

    result = await service.resolve("some name")

    assert result.company.company_id == company.company_id
    assert result.match_type.value == "former_name"


@pytest.mark.asyncio
async def test_resolve_alias_short_name_same_company_not_ambiguous(
    monkeypatch,
) -> None:
    # 同一公司 short_name 多条同 normalized_alias 行（如不同来源重复登记）：
    # 不因行数多而 ambiguity，只按 distinct company_id 判断。
    company = _company()
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, normalized):
        return [
            (company, "short_name"),
            (company, "short_name"),
        ]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake)

    result = await service.resolve("贵州茅台")

    assert result.company.company_id == company.company_id
    assert result.match_type.value == "short_name"


@pytest.mark.asyncio
async def test_resolve_whitespace_in_short_name_falls_back_to_direct_match(monkeypatch) -> None:
    """P1 generalization: alias table miss due to whitespace → direct name fallback."""
    company = _company(short_name="五 粮 液", official_name="宜宾五粮液股份有限公司")
    service = CompanyIdentityService(_SessionMaker())

    async def fake_alias(self, normalized):
        return []  # alias table miss

    async def fake_direct(self, normalized):
        return [(company, "short_name")]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake_alias)
    monkeypatch.setattr(CompanyRepository, "find_by_direct_name", fake_direct)

    result = await service.resolve("五粮液")

    assert result.company.company_id == company.company_id
    assert result.match_type.value == "short_name"


@pytest.mark.asyncio
async def test_resolve_whitespace_in_official_name_falls_back(monkeypatch) -> None:
    """Direct name fallback also works for official_name with whitespace."""
    company = _company(short_name="五粮液", official_name="宜宾 五粮液 股份有限公司")
    service = CompanyIdentityService(_SessionMaker())

    async def fake_alias(self, normalized):
        return []

    async def fake_direct(self, normalized):
        return [(company, "official_name")]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake_alias)
    monkeypatch.setattr(CompanyRepository, "find_by_direct_name", fake_direct)

    result = await service.resolve("宜宾五粮液股份有限公司")

    assert result.company.company_id == company.company_id
    assert result.match_type.value == "official_name"


@pytest.mark.asyncio
async def test_resolve_direct_fallback_ambiguous(monkeypatch) -> None:
    """Direct name fallback preserves ambiguity detection."""
    c1 = _company(company_id=uuid4(), short_name="名称A", official_name="公司A")
    c2 = _company(company_id=uuid4(), short_name="名称B", official_name="公司B")
    # Make sure they have different IDs
    c2.company_id = uuid4()
    service = CompanyIdentityService(_SessionMaker())

    async def fake_alias(self, normalized):
        return []

    async def fake_direct(self, normalized):
        return [(c1, "short_name"), (c2, "short_name")]

    monkeypatch.setattr(CompanyRepository, "find_by_normalized_alias", fake_alias)
    monkeypatch.setattr(CompanyRepository, "find_by_direct_name", fake_direct)

    with pytest.raises(CompanyIdentityAmbiguous):
        await service.resolve("名称")


@pytest.mark.asyncio
async def test_get_company_missing(monkeypatch) -> None:
    service = CompanyIdentityService(_SessionMaker())

    async def fake(self, company_id):
        return None

    monkeypatch.setattr(CompanyRepository, "get_by_id", fake)

    with pytest.raises(CompanyIdentityNotFound):
        await service.get_company(uuid4())
