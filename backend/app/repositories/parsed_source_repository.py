"""Data access for parsed sources (stage 2E.1)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.parsed_source import ParsedSourceModel


class ParsedSourceRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_fingerprint(
        self,
        parse_fingerprint: str,
    ) -> ParsedSourceModel | None:
        result = await self._session.execute(
            select(ParsedSourceModel).where(
                ParsedSourceModel.parse_fingerprint == parse_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def get_by_source_id(self, source_id: UUID) -> ParsedSourceModel | None:
        result = await self._session.execute(
            select(ParsedSourceModel).where(ParsedSourceModel.source_id == source_id)
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self,
        snapshot: ParsedSourceModel,
    ) -> tuple[ParsedSourceModel, bool]:
        """INSERT ... ON CONFLICT(parse_fingerprint) DO NOTHING RETURNING。

        并发下相同 parse 只能有 1 个 ParsedSource：输家回查既有行
        （created=False）并复用其 Blocks，不重复插入（replay 语义）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(snapshot, column.key)
            for column in ParsedSourceModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ParsedSourceModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ParsedSourceModel.parse_fingerprint])
            .returning(ParsedSourceModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(snapshot.parse_fingerprint)
        if existing is None:
            raise RuntimeError("parsed source conflict without existing row")
        return existing, False
