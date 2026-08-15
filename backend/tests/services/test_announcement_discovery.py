"""Unit tests for announcement discovery helpers (V1.1 closure)."""

from datetime import date

from app.services.announcement_discovery_service import (
    _keyword_for,
    _matches_keyword,
    _parse_notice_date,
    _scan_cutoff,
    looks_like_pdf,
    parse_challenge_cookies,
    reporting_period_end_for,
)


def test_keyword_for() -> None:
    assert _keyword_for("annual_report") == "年度报告"
    assert _keyword_for("semiannual_report") == "半年度报告"
    assert _keyword_for("quarterly_report") == "季度报告"
    assert _keyword_for("company_announcement") is None
    assert _keyword_for("other") is None


def test_matches_ir_title() -> None:
    from app.services.announcement_discovery_service import _matches_ir_title

    assert _matches_ir_title("宁德时代:2025年8月14日投资者关系活动记录表")
    assert _matches_ir_title("宁德时代:2024年度业绩说明会")
    assert not _matches_ir_title("宁德时代:2025年年度报告")
    assert not _matches_ir_title("宁德时代:投资者关系活动记录表(摘要)")
    assert not _matches_ir_title("宁德时代:2025年三季度报告")


def test_matches_announcement_title() -> None:
    from app.services.announcement_discovery_service import _matches_announcement_title

    assert _matches_announcement_title("宁德时代:关于回购公司股份的公告")
    assert _matches_announcement_title("宁德时代:2025年半年度权益分派实施公告")
    assert not _matches_announcement_title(
        "宁德时代:关于宁德时代新能源科技股份有限公司2025年第二次临时股东会的法律意见书"
    )
    assert not _matches_announcement_title("宁德时代:验资报告")
    assert not _matches_announcement_title("宁德时代:更正公告")


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


def test_parse_challenge_cookies() -> None:
    js = (
        '<script>function a(a){var e={WTKkN:146423112,bOYDu:353574479,'
        'dtzqS:function(a,n){return a+n},wyeCN:586194283,pCQRM:function(a){return a()}},'
        't=0;return t+=e["WTKkN"],t+=e["bOYDu"],t=e["dtzqS"](t,e["wyeCN"]),'
        '[t,e["pCQRM"](n)][a]}'
        '...a["iTyzs"](t,2975465472)...'
        'document.cookie="__tst_status="+a(0)+"#;"</script>'
    )
    parsed = parse_challenge_cookies(js)
    assert parsed is not None
    t, ssid = parsed
    assert t == 146423112 + 353574479 + 586194283
    assert ssid == 2975465472


def test_parse_challenge_cookies_unparseable() -> None:
    assert parse_challenge_cookies("<html>plain error page</html>") is None
    assert parse_challenge_cookies("") is None


def test_looks_like_pdf() -> None:
    assert looks_like_pdf(b"%PDF-1.7\n" + b"x" * 2000)
    assert not looks_like_pdf(b"<script>function a(a){}</script>")
    assert not looks_like_pdf(b"%PDF-1.7")  # 太短（反爬 stub）
    assert not looks_like_pdf(b"")


def test_reporting_period_end_for() -> None:
    assert reporting_period_end_for("annual_report", "2024", "宁德时代:2024年年度报告") == date(
        2024, 12, 31
    )
    assert reporting_period_end_for("semiannual_report", "2025", "宁德时代:2025年半年度报告") == date(
        2025, 6, 30
    )
    assert reporting_period_end_for(
        "quarterly_report", "2025", "宁德时代:2025年第一季度报告"
    ) == date(2025, 3, 31)
    assert reporting_period_end_for(
        "quarterly_report", "2025", "宁德时代:2025年第三季度报告"
    ) == date(2025, 9, 30)
    assert reporting_period_end_for("quarterly_report", "2025", "宁德时代:2025年季度报告") == date(
        2025, 9, 30
    )
    assert reporting_period_end_for("annual_report", None, "x") is None
    assert reporting_period_end_for("annual_report", "20", "x") is None
    assert reporting_period_end_for("company_announcement", "2024", "x") is None


def test_scan_cutoff() -> None:
    as_of = date(2025, 12, 31)
    # 年报 Y：最早 Y+1-01-01 披露（2023 年报 2024-03 可被发现）。
    assert _scan_cutoff("annual_report", "2023", as_of) == date(2024, 1, 1)
    assert _scan_cutoff("annual_report", "2024", as_of) == date(2025, 1, 1)
    assert _scan_cutoff("semiannual_report", "2025", as_of) == date(2025, 7, 1)
    assert _scan_cutoff("quarterly_report", "2025", as_of) == date(2025, 1, 1)
    # 无 period → as_of - 400 天。
    assert _scan_cutoff("annual_report", None, as_of) == date(2024, 11, 26)
    # 非法 period → 回落无 period 窗口。
    assert _scan_cutoff("annual_report", "20", as_of) == date(2024, 11, 26)
