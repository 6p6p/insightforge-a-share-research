"""Research planner LLM runtime 单测（stage 7A.1 Gate E）：配置 + factory + adapter。

零网络、零真实 Key：adapter 构造与 model_id 不调用 provider；factory 分派只读
Settings；真实调用路径（generate）用替身捕获 ChatDeepSeek 构造参数与
with_structured_output 链，不发起任何真实网络请求。

覆盖 Gate E 的确定性部分：
- Settings 默认 provider/model = deepseek / deepseek-v4-flash；
- factory 返回生产 `DeepSeekResearchPlannerModel`（model_id = provider:model）；
- 无凭证 → pending_credentials 判定（`has_llm_credentials` False /
  `require_llm_credentials` 抛 `MissingLLMCredentialsError`），不阻塞确定性 Gate；
- 真实调用路径：`DeepSeekResearchPlannerModel.generate` 构造的 ChatDeepSeek 使用
  deepseek-v4-flash、显式传 thinking disabled（`extra_body`）、temperature=0，并把
  `with_structured_output` 链接到 `ResearchPlanPayload`。
"""

import asyncio
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.llm.errors import MissingLLMCredentialsError, UnsupportedLLMProviderError
from app.llm.factory import has_llm_credentials, require_llm_credentials
from app.research_planning.contracts import (
    CompanyIdentitySnapshot,
    ResearchPlannerRequest,
    ResearchPlanPayload,
)
from app.research_planning.planner import (
    DeepSeekResearchPlannerModel,
    create_research_planner_model,
)


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        app_env="test",
        log_level="DEBUG",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )
    base.update(overrides)
    return Settings(**base)


def _request() -> ResearchPlannerRequest:
    return ResearchPlannerRequest(
        task_id=uuid4(),
        company=CompanyIdentitySnapshot(
            security_code="600519",
            official_name="贵州茅台",
            exchange="SSE",
            board="sse_main",
            aliases=["茅台"],
        ),
        research_question="分析公司的经营质量、主要风险和估值。",
        analysis_as_of=date(2026, 8, 10),
    )


def test_default_llm_settings() -> None:
    s = _settings()
    assert s.llm_provider == "deepseek"
    assert s.llm_model == "deepseek-v4-flash"
    assert s.deepseek_api_key is None


def test_factory_returns_deepseek_planner_adapter() -> None:
    model = create_research_planner_model(_settings())
    assert isinstance(model, DeepSeekResearchPlannerModel)
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_factory_accepts_openai_provider() -> None:
    # v1.2.8：非空 provider 直接视为 wrapper（openai-compatible 语义）。
    model = create_research_planner_model(_settings(llm_provider="openai"))
    assert model is not None
    assert model.model_id == "openai:deepseek-v4-flash"


def test_pending_credentials_without_key() -> None:
    """无凭证 → pending_credentials 判定（不发起真实调用、不阻塞确定性 Gate）。"""
    s = _settings()
    assert has_llm_credentials(s) is False
    with pytest.raises(MissingLLMCredentialsError):
        require_llm_credentials(s)
    # 有 key → 可继续。
    assert has_llm_credentials(_settings(deepseek_api_key="sk-x")) is True


def test_planner_generate_passes_thinking_disabled_and_structured_output() -> None:
    """真实调用路径：generate() 构造 ChatDeepSeek = deepseek-v4-flash + thinking
    disabled，并把 with_structured_output 链接到 ResearchPlanPayload。

    用替身捕获构造参数与 structured 链，不发起任何真实网络请求（0 真实 DeepSeek）。
    """
    import langchain_deepseek as lds

    captured = {}

    class _FakeStructured:
        def __init__(self, schema):
            self._schema = schema

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return {
                "raw": SimpleNamespace(
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
                ),
                "parsed": ResearchPlanPayload.model_validate(_payload()),
                "parsing_error": None,
            }

    class _FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, schema, include_raw=True):
            captured["structured_schema"] = schema
            return _FakeStructured(schema)

    original = lds.ChatDeepSeek
    lds.ChatDeepSeek = _FakeChat
    try:
        model = DeepSeekResearchPlannerModel(_settings())
        request = _request()
        payload = asyncio.run(model.generate(request))
    finally:
        lds.ChatDeepSeek = original

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    # temperature=0 不等于关闭 thinking：必须显式传 disabled。
    assert captured["extra_body"]["thinking"]["type"] == "disabled"
    assert captured["temperature"] == 0.0
    assert captured["structured_schema"] is ResearchPlanPayload
    assert isinstance(payload, ResearchPlanPayload)
    # prompt 只含语义输入，不含 task_id / 内部 UUID（spec F）。
    blob = _dump_messages(captured["messages"])
    assert str(request.task_id) not in blob
    assert "600519" in blob
    assert "贵州茅台" in blob


def _dump_messages(messages) -> str:
    import json

    return json.dumps(messages, ensure_ascii=False)


def _payload() -> dict:
    return {
        "research_scope": ["business", "financial", "valuation"],
        "document_needs": [
            {
                "need_code": "annual_report_2024",
                "purpose": "需要 2024 年报",
                "source_type": "annual_report",
                "period": "2024",
            }
        ],
        "financial_needs": [
            {
                "need_code": "revenue_2024",
                "purpose": "需要营收绝对变化",
                "calculation_code": "absolute_change_cny",
                "metric_code": "revenue",
                "period": "2024",
            }
        ],
        "macro_needs": [],
        "event_needs": [],
        "valuation_needs": [{"need_code": "pe_ttm_valuation", "metric_code": "pe_ttm"}],
        "analysis_modules": ["business_event", "financial", "valuation"],
        "research_focus": ["经营质量", "估值水平"],
    }
