"""Evaluation 复用生产 factory 注入 usage_observer（7B.1.2B spec A/B）。

目标：Evaluation 通过**生产 factory 边界**（`create_research_orchestration_dependencies`）
注入**同一个** `LlmUsageObserver`，让全部 10 个 production LLM adapter（planner /
evidence extractor / 5×Stage4 / draft / audit / revision）拿到同一 collector，
而无需为 Evaluation 单独实现一套 Full factory。

- 不直接 `DeepSeekX(settings, observer)` —— 必须走生产 factory；
- 通过 monkeypatch 各 adapter 构造器记录 observer 身份（构造阶段 0 LLM call /
  0 network / 0 DB 连接；所有 model adapter 惰性加载）；
- 生产默认（不传 observer）仍能装配，且 observer 为 None。
"""

import importlib

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.urls import to_postgres_connection_uri
from app.research_orchestration.factory import create_research_orchestration_dependencies
from app.workflows.checkpoint import LangGraphCheckpointManager

pytestmark = pytest.mark.asyncio

# 生产 factory 边界的 10 个 LLM adapter（模块路径 → 类名）。
# evidence extractor 在 fulfillment(document) 与 backflow 各构造一次 → 共 11 次。
_ADAPTERS: tuple[tuple[str, str], ...] = (
    ("app.research_planning.planner", "DeepSeekResearchPlannerModel"),
    ("app.evidence.extractor.adapters", "DeepSeekEvidenceExtractionModel"),
    ("app.analysis.claims.adapters", "DeepSeekClaimAnalysisModel"),
    ("app.analysis.financial.adapters", "DeepSeekFinancialAnalysisModel"),
    ("app.analysis.macro.adapters", "DeepSeekMacroAnalysisModel"),
    ("app.analysis.synthesis.adapters", "DeepSeekSynthesisAnalysisModel"),
    ("app.analysis.valuation.adapters", "DeepSeekValuationAnalysisModel"),
    ("app.draft_section.adapters", "DeepSeekDraftSectionModel"),
    ("app.audit.adapters", "DeepSeekAuditModel"),
    ("app.revision.adapters", "DeepSeekRevisionWriterModel"),
)


class _RecordingObserver:
    """最小 observer：只暴露稳定身份；测试不触发任何 LLM call，record 不会被调用。"""

    def __init__(self) -> None:
        self.records: list = []

    async def record(self, record) -> None:  # noqa: D401 — 仅占位
        self.records.append(record)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        app_name="InsightForge",
        log_level="DEBUG",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )


def _patch_adapter_constructors(monkeypatch, observer_log: list[tuple[str, object]]) -> None:
    """monkeypatch 各 adapter 的 `__init__`，记录构造时的 usage_observer 身份。"""
    for module_name, class_name in _ADAPTERS:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        original_init = cls.__init__

        def wrapper(self, settings, usage_observer=None, _orig=original_init, _name=class_name):
            observer_log.append((_name, usage_observer))
            return _orig(self, settings, usage_observer=usage_observer)

        monkeypatch.setattr(cls, "__init__", wrapper)


async def test_full_factory_threads_single_observer_to_all_10_adapters(monkeypatch) -> None:
    """生产 factory 边界：注入的同一个 observer 到达全部 10 个 adapter。

    evidence extractor 走 document + backflow 两条路径构造两次，两次都应拿到
    同一个 observer；构造过程若发生任何 DB/网络连接会立即抛错（0 network）。
    """
    settings = _settings()
    observer = _RecordingObserver()
    constructed: list[tuple[str, object]] = []
    engine = create_async_engine(settings.database_url)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        langgraph = LangGraphCheckpointManager(to_postgres_connection_uri(settings.database_url))
        _patch_adapter_constructors(monkeypatch, constructed)

        deps = create_research_orchestration_dependencies(
            settings, sessionmaker, langgraph, usage_observer=observer
        )
        assert deps is not None

        # 10 个 distinct adapter 类全部被构造。
        distinct = {name for name, _ in constructed}
        assert distinct == {name for _, name in _ADAPTERS}
        # 11 次构造：evidence extractor 在 document 与 backflow 各一次。
        assert len(constructed) == 11
        evidence_constructs = [n for n, _ in constructed if n == "DeepSeekEvidenceExtractionModel"]
        assert len(evidence_constructs) == 2
        # 每一次构造都收到**同一个** observer 实例。
        for _, obs in constructed:
            assert obs is observer
    finally:
        await engine.dispose()


async def test_factory_default_observer_is_none(monkeypatch) -> None:
    """生产默认：不传 observer → 全部 adapter 收到 None，仍能完整装配。"""
    settings = _settings()
    constructed: list[tuple[str, object]] = []
    engine = create_async_engine(settings.database_url)
    try:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        langgraph = LangGraphCheckpointManager(to_postgres_connection_uri(settings.database_url))
        _patch_adapter_constructors(monkeypatch, constructed)

        deps = create_research_orchestration_dependencies(settings, sessionmaker, langgraph)
        assert deps is not None

        assert {name for name, _ in constructed} == {name for _, name in _ADAPTERS}
        for _, obs in constructed:
            assert obs is None
    finally:
        await engine.dispose()
