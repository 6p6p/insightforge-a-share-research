"""Source discovery layer unit tests (P1).

- 契约：request / result / outcome 语义；
- SourceDiscoveryService 路由：provider 顺序、第一个 acquired 停止、
  全部 exhausted、无匹配 provider、provider 契约违反（抛异常）翻译。
"""

from datetime import date
from uuid import uuid4

import pytest

from app.services.source_discovery.contracts import (
    REASON_DISCOVERY_FAILED,
    REASON_NO_CANDIDATES,
    REASON_PROVIDER_UNAVAILABLE,
    SourceDiscoveryOutcome,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)
from app.services.source_discovery.service import SourceDiscoveryService

_COMPANY_ID = uuid4()


def _request(**overrides) -> SourceDiscoveryRequest:
    base = dict(
        company_id=_COMPANY_ID,
        security_code="600519",
        need_kind="document",
        source_type="annual_report",
        period="2024",
        as_of=date(2026, 8, 10),
    )
    base.update(overrides)
    return SourceDiscoveryRequest(**base)


class FakeProvider:
    """确定性 fake provider：supports / result / 异常注入 / 调用计数。"""

    def __init__(
        self,
        key: str,
        *,
        supports: bool = True,
        result: SourceDiscoveryResult | None = None,
        raise_on_discover: BaseException | None = None,
        raise_on_supports: BaseException | None = None,
    ) -> None:
        self.provider_key = key
        self._supports = supports
        self._result = result
        self._raise_on_discover = raise_on_discover
        self._raise_on_supports = raise_on_supports
        self.calls: list[SourceDiscoveryRequest] = []

    def supports(self, request: SourceDiscoveryRequest) -> bool:
        if self._raise_on_supports is not None:
            raise self._raise_on_supports
        return self._supports

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        self.calls.append(request)
        if self._raise_on_discover is not None:
            raise self._raise_on_discover
        return self._result or SourceDiscoveryResult(provider_key=self.provider_key)


# ---------------------------------------------------------------- 路由


@pytest.mark.asyncio
async def test_first_acquired_provider_stops_chain() -> None:
    src_id = uuid4()
    p1 = FakeProvider(
        "p1",
        result=SourceDiscoveryResult(provider_key="p1", acquired=True, source_ids=(src_id,)),
    )
    p2 = FakeProvider("p2")
    service = SourceDiscoveryService([p1, p2])

    outcome = await service.discover(_request())

    assert outcome.acquired is True
    assert outcome.exhausted is False
    assert outcome.source_ids == (src_id,)
    assert len(p1.calls) == 1
    assert len(p2.calls) == 0  # 第一个 acquired → 不再尝试后续 provider


@pytest.mark.asyncio
async def test_all_exhausted_returns_exhausted_with_reasons() -> None:
    p1 = FakeProvider(
        "p1",
        result=SourceDiscoveryResult(
            provider_key="p1", reason=REASON_NO_CANDIDATES, exhausted=True
        ),
    )
    p2 = FakeProvider(
        "p2",
        result=SourceDiscoveryResult(
            provider_key="p2", reason=REASON_NO_CANDIDATES, exhausted=True
        ),
    )
    service = SourceDiscoveryService([p1, p2])

    outcome = await service.discover(_request())

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reasons == (REASON_NO_CANDIDATES, REASON_NO_CANDIDATES)
    assert outcome.primary_reason == REASON_NO_CANDIDATES
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1


@pytest.mark.asyncio
async def test_unsupported_providers_are_skipped() -> None:
    p1 = FakeProvider("p1", supports=False)
    p2 = FakeProvider("p2", result=SourceDiscoveryResult(provider_key="p2", acquired=True))
    service = SourceDiscoveryService([p1, p2])

    outcome = await service.discover(_request())

    assert outcome.acquired is True
    assert len(p1.calls) == 0
    assert len(p2.calls) == 1


@pytest.mark.asyncio
async def test_no_matching_provider_returns_provider_unavailable() -> None:
    service = SourceDiscoveryService([FakeProvider("p1", supports=False)])

    outcome = await service.discover(_request())

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reasons == (REASON_PROVIDER_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_provider_exception_translated_to_exhausted() -> None:
    p1 = FakeProvider("p1", raise_on_discover=RuntimeError("boom"))
    p2 = FakeProvider(
        "p2",
        result=SourceDiscoveryResult(
            provider_key="p2", reason=REASON_NO_CANDIDATES, exhausted=True
        ),
    )
    service = SourceDiscoveryService([p1, p2])

    outcome = await service.discover(_request())

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reasons == (REASON_DISCOVERY_FAILED, REASON_NO_CANDIDATES)


@pytest.mark.asyncio
async def test_provider_supports_exception_is_skipped() -> None:
    p1 = FakeProvider("p1", raise_on_supports=RuntimeError("boom"))
    p2 = FakeProvider("p2", result=SourceDiscoveryResult(provider_key="p2", acquired=True))
    service = SourceDiscoveryService([p1, p2])

    outcome = await service.discover(_request())

    assert outcome.acquired is True
    assert len(p2.calls) == 1


@pytest.mark.asyncio
async def test_acquired_source_ids_deduplicated_across_providers() -> None:
    src_id = uuid4()
    p1 = FakeProvider(
        "p1",
        result=SourceDiscoveryResult(provider_key="p1", acquired=True, source_ids=(src_id,)),
    )
    service = SourceDiscoveryService([p1])

    outcome = await service.discover(_request())

    assert outcome.source_ids == (src_id,)


# ---------------------------------------------------------------- 契约语义


def test_outcome_primary_reason() -> None:
    outcome = SourceDiscoveryOutcome(acquired=False, exhausted=True, reasons=("a", "b"))
    assert outcome.primary_reason == "a"
    empty = SourceDiscoveryOutcome()
    assert empty.primary_reason is None


def test_request_frozen() -> None:
    from dataclasses import FrozenInstanceError

    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.need_kind = "macro"  # type: ignore[misc]


def test_result_requires_provider_key() -> None:
    with pytest.raises(TypeError):
        SourceDiscoveryResult()  # type: ignore[call-arg]
