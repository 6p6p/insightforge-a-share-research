"""Data access for macro series identity (stage 2C.2A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.macro_series import MacroSeriesModel


class MacroSeriesRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers.

    Series 是稳定身份：不提供 update 方法，创建后不可变。get_or_create 使用
    PostgreSQL ON CONFLICT 保证并发下相同身份只保留一行。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_identity(
        self,
        *,
        provider_key: str,
        source_id: str,
        external_indicator_id: str,
        geography_type: str,
        geography_code: str,
        frequency: str,
    ) -> MacroSeriesModel | None:
        result = await self._session.execute(
            select(MacroSeriesModel).where(
                MacroSeriesModel.provider_key == provider_key,
                MacroSeriesModel.source_id == source_id,
                MacroSeriesModel.external_indicator_id == external_indicator_id,
                MacroSeriesModel.geography_type == geography_type,
                MacroSeriesModel.geography_code == geography_code,
                MacroSeriesModel.frequency == frequency,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, series_id: UUID) -> MacroSeriesModel | None:
        result = await self._session.execute(
            select(MacroSeriesModel).where(MacroSeriesModel.series_id == series_id)
        )
        return result.scalar_one_or_none()

    async def create(self, series: MacroSeriesModel) -> MacroSeriesModel:
        self._session.add(series)
        await self._session.flush()
        return series

    async def get_or_create(self, series: MacroSeriesModel) -> tuple[MacroSeriesModel, bool]:
        """ON CONFLICT DO UPDATE（no-op）：并发下相同身份只保留一行。

        命中冲突时 DO UPDATE 会等待冲突事务提交后读回既有行并执行 no-op
        SET（created_at = 既有行的 created_at），避免 DO NOTHING + 回查在
        竞争窗口下查不到未提交行的竞态。created=True 表示本次插入成功，
        False 表示复用既有行。
        """
        stmt = (
            insert(MacroSeriesModel)
            .values(
                series_id=series.series_id,
                provider_key=series.provider_key,
                source_id=series.source_id,
                external_indicator_id=series.external_indicator_id,
                geography_type=series.geography_type,
                geography_code=series.geography_code,
                frequency=series.frequency,
            )
            .on_conflict_do_update(
                index_elements=[
                    MacroSeriesModel.provider_key,
                    MacroSeriesModel.source_id,
                    MacroSeriesModel.external_indicator_id,
                    MacroSeriesModel.geography_type,
                    MacroSeriesModel.geography_code,
                    MacroSeriesModel.frequency,
                ],
                set_={"created_at": MacroSeriesModel.created_at},
            )
            .returning(MacroSeriesModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one()
        return row, row.series_id == series.series_id
