"""Discovery provider wrapper unit tests (P1).

- AnnouncementDiscoveryProvider：supports 判定 + 包装 acquire_report
  （acquired / None / 异常 → exhausted）；
- MacroDiscoveryProvider：supports 判定 + 包装 fetch_for_need
  （persisted / 未命中 / 异常 → exhausted）；
- SearchDiscoveryProvider / NewsDiscoveryProvider：P1 扩展点占位
  （未配置 → 稳定 reason + exhausted）。
"""

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.services.source_discovery.contracts import (
    REASON_NEWS_NOT_ENABLED,
    REASON_NO_CANDIDATES,
    REASON_SEARCH_NOT_CONFIGURED,
    SourceDiscoveryRequest,
)
from app.services.source_discovery.providers import (
    AnnouncementDiscoveryProvider,
    MacroDiscoveryProvider,
    NewsDiscoveryProvider,
    SearchDiscoveryProvider,
)

_COMPANY_ID = uuid4()
_AS_OF = date(2026, 8, 10)


def _request(**overrides) -> SourceDiscoveryRequest:
    base = dict(
        company_id=_COMPANY_ID,
        security_code="600519",
        need_kind="document",
        source_type="annual_report",
        period="2024",
        as_of=_AS_OF,
    )
    base.update(overrides)
    return SourceDiscoveryRequest(**base)


class FakeAnnouncement:
    def __init__(self, result=None, raise_on: BaseException | None = None) -> None:
        self._result = result
        self._raise_on = raise_on
        self.calls: list[dict] = []

    async def acquire_report(self, **kwargs) -> object | None:
        self.calls.append(kwargs)
        if self._raise_on is not None:
            raise self._raise_on
        return self._result


class FakeAcquireResult:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id


class FakeMacroFetch:
    def __init__(self, persisted: bool, raise_on: BaseException | None = None) -> None:
        self._persisted = persisted
        self._raise_on = raise_on
        self.calls: list[dict] = []

    async def fetch_for_need(self, **kwargs) -> object:
        self.calls.append(kwargs)
        if self._raise_on is not None:
            raise self._raise_on
        return type("R", (), {"fetched": self._persisted, "persisted": self._persisted})()


# ---------------------------------------------------------------- announcement


def test_announcement_supports_document_types() -> None:
    provider = AnnouncementDiscoveryProvider()
    assert provider.supports(_request(source_type="annual_report"))
    assert provider.supports(_request(source_type="quarterly_report"))
    assert provider.supports(_request(source_type="company_announcement"))
    assert provider.supports(_request(source_type="issuer_ir_material"))
    assert not provider.supports(_request(need_kind="macro"))
    assert not provider.supports(_request(source_type="news_article"))


@pytest.mark.asyncio
async def test_announcement_acquired_returns_source_id() -> None:
    src_id = str(uuid4())
    provider = AnnouncementDiscoveryProvider(FakeAnnouncement(FakeAcquireResult(src_id)))
    outcome = await provider.discover(_request())
    assert outcome.acquired is True
    assert outcome.source_ids == (UUID(src_id),)


@pytest.mark.asyncio
async def test_announcement_no_candidates_exhausted() -> None:
    provider = AnnouncementDiscoveryProvider(FakeAnnouncement(None))
    outcome = await provider.discover(_request())
    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_NO_CANDIDATES


@pytest.mark.asyncio
async def test_announcement_exception_exhausted() -> None:
    provider = AnnouncementDiscoveryProvider(FakeAnnouncement(raise_on=RuntimeError("boom")))
    outcome = await provider.discover(_request())
    assert outcome.acquired is False
    assert outcome.exhausted is True


@pytest.mark.asyncio
async def test_announcement_unbound_inner_exhausted() -> None:
    provider = AnnouncementDiscoveryProvider()
    outcome = await provider.discover(_request())
    assert outcome.acquired is False
    assert outcome.exhausted is True


# ---------------------------------------------------------------- macro


def test_macro_supports_macro_need_and_dataset() -> None:
    provider = MacroDiscoveryProvider()
    assert provider.supports(_request(need_kind="macro"))
    assert provider.supports(_request(need_kind="document", source_type="macro_dataset"))
    assert not provider.supports(_request(need_kind="document", source_type="annual_report"))


@pytest.mark.asyncio
async def test_macro_persisted_acquired() -> None:
    provider = MacroDiscoveryProvider(FakeMacroFetch(persisted=True))
    outcome = await provider.discover(_request(need_kind="macro", topic="中国GDP"))
    assert outcome.acquired is True
    assert outcome.source_ids == ()


@pytest.mark.asyncio
async def test_macro_not_persisted_exhausted() -> None:
    provider = MacroDiscoveryProvider(FakeMacroFetch(persisted=False))
    outcome = await provider.discover(_request(need_kind="macro", topic="unknown_topic"))
    assert outcome.acquired is False
    assert outcome.exhausted is True


@pytest.mark.asyncio
async def test_macro_exception_exhausted() -> None:
    provider = MacroDiscoveryProvider(FakeMacroFetch(persisted=False, raise_on=RuntimeError("x")))
    outcome = await provider.discover(_request(need_kind="macro"))
    assert outcome.acquired is False
    assert outcome.exhausted is True


# ---------------------------------------------------------------- 扩展点占位


def test_search_provider_supports_other_and_financial() -> None:
    provider = SearchDiscoveryProvider()
    assert provider.supports(_request(source_type="other"))
    assert provider.supports(_request(need_kind="financial"))
    assert not provider.supports(_request(source_type="annual_report"))


@pytest.mark.asyncio
async def test_search_not_configured_exhausted() -> None:
    provider = SearchDiscoveryProvider()
    outcome = await provider.discover(_request(source_type="other"))
    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_SEARCH_NOT_CONFIGURED


def test_news_provider_supports_event_and_news() -> None:
    provider = NewsDiscoveryProvider()
    assert provider.supports(_request(need_kind="event"))
    assert provider.supports(_request(need_kind="document", source_type="news_article"))
    assert not provider.supports(_request(need_kind="document", source_type="annual_report"))


@pytest.mark.asyncio
async def test_news_not_enabled_exhausted() -> None:
    provider = NewsDiscoveryProvider()
    outcome = await provider.discover(_request(need_kind="event"))
    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_NEWS_NOT_ENABLED
