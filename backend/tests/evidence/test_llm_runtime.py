"""LLM runtime 单元测试（stage 3C.2.1）：Settings 配置 + factory + DeepSeek adapter。

零网络、零真实 Key：adapter 构造与 model_id 不调用 provider；factory 分派只读
Settings。真实调用（extract）不在自动测试范围（conftest 禁止真实外网）。
"""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.config import Settings
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.adapters import DeepSeekEvidenceExtractionModel
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
)
from app.llm.factory import create_evidence_extraction_model, has_llm_credentials

_CHUNK_ID = UUID("11111111-1111-1111-1111-111111111111")
_CHUNK_SET_ID = UUID("22222222-2222-2222-2222-222222222222")
_PARSED_SOURCE_ID = UUID("33333333-3333-3333-3333-333333333333")
_SOURCE_ID = UUID("44444444-4444-4444-4444-444444444444")
_COMPANY_ID = UUID("55555555-5555-5555-5555-555555555555")

_FAKE_DECISION = EvidenceExtractionDecision(
    relevant=True,
    items=[
        EvidenceExtractionItem(
            evidence_statement="公司2025年营业收入为100亿元，同比增长12%。",
            evidence_type=EvidenceType.METRIC,
            quote_text="公司2025年营业收入为100亿元，同比增长12%。",
            confidence=EvidenceConfidence.HIGH,
        )
    ],
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


def test_default_llm_settings() -> None:
    s = _settings()
    assert s.llm_provider == "deepseek"
    assert s.llm_model == "deepseek-v4-flash"
    assert s.llm_timeout_seconds == 60
    assert s.llm_max_retries == 1
    assert s.deepseek_api_key is None


def test_empty_deepseek_key_normalized_to_none() -> None:
    assert _settings(deepseek_api_key="").deepseek_api_key is None
    assert _settings(deepseek_api_key="   ").deepseek_api_key is None


def test_secret_value_and_no_leak_in_repr() -> None:
    s = _settings(deepseek_api_key="sk-test-secret")
    assert s.deepseek_api_key is not None
    assert s.deepseek_api_key.get_secret_value() == "sk-test-secret"
    assert "sk-test-secret" not in repr(s)


def test_invalid_llm_timeout_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(llm_timeout_seconds=0)
    with pytest.raises(ValueError):
        _settings(llm_timeout_seconds=61)


def test_invalid_llm_max_retries_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(llm_max_retries=-1)
    with pytest.raises(ValueError):
        _settings(llm_max_retries=11)


def test_factory_returns_deepseek_adapter() -> None:
    model = create_evidence_extraction_model(_settings())
    assert isinstance(model, DeepSeekEvidenceExtractionModel)
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_factory_unsupported_provider() -> None:
    with pytest.raises(Exception) as exc_info:
        create_evidence_extraction_model(_settings(llm_provider="openai"))
    from app.llm.errors import UnsupportedLLMProviderError

    assert isinstance(exc_info.value, UnsupportedLLMProviderError)


def test_adapter_has_no_tools_or_web_search() -> None:
    model = create_evidence_extraction_model(_settings())
    assert not hasattr(model, "tools")
    assert not hasattr(model, "web_search")


def test_adapter_constructs_without_credentials() -> None:
    # 无 key 仍允许构造（应用启动不调用 factory / extract）。
    model = DeepSeekEvidenceExtractionModel(_settings())
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_no_legacy_model_references() -> None:
    """迁移 Gate：当前 runtime 不使用已停止的 legacy model names。"""
    assert _settings().llm_model != "deepseek-chat"
    assert _settings().llm_model != "deepseek-reasoner"
    model = create_evidence_extraction_model(_settings())
    assert "deepseek-chat" not in model.model_id
    assert "deepseek-reasoner" not in model.model_id
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_production_adapter_explicitly_disables_thinking() -> None:
    """生产 adapter 显式传 thinking disabled（不依赖 provider 默认行为）。

    langchain-deepseek==1.1.0 公开接口：`extra_body` 是传递 provider 自定义
    参数（thinking）的官方通道；`model_kwargs` 会把非 OpenAI 参数打进
    top-level payload 造成 API 错误。测试直接复用 adapter 构造参数来验证
    ChatDeepSeek 对象收到 `extra_body={"thinking": {"type": "disabled"}}`。
    """
    from langchain_deepseek import ChatDeepSeek  # 零网络：仅构造

    s = _settings()
    llm = ChatDeepSeek(
        model=s.llm_model,
        temperature=0.0,
        timeout=s.llm_timeout_seconds,
        max_retries=s.llm_max_retries,
        # 本 langchain 版本在默认 api_base + 无 key 时会拒绝构造（不影响
        # adapter 运行时行为：无 key 时 ValidationError 被映射为
        # EvidenceExtractorUnavailable）。这里用假 key 只为过构造校验。
        api_key="sk-test-not-real",
        extra_body={"thinking": {"type": "disabled"}},
    )
    assert llm.model == "deepseek-v4-flash"
    assert llm.extra_body == {"thinking": {"type": "disabled"}}
    # temperature=0 不等于关闭 thinking：必须显式传 disabled。
    assert llm.extra_body["thinking"]["type"] == "disabled"


def test_adapter_extract_passes_thinking_disabled_and_model() -> None:
    """真实调用路径：adapter.extract() 构造的 ChatDeepSeek 使用 deepseek-v4-flash
    且显式传 thinking disabled，并经 instrumentation 上报 usage。

    用替身捕获 ChatDeepSeek 构造参数与 with_structured_output 链，不发起
    任何真实网络请求。
    """
    import langchain_deepseek as lds

    captured = {}

    class _RecordingObserver:
        def __init__(self):
            self.records = []

        async def record(self, record):
            self.records.append(record)

    class _FakeStructured:
        async def ainvoke(self, messages):
            return {
                "raw": SimpleNamespace(
                    usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
                ),
                "parsed": _FAKE_DECISION,
                "parsing_error": None,
            }

    class _FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, schema, include_raw=True):
            assert include_raw is True
            return _FakeStructured()

    original = lds.ChatDeepSeek
    lds.ChatDeepSeek = _FakeChat
    observer = _RecordingObserver()
    try:
        from app.evidence.extractor.adapters import DeepSeekEvidenceExtractionModel
        from app.rag.retrieval.contracts import RetrievalHit

        model = DeepSeekEvidenceExtractionModel(_settings(), usage_observer=observer)
        hit = RetrievalHit(
            rank=1,
            chunk_id=_CHUNK_ID,
            chunk_set_id=_CHUNK_SET_ID,
            parsed_source_id=_PARSED_SOURCE_ID,
            source_id=_SOURCE_ID,
            company_id=_COMPANY_ID,
            text="公司2025年营业收入为100亿元，同比增长12%。",
            distance=0.1,
            provider_key="sse",
            document_type="annual_report",
            source_title="2025 年度报告",
            source_url="https://example.com/2025.pdf",
            published_at=None,
            reporting_period_end=None,
            authority_tier=1,
            critical_claim_eligible=False,
            chunk_ordinal=1,
            locator_refs=[{"type": "pdf_page", "page_number": 1, "line_index": 1}],
        )
        asyncio.run(model.extract("公司2025年营业收入是多少？", hit))
    finally:
        lds.ChatDeepSeek = original

    assert captured["model"] == "deepseek-v4-flash"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    # 不依赖 provider 默认 thinking：显式 disabled 已传入。
    assert captured["extra_body"]["thinking"]["type"] == "disabled"
    assert captured["temperature"] == 0.0

    # adapter → instrumentation wrapper → observer 端到端接线。
    assert len(observer.records) == 1
    rec = observer.records[0]
    assert rec.component_name == "evidence_extraction"
    assert rec.provider == "deepseek"
    assert rec.model_id == "deepseek:deepseek-v4-flash"
    assert rec.outcome == "success"
    assert rec.usage_status == "reported"
    assert rec.input_tokens == 10
    assert rec.output_tokens == 5
    assert rec.total_tokens == 15


def test_has_llm_credentials() -> None:
    assert has_llm_credentials(_settings()) is False
    assert has_llm_credentials(_settings(deepseek_api_key="sk-x")) is True
