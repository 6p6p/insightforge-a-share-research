"""Data access for evidence cards (stage 3C.1)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.evidence_card import EvidenceCardModel


class EvidenceCardRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, evidence_card_id: UUID) -> EvidenceCardModel | None:
        result = await self._session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id == evidence_card_id)
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, evidence_fingerprint: str) -> EvidenceCardModel | None:
        result = await self._session.execute(
            select(EvidenceCardModel).where(
                EvidenceCardModel.evidence_fingerprint == evidence_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def list_by_company(
        self,
        company_id: UUID,
        limit: int,
        offset: int,
    ) -> list[EvidenceCardModel]:
        stmt = (
            select(EvidenceCardModel)
            .where(EvidenceCardModel.company_id == company_id)
            .order_by(EvidenceCardModel.created_at.asc(), EvidenceCardModel.evidence_card_id.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create_or_get(self, card: EvidenceCardModel) -> tuple[EvidenceCardModel, bool]:
        """INSERT ... ON CONFLICT(evidence_fingerprint) DO NOTHING RETURNING。

        并发下相同 Evidence（同一 fingerprint）只能有 1 张卡：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（修订 = 新 EvidenceCard = 新 fingerprint = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(card, column.key)
            for column in EvidenceCardModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(EvidenceCardModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[EvidenceCardModel.evidence_fingerprint])
            .returning(EvidenceCardModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(card.evidence_fingerprint)
        if existing is None:
            raise RuntimeError("evidence card conflict without existing row")
        return existing, False
