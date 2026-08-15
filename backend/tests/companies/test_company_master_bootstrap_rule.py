"""Unit tests: Company Master bootstrap consistency rule (V1.1 self-heal).

0 DB：`master_data_missing` 是纯函数判定——marker 存在时实际数据为空表即视为
缺失（不允许 replay 掩盖数据丢失）；非空（即使与 snapshot count 不一致）一律
尊重现有数据（Case D）。
"""

from app.services.company_master_service import (
    CompanyMasterImportResult,
    master_data_missing,
)


def test_master_data_missing_when_both_empty() -> None:
    assert master_data_missing(0, 0) is True


def test_master_data_missing_when_aliases_empty() -> None:
    # companies 有行但 aliases 空 → 数据明显不完整 → 缺失（修复）。
    assert master_data_missing(5543, 0) is True
    assert master_data_missing(1, 0) is True


def test_master_data_missing_when_companies_empty() -> None:
    assert master_data_missing(0, 11098) is True


def test_master_data_complete_when_nonempty() -> None:
    # 非空（即使 count 与 snapshot 不完全一致）→ 视为存在，不 repair 不重灌。
    assert master_data_missing(5543, 11098) is False
    assert master_data_missing(1, 1) is False
    assert master_data_missing(5400, 10900) is False


def test_import_result_repair_flag_defaults() -> None:
    result = CompanyMasterImportResult(
        snapshot_version="v",
        content_sha256="0" * 64,
        imported_companies=0,
        imported_aliases=0,
        skipped=False,
        replayed=True,
    )
    assert result.repair is False
    repaired = CompanyMasterImportResult(
        snapshot_version="v",
        content_sha256="0" * 64,
        imported_companies=5543,
        imported_aliases=11098,
        skipped=False,
        replayed=False,
        repair=True,
    )
    assert repaired.repair is True
