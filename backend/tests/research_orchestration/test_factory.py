"""Production factory smoke tests（7A.2B.2 spec S，0 network / 0 DB connect）。

`create_research_orchestration_dependencies` / `create_research_orchestration_runner`
按 Settings 装配完整顶层编排：复用 fulfillment / stage4 / stage5 production
factories + PG Checkpointer。构造阶段 **0 model call / 0 network / 0 DB 连接**
（所有模型 adapter 惰性加载）——本文件只 smoke 装配结果，不执行 graph
（0 real DeepSeek 约束对自动测试仍然成立）。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.urls import to_postgres_connection_uri
from app.research_orchestration.factory import (
    create_research_orchestration_dependencies,
    create_research_orchestration_runner,
)
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import ResearchOrchestrationChildService
from app.workflows.checkpoint import LangGraphCheckpointManager

pytestmark = pytest.mark.asyncio


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        app_name="InsightForge",
        log_level="DEBUG",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )


async def test_dependencies_assembly_shared_instances() -> None:
    """完整装配：复用 fulfillment 内部 plan/router/preparation 同一批实例。

    spec S：顶层编排 graph 的 ensure_plan/ensure_route/prepare 节点与 fulfill
    共享同一服务实例，保证 plan fingerprint / route verify 一致性。构造过程
    若发生任何 DB/网络连接会立即抛错——测试通过即证明 0 network / 0 DB。
    """
    settings = _settings()
    engine = create_async_engine(settings.database_url)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        langgraph = LangGraphCheckpointManager(to_postgres_connection_uri(settings.database_url))
        deps = create_research_orchestration_dependencies(settings, sessionmaker, langgraph)
        # 共享实例：fulfillment 内部 plan/router/preparation 与顶层 deps 同一批。
        assert deps.fulfillment.plan_service is deps.plan_service
        assert deps.fulfillment.router is deps.router
        assert deps.fulfillment.preparation is deps.preparation
        # 全部字段装配齐全。
        assert isinstance(deps.child_service, ResearchOrchestrationChildService)
        assert deps.stage4_runner is not None
        assert deps.stage5_runner is not None
        assert deps.synthesis_service is not None
        assert deps.sessionmaker is sessionmaker

        runner = create_research_orchestration_runner(
            settings, sessionmaker, langgraph, dependencies=deps
        )
        assert isinstance(runner, ResearchOrchestrationRunner)
    finally:
        await engine.dispose()


async def test_runner_rebuilds_dependencies_when_omitted() -> None:
    """`dependencies=None` → factory 自行装配完整 deps（功能等价，spec S）。"""
    settings = _settings()
    engine = create_async_engine(settings.database_url)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        langgraph = LangGraphCheckpointManager(to_postgres_connection_uri(settings.database_url))
        runner = create_research_orchestration_runner(settings, sessionmaker, langgraph)
        assert isinstance(runner, ResearchOrchestrationRunner)
    finally:
        await engine.dispose()
