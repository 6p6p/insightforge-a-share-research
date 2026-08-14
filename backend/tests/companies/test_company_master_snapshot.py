"""Unit tests: company master snapshot contract & validation (V1.1 P0-1).

0 DB / 0 network / 0 LLM：纯数据契约测试。
"""

import json

import pytest

from app.companies.master.snapshot import (
    BUNDLED_SNAPSHOT_PATH,
    CompanyMasterEntry,
    CompanyMasterSnapshot,
    board_for_code,
    exchange_for_code,
    load_bundled_snapshot,
)


def _entry(**overrides) -> CompanyMasterEntry:
    base = dict(
        security_code="600519",
        exchange="SSE",
        board="sse_main",
        listing_status="listed",
        listing_date="2001-08-27",
        official_name="贵州茅台酒股份有限公司",
        short_name="贵州茅台",
    )
    base.update(overrides)
    return CompanyMasterEntry.model_validate(base)


def test_exchange_and_board_derivation() -> None:
    cases = {
        "600519": ("SSE", "sse_main"),
        "688981": ("SSE", "star"),
        "000001": ("SZSE", "szse_main"),
        "300750": ("SZSE", "chinext"),
        "920002": ("BSE", "bse"),
        "830799": ("BSE", "bse"),
    }
    for code, (exchange, board) in cases.items():
        assert exchange_for_code(code) == exchange
        assert board_for_code(code, exchange) == board
    assert exchange_for_code("900901") is None  # 沪 B 股不在 A 股主数据


def test_entry_validation_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        _entry(security_code="60051")
    with pytest.raises(ValueError):
        _entry(security_code="60051A")
    with pytest.raises(ValueError):
        _entry(exchange="NYSE")
    with pytest.raises(ValueError):
        _entry(official_name="")
    with pytest.raises(ValueError):
        _entry(listing_status="suspended")


def test_snapshot_consistency_rejects_duplicates_and_mismatch() -> None:
    a = _entry()
    b = _entry(security_code="600519", short_name="茅台")  # 同 identity_key
    snap = CompanyMasterSnapshot(
        snapshot_version="test-v1",
        companies=[a, b] * 3000,  # 满足规模下限
    )
    with pytest.raises(ValueError, match="duplicate identity_key"):
        snap.validate_consistency()

    c = _entry(security_code="300750", exchange="SZSE", board="chinext")
    d = _entry(security_code="688981", exchange="SSE", board="sse_main")  # 688 → star
    snap2 = CompanyMasterSnapshot(snapshot_version="test-v2", companies=[c, d] * 3000)
    with pytest.raises(ValueError, match="inconsistent with prefix"):
        snap2.validate_consistency()


def test_snapshot_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="unsupported company master snapshot schema"):
        CompanyMasterSnapshot(
            schema_version=99,
            snapshot_version="v1",
            companies=[_entry()] * 6000,
        )


def test_snapshot_rejects_demo_scale() -> None:
    with pytest.raises(ValueError, match="must cover"):
        CompanyMasterSnapshot(
            snapshot_version="demo-v1",
            companies=[_entry()],
        ).validate_consistency()


def test_bundled_snapshot_loads_and_validates() -> None:
    loaded = load_bundled_snapshot()
    assert loaded.content_sha256 and len(loaded.content_sha256) == 64
    assert len(loaded.snapshot.companies) > 5000
    assert loaded.snapshot.snapshot_version.startswith("company-master-v1-")
    assert BUNDLED_SNAPSHOT_PATH.exists()
    # 关键公司存在。
    by_code = {c.security_code: c for c in loaded.snapshot.companies}
    assert by_code["600519"].official_name == "贵州茅台酒股份有限公司"
    assert by_code["600519"].short_name == "贵州茅台"
    assert by_code["300750"].official_name == "宁德时代新能源科技股份有限公司"
    assert by_code["300750"].board == "chinext"


def test_bundled_snapshot_aliases_gt_companies() -> None:
    loaded = load_bundled_snapshot()
    alias_count = sum(2 + len(entry.former_names) for entry in loaded.snapshot.companies)
    assert alias_count > len(loaded.snapshot.companies)


def test_snapshot_json_is_stable_format() -> None:
    raw = json.loads(BUNDLED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert {"sources", "companies", "snapshot_version", "as_of"} <= raw.keys()
    for source in raw["sources"]:
        assert source["exchange"] in ("SSE", "SZSE", "BSE")
        assert source["source_name"]
        assert source["url"]
        assert source["row_count"] > 0
