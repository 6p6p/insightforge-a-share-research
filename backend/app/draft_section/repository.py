"""Data access for evidence-bound section drafts (stage 5B)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.draft_section import DraftSectionModel


class DraftSectionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, draft_section_id: UUID) -> DraftSectionModel | None:
        result = await self._session.execute(
            select(DraftSectionModel).where(DraftSectionModel.draft_section_id == draft_section_id)
        )
        return result.scalar_one_or_none()

    async def get_by_writer_input_fingerprint(
        self, writer_input_fingerprint: str
    ) -> DraftSectionModel | None:
        result = await self._session.execute(
            select(DraftSectionModel).where(
                DraftSectionModel.writer_input_fingerprint == writer_input_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, draft: DraftSectionModel) -> tuple[DraftSectionModel, bool]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING（无目标索引）。

        并发下相同 writer 输入（同一指纹）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（输入 / schema / writer 任一变化 = 新指纹 = 新行）。

        注意：不用 `ON CONFLICT (writer_input_fingerprint)`，因为模型同时有
        `uq_draft_sections_writer_input_fingerprint` 与 `uq_draft_sections_section_fingerprint`
        两个唯一约束。并发相同输入时输家行同时违反两个索引，PostgreSQL 先检查到
        哪个索引不确定——若先命中 section_fingerprint，带目标索引的 ON CONFLICT 不会
        抑制它 → UniqueViolation。无目标索引的 `ON CONFLICT DO NOTHING` 抑制**任意**
        唯一约束冲突，随后回查既有行即得真实行（相同指纹 ⟹ 相同内容）。
        """
        excluded = {"created_at"}
        values = {}
        for column in DraftSectionModel.__table__.columns:
            key = column.key
            if key in excluded:
                continue
            value = getattr(draft, key)
            # P1：server_default 列（status）未显式赋值时交给 DB 默认（保持
            # status='completed'），避免显式传 None 违反 NOT NULL；显式 degraded
            # 状态仍会写入。
            if value is None and column.server_default is not None:
                continue
            values[key] = value
        stmt = (
            insert(DraftSectionModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(DraftSectionModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_writer_input_fingerprint(draft.writer_input_fingerprint)
        if existing is None:
            raise RuntimeError("draft section conflict without existing row")
        return existing, False
