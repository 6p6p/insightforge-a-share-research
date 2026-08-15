"""Unit tests for announcement discovery helpers (V1.1 closure)."""

from app.services.announcement_discovery_service import (
    _keyword_for,
    _matches_keyword,
    _parse_notice_date,
)


def test_keyword_for() -> None:
    assert _keyword_for("annual_report") == "年度报告"
    assert _keyword_for("semiannual_report") == "半年度报告"
    assert _keyword_for("quarterly_report") == "季度报告"
    assert _keyword_for("company_announcement") is None
    assert _keyword_for("other") is None


def test_matches_keyword_annual() -> None:
    assert _matches_keyword("宁德时代:2023年年度报告", "年度报告")
    # 半年度报告不能命中年度报告关键词。
    assert not _matches_keyword("宁德时代:2023年半年度报告", "年度报告")
    # 摘要 / 英文版排除。
    assert not _matches_keyword("宁德时代:2023年年度报告摘要", "年度报告")
    assert not _matches_keyword("宁德时代:2023年年度报告(英文版)", "年度报告")


def test_matches_keyword_semiannual() -> None:
    assert _matches_keyword("宁德时代:2023年半年度报告", "半年度报告")
    assert not _matches_keyword("宁德时代:2023年年度报告", "半年度报告")


def test_matches_keyword_quarterly() -> None:
    assert _matches_keyword("宁德时代:2023年第一季度报告", "季度报告")
    assert _matches_keyword("宁德时代:2023年第三季度报告", "季度报告")
    assert not _matches_keyword("宁德时代:2023年半年度报告", "季度报告")


def test_parse_notice_date() -> None:
    assert _parse_notice_date("2026-08-12 00:00:00") is not None
    assert _parse_notice_date("2026-08-12") is not None
    assert _parse_notice_date("") is None
    assert _parse_notice_date(None) is None
    assert _parse_notice_date("not-a-date") is None
