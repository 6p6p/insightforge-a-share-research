"""Tests for the official disclosure discovery contracts."""

from datetime import date, datetime
from uuid import uuid4

import pytest

from app.disclosures.contracts import (
    DisclosureCandidate,
    DisclosureSearchRequest,
)
from app.domain.companies import ExchangeCode
from app.domain.source_records import SourceDocumentType
from app.domain.sources import AcquisitionMethod


def _request(**overrides) -> DisclosureSearchRequest:
    defaults: dict = {
        "company_id": uuid4(),
        "exchange": ExchangeCode.SSE,
        "security_code": "600519",
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 8, 7),
        "document_types": (SourceDocumentType.ANNUAL_REPORT,),
        "keywords": (),
        "limit": 50,
    }
    defaults.update(overrides)
    return DisclosureSearchRequest(**defaults)


def test_valid_request_ok() -> None:
    request = _request()
    assert request.limit == 50
    assert request.security_code == "600519"


@pytest.mark.parametrize(
    "code",
    ["12345", "1234567", "abcd12", "60051x", "", " 600519"],
)
def test_security_code_must_be_six_digits(code: str) -> None:
    with pytest.raises(ValueError):
        _request(security_code=code)


def test_security_code_non_string_rejected() -> None:
    with pytest.raises(ValueError):
        _request(security_code=600519)  # type: ignore[arg-type]


def test_date_order_invalid() -> None:
    with pytest.raises(ValueError):
        _request(start_date=date(2026, 8, 7), end_date=date(2026, 1, 1))


def test_date_range_single_day_ok() -> None:
    # 2026-01-01 至 2026-01-01：1 个自然日（含首尾），合法
    _request(start_date=date(2026, 1, 1), end_date=date(2026, 1, 1))


def test_date_range_ok_at_366_days() -> None:
    # 2026-01-01 至 2027-01-01：闭区间 366 个自然日，恰好在边界内
    _request(start_date=date(2026, 1, 1), end_date=date(2027, 1, 1))


def test_date_range_over_366_days_invalid() -> None:
    # 2026-01-01 至 2027-01-02：闭区间 367 个自然日，超出边界
    with pytest.raises(ValueError):
        _request(start_date=date(2026, 1, 1), end_date=date(2027, 1, 2))


def test_empty_document_types_invalid() -> None:
    with pytest.raises(ValueError):
        _request(document_types=())


def test_limit_bounds() -> None:
    _request(limit=1)
    _request(limit=100)
    with pytest.raises(ValueError):
        _request(limit=0)
    with pytest.raises(ValueError):
        _request(limit=101)


def test_keyword_count_limit() -> None:
    _request(keywords=tuple(f"k{i}" for i in range(10)))
    with pytest.raises(ValueError):
        _request(keywords=tuple(f"k{i}" for i in range(11)))


def test_keyword_length_limit() -> None:
    _request(keywords=("a" * 100,))
    with pytest.raises(ValueError):
        _request(keywords=("a" * 101,))


def test_candidate_requires_fields() -> None:
    candidate = DisclosureCandidate(
        provider_key="sse",
        title="贵州茅台 2025 年度报告",
        source_url="https://www.sse.com.cn/2025/000001.pdf",
        discovery_url="https://www.sse.com.cn/disclosure/listedinfo/announcement/",
        published_at=datetime(2026, 4, 30, 9, 0, 0),
        document_type=SourceDocumentType.ANNUAL_REPORT,
        company_security_code="600519",
        acquisition_method=AcquisitionMethod.OFFICIAL_WEB_PAGE,
    )
    assert candidate.provider_key == "sse"
    assert candidate.external_document_id is None
    assert candidate.reporting_period_end is None


def test_candidate_rejects_blank_title() -> None:
    with pytest.raises(ValueError):
        DisclosureCandidate(
            provider_key="sse",
            title="   ",
            source_url="https://www.sse.com.cn/2025/000001.pdf",
            discovery_url="https://www.sse.com.cn/disclosure/",
            published_at=datetime(2026, 4, 30, 9, 0, 0),
            document_type=SourceDocumentType.ANNUAL_REPORT,
            company_security_code="600519",
            acquisition_method=AcquisitionMethod.OFFICIAL_WEB_PAGE,
        )


def test_candidate_rejects_http_urls() -> None:
    common: dict = {
        "provider_key": "sse",
        "title": "t",
        "published_at": datetime(2026, 4, 30, 9, 0, 0),
        "document_type": SourceDocumentType.ANNUAL_REPORT,
        "company_security_code": "600519",
        "acquisition_method": AcquisitionMethod.OFFICIAL_WEB_PAGE,
    }
    with pytest.raises(ValueError):
        DisclosureCandidate(
            source_url="http://www.sse.com.cn/x.pdf",
            discovery_url="https://www.sse.com.cn/disclosure/",
            **common,
        )
    with pytest.raises(ValueError):
        DisclosureCandidate(
            source_url="https://www.sse.com.cn/x.pdf",
            discovery_url="http://x",
            **common,
        )


def test_candidate_provider_metadata_holds_primitive_only() -> None:
    candidate = DisclosureCandidate(
        provider_key="cninfo",
        title="t",
        source_url="https://www.cninfo.com.cn/x.pdf",
        discovery_url="https://www.cninfo.com.cn/new/disclosure",
        published_at=datetime(2026, 4, 30, 9, 0, 0),
        document_type=SourceDocumentType.COMPANY_ANNOUNCEMENT,
        company_security_code="000001",
        acquisition_method=AcquisitionMethod.OFFICIAL_WEB_PAGE,
        external_document_id="ann-123",
        reporting_period_end=date(2025, 12, 31),
        provider_metadata={"announcement_id": "123", "page": 1, "is_regular": True},
    )
    assert candidate.provider_metadata["announcement_id"] == "123"
    assert candidate.provider_metadata["page"] == 1
    assert candidate.provider_metadata["is_regular"] is True
