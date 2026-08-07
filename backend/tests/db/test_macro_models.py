"""Structural tests for macro persistence models (stage 2C.2A).

约束的实际强制由 PostgreSQL 承担并在集成测试 test_macro_persistence_schema.py
验证；这里验证模型定义本身（表名、列类型、约束存在性、枚举成员）。
"""

from sqlalchemy import REAL, CheckConstraint, Double, Float, Index, Numeric, UniqueConstraint

from app.db.models import (
    MacroDatasetSnapshotModel,
    MacroObservationModel,
    MacroSeriesModel,
    MacroSnapshotArtifactModel,
)
from app.db.models.macro_observation import MacroObservationModel as Obs
from app.domain.macro_persistence import (
    MacroSnapshotArtifactRole,
    MacroSnapshotStatus,
)
from app.domain.source_records import RawArtifactMediaType


def _constraints(model) -> list[CheckConstraint]:
    return [a for a in model.__table_args__ if isinstance(a, CheckConstraint)]


def _uniques(model) -> list[UniqueConstraint]:
    return [a for a in model.__table_args__ if isinstance(a, UniqueConstraint)]


def _indexes(model) -> list[Index]:
    return [a for a in model.__table_args__ if isinstance(a, Index)]


def _names(items) -> set[str]:
    return {c.name for c in items}


# ------------------------------------------------------------- macro_series


def test_macro_series_table_name() -> None:
    assert MacroSeriesModel.__tablename__ == "macro_series"


def test_macro_series_identity_unique_constraint() -> None:
    unique = _uniques(MacroSeriesModel)
    assert {u.name for u in unique} == {"uq_macro_series_identity"}
    uq = unique[0]
    assert list(uq.columns.keys()) == [
        "provider_key",
        "source_id",
        "external_indicator_id",
        "geography_type",
        "geography_code",
        "frequency",
    ]


def test_macro_series_check_constraints_present() -> None:
    names = _names(_constraints(MacroSeriesModel))
    assert {
        "ck_macro_series_source_id_not_blank",
        "ck_macro_series_external_indicator_id_format",
        "ck_macro_series_geography_type",
        "ck_macro_series_geography_code",
        "ck_macro_series_frequency",
        "ck_macro_series_provider_key_not_blank",
    } <= names


def test_macro_series_foreign_key_to_source_providers() -> None:
    fks = {fk.target_fullname for fk in MacroSeriesModel.__table__.foreign_keys}
    assert "source_providers.provider_key" in fks
    assert list(fks) == ["source_providers.provider_key"]


# ----------------------------------------------- macro_dataset_snapshots


def test_macro_dataset_snapshot_table_name() -> None:
    assert MacroDatasetSnapshotModel.__tablename__ == "macro_dataset_snapshots"


def test_macro_dataset_snapshot_fingerprint_unique() -> None:
    unique = _uniques(MacroDatasetSnapshotModel)
    assert {u.name for u in unique} == {"uq_macro_dataset_snapshots_fingerprint"}
    assert list(unique[0].columns.keys()) == ["snapshot_fingerprint"]


def test_macro_dataset_snapshot_check_constraints_present() -> None:
    names = _names(_constraints(MacroDatasetSnapshotModel))
    assert {
        "ck_macro_dataset_snapshots_fingerprint",
        "ck_macro_dataset_snapshots_requested_country_code",
        "ck_macro_dataset_snapshots_year_range",
        "ck_macro_dataset_snapshots_year_span",
        "ck_macro_dataset_snapshots_source_id_not_blank",
        "ck_macro_dataset_snapshots_iso3_format",
        "ck_macro_dataset_snapshots_iso2_format",
        "ck_macro_dataset_snapshots_indicator_name_not_blank",
        "ck_macro_dataset_snapshots_source_name_not_blank",
        "ck_macro_dataset_snapshots_geography_name_not_blank",
        "ck_macro_dataset_snapshots_page",
        "ck_macro_dataset_snapshots_pages",
        "ck_macro_dataset_snapshots_page_le_pages",
        "ck_macro_dataset_snapshots_per_page",
        "ck_macro_dataset_snapshots_provider_total",
        "ck_macro_dataset_snapshots_request_count",
        "ck_macro_dataset_snapshots_acquisition_method",
        "ck_macro_dataset_snapshots_authority_tier_snapshot",
        "ck_macro_dataset_snapshots_topics_array",
        "ck_macro_dataset_snapshots_provider_capabilities_array",
        "ck_macro_dataset_snapshots_status",
    } <= names


