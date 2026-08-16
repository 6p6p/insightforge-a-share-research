"""Database engine and session lifecycle management."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseManager:
    """Owns the async engine, session factory and connectivity probing."""

    def __init__(
        self,
        database_url: str,
        echo: bool = False,
        connect_timeout_seconds: int = 5,
    ) -> None:
        # 池参数：编排执行会并发拉起多个 executor / LangGraph checkpoint
        # 写（fulfill 各 need 并行、stage4/5 子 workflow）——默认 QueuePool
        # (5+10) 在真实运行中会被瞬时打满导致 ConnectionTimeout；放宽容量
        # 并提高排队容忍（连接在编排内是短生命周期会话，不存在无界增长）。
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=20,
            max_overflow=30,
            pool_timeout=30,
            connect_args={"connect_timeout": connect_timeout_seconds},
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    async def ping(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self._engine.dispose()
