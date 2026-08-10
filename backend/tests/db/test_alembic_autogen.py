"""Alembic autogenerate include_object 策略测试（stage 4C final acceptance B3）。

验证 LangGraph AsyncPostgresSaver runtime 管理的 checkpoint 表在 autogenerate
diff 中被排除（已知 LangGraph external table → excluded），而普通 InsightForge
表照常包含（normal table → included）。纯函数 + include_object 回调均可直接单测，
无需真实 DB。
"""

from sqlalchemy import Column, Index, Integer, MetaData, Table, UniqueConstraint

from app.db.alembic_autogen import (
    LANGGRAPH_CHECKPOINTER_TABLES,
    include_object,
    is_langgraph_checkpointer_table,
)


def _table(name: str) -> Table:
    return Table(name, MetaData(), Column("id", Integer, primary_key=True))


def test_checkpointer_tables_are_exact_names() -> None:
    assert LANGGRAPH_CHECKPOINTER_TABLES == frozenset(
        {"checkpoint_migrations", "checkpoints", "checkpoint_writes", "checkpoint_blobs"}
    )


def test_known_langgraph_tables_excluded() -> None:
    for name in sorted(LANGGRAPH_CHECKPOINTER_TABLES):
        assert is_langgraph_checkpointer_table(name) is True
        assert include_object(_table(name), name, "table", reflected=True, compare_to=None) is False


def test_normal_insightforge_table_included() -> None:
    assert is_langgraph_checkpointer_table("financial_calculations") is False
    assert (
        include_object(
            _table("financial_calculations"),
            "financial_calculations",
            "table",
            reflected=True,
            compare_to=None,
        )
        is True
    )


def test_future_application_table_not_wildcard_excluded() -> None:
    # 不把未来业务表（如 claim_synthesis_runs）误伤：exact-name 排除，不用通配。
    assert is_langgraph_checkpointer_table("claim_synthesis_runs") is False
    assert (
        include_object(
            _table("claim_synthesis_runs"),
            "claim_synthesis_runs",
            "table",
            reflected=True,
            compare_to=None,
        )
        is True
    )


def test_langgraph_index_excluded() -> None:
    table = _table("checkpoints")
    idx = Index("checkpoints_thread_id_idx", table.c.id)
    assert (
        include_object(idx, "checkpoints_thread_id_idx", "index", reflected=True, compare_to=None)
        is False
    )


def test_application_index_included() -> None:
    table = _table("financial_calculation_inputs")
    idx = Index("ix_financial_calculation_inputs_metric_observation_id", table.c.id)
    assert (
        include_object(
            idx,
            "ix_financial_calculation_inputs_metric_observation_id",
            "index",
            reflected=True,
            compare_to=None,
        )
        is True
    )


def test_langgraph_constraint_excluded() -> None:
    table = _table("checkpoint_writes")
    uc = UniqueConstraint(table.c.id, name="uq_checkpoint_writes_thread_id")
    assert (
        include_object(
            uc,
            "uq_checkpoint_writes_thread_id",
            "unique_constraint",
            reflected=True,
            compare_to=None,
        )
        is False
    )


def test_application_constraint_included() -> None:
    table = _table("financial_calculation_inputs")
    uc = UniqueConstraint(
        table.c.id,
        name="uq_financial_calculation_inputs_calc_observation",
    )
    assert (
        include_object(
            uc,
            "uq_financial_calculation_inputs_calc_observation",
            "unique_constraint",
            reflected=True,
            compare_to=None,
        )
        is True
    )


def test_none_table_name_not_excluded() -> None:
    assert is_langgraph_checkpointer_table(None) is False
