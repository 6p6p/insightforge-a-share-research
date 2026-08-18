"""Application lifespan: create and tear down shared resources."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import get_logger
from app.core.resources import ApplicationResources
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.research_orchestration.recovery import ResearchOrchestrationRecoveryCoordinator
from app.research_orchestration.service import ResearchOrchestrationService
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_recovery import ResearchExecutionRecoveryCoordinator
from app.services.research_execution_service import ResearchExecutionService
from app.services.workflow_recovery_service import WorkflowRecoveryService
from app.storage.export_store import ExportArtifactStore
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager
from app.workflows.runner import WorkflowRunner

logger = get_logger("app.lifespan")


def _create_research_execution(
    settings,
    sessionmaker,
    langgraph: LangGraphCheckpointManager,
) -> ResearchExecutionService:
    """惰性 runner factory：只在真正启动研究时才构建 Stage4/Stage5 deps。

    生产 factory 从 Settings 构建真实 model（构造不调 API）；自动测试通过
    dependency_overrides 注入 Fake deps，因此启动路径不会触碰真实 LLM。
    """
    from app.stage4.dependencies import create_stage4_dependencies
    from app.stage4.runner import Stage4WorkflowRunner
    from app.stage5.dependencies import create_stage5_dependencies
    from app.stage5.runner import Stage5WorkflowRunner

    def _stage4_factory() -> Stage4WorkflowRunner:
        deps = create_stage4_dependencies(settings, sessionmaker)
        return Stage4WorkflowRunner(sessionmaker, langgraph, deps)

    def _stage5_factory() -> Stage5WorkflowRunner:
        deps = create_stage5_dependencies(settings, sessionmaker)
        return Stage5WorkflowRunner(sessionmaker, langgraph, deps)

    return ResearchExecutionService(
        sessionmaker=sessionmaker,
        checkpoint_manager=langgraph,
        company_identity=CompanyIdentityService(sessionmaker),
        stage4_runner_factory=_stage4_factory,
        stage5_runner_factory=_stage5_factory,
        shutdown_timeout_seconds=settings.workflow_shutdown_timeout_seconds,
    )


def _create_research_orchestration(
    settings,
    sessionmaker,
    langgraph: LangGraphCheckpointManager,
    source_preparation=None,
) -> ResearchOrchestrationService:
    """生产装配顶层编排（7A.2B.2 spec S/T + Gate B）：构造 0 model call / 0 network。

    复用 fulfillment / stage4 / stage5 production factories + PG Checkpointer；
    返回绑定 stage5_runner + orchestration_runner + execution_manager 的 service
    （human action / recovery / API / 后台调度共用同一批实例）。自动测试不触发
    （API 测试 override dependencies；本函数只被 lifespan 调用）。
    """
    from app.research_orchestration.execution_manager import (
        ResearchOrchestrationExecutionManager,
    )
    from app.research_orchestration.factory import (
        create_research_orchestration_dependencies,
        create_research_orchestration_runner,
    )

    deps = create_research_orchestration_dependencies(settings, sessionmaker, langgraph)
    orchestration_runner = create_research_orchestration_runner(
        settings, sessionmaker, langgraph, dependencies=deps
    )
    execution_manager = ResearchOrchestrationExecutionManager(orchestration_runner)
    return ResearchOrchestrationService(
        sessionmaker=sessionmaker,
        plan_service=deps.plan_service,
        stage5_runner=deps.stage5_runner,
        orchestration_runner=orchestration_runner,
        execution_manager=execution_manager,
        source_preparation=source_preparation,
        # P0 backflow manual closure：accept 守卫 + closure 持久化（生产绑定；
        # unit 测试不触发）。
        report_audit_service=deps.stage5_runner.dependencies.report_audit_service,
        report_check_service=deps.stage5_runner.dependencies.report_check_service,
        closure_service=deps.closure_service,
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = application.state.settings
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=settings.database_echo,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    langgraph = LangGraphCheckpointManager(
        connection_uri=to_postgres_connection_uri(settings.database_url)
    )
    sessionmaker = database.session_factory()
    workflow_execution = WorkflowExecutionManager(
        runner=WorkflowRunner(sessionmaker, langgraph),
        shutdown_timeout_seconds=settings.workflow_shutdown_timeout_seconds,
        sessionmaker=sessionmaker,
    )
    # V1.1 P0-2：Source Preparation 生产实例（ingest 后后台 prepare / resume 前
    # 预准备共用；构造 0 network / 0 LLM / 0 DB 连接，模型惰性加载）。
    raw_storage = LocalRawArtifactStore(
        root=settings.raw_storage_root,
        max_bytes=settings.source_max_file_size_bytes,
        max_json_bytes=settings.macro_max_json_response_bytes,
    )
    export_storage = ExportArtifactStore(root=settings.export_storage_root)
    from app.rag.embedding.bge import BGEProvider
    from app.rag.index.service import VectorIndexService
    from app.services.chunking_service import ChunkingService
    from app.services.source_preparation_service import SourcePreparationService

    source_preparation = SourcePreparationService(
        sessionmaker,
        raw_storage,
        ChunkingService(sessionmaker),
        VectorIndexService(
            sessionmaker=sessionmaker,
            embedding_provider=BGEProvider(),
            chroma=chroma,
        ),
    )
    research_execution = _create_research_execution(settings, sessionmaker, langgraph)
    research_orchestration = _create_research_orchestration(
        settings, sessionmaker, langgraph, source_preparation=source_preparation
    )
    resources = ApplicationResources(
        database=database,
        chroma=chroma,
        langgraph=langgraph,
        workflow_execution=workflow_execution,
        research_execution=research_execution,
        research_orchestration=research_orchestration,
        raw_storage=raw_storage,
        export_storage=export_storage,
        source_preparation=source_preparation,
    )
    application.state.resources = resources
    logger.info("application_startup", environment=settings.app_env)
    # LangGraph checkpoint vendor 表（checkpoints / checkpoint_writes /
    # checkpoint_blobs）由 AsyncPostgresSaver.setup() 创建（**不**属于 alembic
    # 业务迁移）；应用启动时幂等确保存在（fresh volume / 空库首次启动必需，
    # 否则 /ready 的 checkpoint 探针报 UndefinedTable）。
    try:
        await langgraph.setup()
    except Exception as exc:  # noqa: BLE001 — PostgreSQL 不可用不阻止启动
        logger.warning("checkpoint_setup_failed", error_type=type(exc).__name__)
    # best-effort reconcile：在接受新 WorkflowRun 前完成；PostgreSQL 不可用不阻止启动
    try:
        recovery = WorkflowRecoveryService(sessionmaker)
        await asyncio.wait_for(
            recovery.reconcile_orphaned_runs(),
            timeout=settings.workflow_reconcile_timeout_seconds,
        )
    except Exception as exc:
        logger.warning(
            "orphaned_runs_reconcile_failed",
            error_type=type(exc).__name__,
        )
    # best-effort 恢复顶层 orchestration（7A.2B.2 spec T）：reconcile 之后——
    # orchestration-owned Stage4/Stage5 child 已被标 FAILED(worker_restarted) →
    # 从同 orchestration_id + 同顶层 thread 恢复；child 仍 RUNNING（live executor /
    # rolling restart）跳过；失败不阻止启动。
    if research_orchestration.orchestration_runner is not None:
        try:
            coordinator = ResearchOrchestrationRecoveryCoordinator(
                sessionmaker, research_orchestration.orchestration_runner
            )
            await asyncio.wait_for(
                coordinator.recover_orchestrations(),
                timeout=settings.workflow_reconcile_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "research_orchestrations_recover_failed",
                error_type=type(exc).__name__,
            )
    # best-effort 恢复研究链（spec E）：legacy coordinator 只处理 non-owned runs
    # （orchestration child 由 research_orchestration_child_runs 归属，NOT EXISTS
    # 排除）；失败不阻止启动。
    try:
        coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, research_execution)
        await asyncio.wait_for(
            coordinator.recover_interrupted_chains(),
            timeout=settings.workflow_reconcile_timeout_seconds,
        )
    except Exception as exc:
        logger.warning(
            "research_chains_recover_failed",
            error_type=type(exc).__name__,
        )
    # V1.1 P0-3/P0-1：production bootstrap（幂等、离线、可观测；失败不阻止启动，
    # 但必须留下明确日志供运维定位）：
    #   1. Source Registry defaults（seed_defaults upsert，不覆盖既有配置）；
    #   2. Company Master（空表 → 导入 bundled versioned snapshot；非空 → skip）。
    if settings.bootstrap_on_startup:
        try:
            from app.services.source_registry_service import SourceRegistryService

            await SourceRegistryService(sessionmaker).seed_defaults()
            logger.info("source_registry_bootstrap_completed")
        except Exception as exc:
            logger.warning(
                "source_registry_bootstrap_failed",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
            )
        try:
            from app.services.company_master_service import CompanyMasterBootstrapService

            result = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
            logger.info(
                "company_master_bootstrap_completed",
                snapshot_version=result.snapshot_version,
                imported_companies=result.imported_companies,
                imported_aliases=result.imported_aliases,
                skipped=result.skipped,
                replayed=result.replayed,
                repair=result.repair,
                error_code=result.error_code,
            )
        except Exception as exc:
            logger.warning(
                "company_master_bootstrap_failed",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
            )
        # V1.1 closure：Issuer Domain registry（issuer_official 受控来源的域名
        # 依据；空表 → 导入 bundled snapshot；非空 → skip）。
        try:
            from app.services.issuer_domain_service import IssuerDomainService

            result = await IssuerDomainService(sessionmaker).bootstrap()
            logger.info(
                "issuer_domain_bootstrap_completed",
                snapshot_version=result.snapshot_version,
                imported_domains=result.imported_domains,
                imported_companies=result.imported_companies,
                skipped=result.skipped,
                replayed=result.replayed,
                repair=result.repair,
                error_code=result.error_code,
            )
        except Exception as exc:
            logger.warning(
                "issuer_domain_bootstrap_failed",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
            )
    try:
        yield
    finally:
        try:
            await resources.workflow_execution.close()
        except Exception as exc:
            logger.warning(
                "workflow_execution_close_failed",
                error_type=type(exc).__name__,
            )
        try:
            await resources.research_execution.close()
        except Exception as exc:
            logger.warning(
                "research_execution_close_failed",
                error_type=type(exc).__name__,
            )
        try:
            if resources.research_orchestration.execution_manager is not None:
                await resources.research_orchestration.execution_manager.close()
        except Exception as exc:
            logger.warning(
                "research_orchestration_execution_close_failed",
                error_type=type(exc).__name__,
            )
        try:
            await resources.langgraph.close()
        except Exception as exc:
            logger.warning("checkpoint_close_failed", error_type=type(exc).__name__)
        await resources.database.dispose()
        application.state.resources = None
        logger.info("application_shutdown")
