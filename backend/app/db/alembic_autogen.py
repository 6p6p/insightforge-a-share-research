"""Alembic autogenerate 包含策略（stage 4C final acceptance，drift closeout）。

LangGraph `AsyncPostgresSaver.setup()` 在运行时创建 checkpoint 表
（`checkpoints` / `checkpoint_writes` / `checkpoint_blobs` /
`checkpoint_migrations`），这些表**不是** InsightForge Alembic migration 的
schema owner——由 LangGraph Postgres Checkpointer runtime setup 管理，不应被
autogenerate 当成"metadata 里缺失、应删除/应迁移"的对象。

这里通过 `include_object` 回调精确排除：**只忽略当前真实由 LangGraph
Checkpointer 管理的 exact table names**，不用 `checkpoint*` 模糊通配（避免误伤
未来业务表），也**不排除**任何 application-owned 表（financial / macro /
valuation / claims 等）。普通 InsightForge 表照常进入 autogenerate diff。
"""

from __future__ import annotations

# 当前真实由 LangGraph AsyncPostgresSaver.setup() 管理的 exact table names。
# 修改 LangGraph 版本 / checkpointer 配置时同步审计此集合，不要扩大为通配。
LANGGRAPH_CHECKPOINTER_TABLES = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_writes",
        "checkpoint_blobs",
    }
)

# 自带 .table 属性的对象类型：Table 本身 + 依附于表的 Index / 各种 Constraint。
_OBJECT_TYPES_WITH_TABLE = frozenset(
    {
        "table",
        "index",
        "constraint",
        "unique_constraint",
        "check_constraint",
        "foreign_key_constraint",
        "primary_key_constraint",
    }
)


def is_langgraph_checkpointer_table(table_name: str | None) -> bool:
    """纯函数：该表名是否由 LangGraph Postgres Checkpointer runtime 管理。"""
    return table_name in LANGGRAPH_CHECKPOINTER_TABLES


def include_object(object, name, type_, reflected, compare_to) -> bool:
    """Alembic autogenerate 的 include_object 回调。

    排除 LangGraph checkpoint 表及其索引 / 约束（表、index、constraint 均通过
    `.table` 关联回所属表判断），其余对象一律包含。只作用于 autogenerate
    （`alembic revision --autogenerate` / `alembic check`），不影响迁移执行。
    """
    if type_ in _OBJECT_TYPES_WITH_TABLE:
        if type_ == "table":
            # Table 对象自身没有 `.table` 属性，表名在 `.name` 上。
            table_name = getattr(object, "name", None)
        else:
            # Index / Constraint 通过 `.table` 关联回所属表。
            table = getattr(object, "table", None)
            table_name = getattr(table, "name", None) if table is not None else None
        if is_langgraph_checkpointer_table(table_name):
            return False
    return True