def test_macro_dataset_snapshot_fetched_at_desc_index() -> None:
    indexes = _indexes(MacroDatasetSnapshotModel)
    desc_index = next(i for i in indexes if i.name == "ix_macro_dataset_snapshots_fetched_at")
    assert "fetched_at" in str(desc_index)


# ------------------------------------------------ macro_snapshot_artifacts


def test_macro_snapshot_artifact_table_name() -> None:
    assert MacroSnapshotArtifactModel.__tablename__ == "macro_snapshot_artifacts"


def test_macro_snapshot_artifact_role_page_unique_nulls_not_distinct() -> None:
    unique = {u.name: u for u in _uniques(MacroSnapshotArtifactModel)}
    uq = unique["uq_macro_snapshot_artifacts_snapshot_role_page"]
    assert list(uq.columns.keys()) == ["snapshot_id", "role", "page"]
    assert uq.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_macro_snapshot_artifact_check_constraints_present() -> None:
    names = _names(_constraints(MacroSnapshotArtifactModel))
    assert {
        "ck_macro_snapshot_artifacts_role",
        "ck_macro_snapshot_artifacts_role_page",
        "ck_macro_snapshot_artifacts_response_status",
        "ck_macro_snapshot_artifacts_final_hostname",
    } <= names


def test_macro_snapshot_artifact_foreign_keys() -> None:
    fks = {fk.target_fullname for fk in MacroSnapshotArtifactModel.__table__.foreign_keys}
    assert "macro_dataset_snapshots.snapshot_id" in fks
    assert "raw_artifacts.artifact_id" in fks


# ------------------------------------------------------- macro_observations


def test_macro_observation_table_name() -> None:
    assert MacroObservationModel.__tablename__ == "macro_observations"


def test_macro_observation_value_is_numeric_not_float() -> None:
    col = Obs.__table__.c.value_numeric
    assert isinstance(col.type, Numeric)
    assert not isinstance(col.type, Float)
    assert not isinstance(col.type, Double)
    assert not isinstance(col.type, REAL)


def test_macro_observation_check_constraints_present() -> None:
    names = _names(_constraints(MacroObservationModel))
    assert {
        "ck_macro_observations_period",
        "ck_macro_observations_normalized_period_start",
        "ck_macro_observations_period_semantics",
        "ck_macro_observations_frequency",
        "ck_macro_observations_value_is_missing",
        "ck_macro_observations_missing_scale",
        "ck_macro_observations_present_scale",
    } <= names


def test_macro_observation_snapshot_period_unique() -> None:
    unique = _uniques(MacroObservationModel)
    assert {u.name for u in unique} == {"uq_macro_observations_snapshot_period"}
    assert list(unique[0].columns.keys()) == ["snapshot_id", "period"]


def test_macro_observation_foreign_key_cascade() -> None:
    fk = MacroObservationModel.__table__.c.snapshot_id.foreign_keys.pop()
    assert fk.target_fullname == "macro_dataset_snapshots.snapshot_id"
    assert fk.ondelete == "CASCADE"


# ------------------------------------------------------------- registration


def test_models_registered_in_package() -> None:
    # app.db.models 显式导出四个新模型
    assert MacroSeriesModel.__name__ == "MacroSeriesModel"
    assert MacroDatasetSnapshotModel.__name__ == "MacroDatasetSnapshotModel"
    assert MacroSnapshotArtifactModel.__name__ == "MacroSnapshotArtifactModel"
    assert MacroObservationModel.__name__ == "MacroObservationModel"


# ------------------------------------------------------------------ domain


def test_raw_artifact_media_type_has_json() -> None:
    assert RawArtifactMediaType.JSON.value == "application/json"
    assert RawArtifactMediaType.PDF.value == "application/pdf"


def test_macro_snapshot_artifact_role_members() -> None:
    assert {r.value for r in MacroSnapshotArtifactRole} == {
        "indicator_metadata",
        "country_metadata",
        "observations_page",
    }


def test_macro_snapshot_status_only_available() -> None:
    assert {s.value for s in MacroSnapshotStatus} == {"available"}
