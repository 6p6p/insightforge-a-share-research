"""Default research intent generator unit tests (P0: Company Only Research Flow).

- template 确定性（同输入 → 同文本，replay 兼容）；
- 模块短语映射 / 空模块 fallback / 长度边界；
- optional LLM enhancement：成功返回、失败降级 template、输出校验拒绝
  非法文本（空 / 超长 / internal ID-like）；
- settings 开关 → factory 装配（默认关闭 → 0 新增 LLM）。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.llm.errors import UnsupportedLLMProviderError
from app.research_planning.contracts import CompanyIdentitySnapshot
from app.research_planning.intent import (
    MAX_GENERATED_QUESTION_LENGTH,
    DefaultResearchIntentGenerator,
    IntentEnhancementOutput,
    IntentEnhancementRequest,
    IntentEnhancementUnavailable,
    build_intent_messages,
    build_intent_template,
    create_intent_enhancement_model,
)

_COMPANY = CompanyIdentitySnapshot(
    security_code="300750",
    official_name="宁德时代新能源科技股份有限公司",
    exchange="SZSE",
    board="chinext",
    aliases=["宁德时代", "CATL"],
)
_START = date(2023, 1, 1)
_END = date(2026, 8, 10)


class FakeIntentEnhancementModel:
    """确定性 fake enhancement：固定输出 / 可注入失败 / 调用计数。"""

    def __init__(
        self,
        output: str = "增强后的问题",
        *,
        fail_with: BaseException | None = None,
        model_id: str = "test:fake-intent-enhancement",
    ) -> None:
        self._output = output
        self._fail_with = fail_with
        self._model_id = model_id
        self.calls: list[IntentEnhancementRequest] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def enhance(self, request: IntentEnhancementRequest) -> str:
        self.calls.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return self._output


# ---------------------------------------------------------------- template


def test_template_is_deterministic() -> None:
    a = build_intent_template(
        company=_COMPANY, modules=["business", "financial"], start_date=_START, end_date=_END
    )
    b = build_intent_template(
        company=_COMPANY, modules=["business", "financial"], start_date=_START, end_date=_END
    )
    assert a == b
    assert "宁德时代新能源科技股份有限公司（300750）" in a
    assert "2023-01-01至2026-08-10" in a


def test_template_module_phrases_preserve_order_and_dedupe() -> None:
    text = build_intent_template(
        company=_COMPANY,
        modules=["financial", "business", "financial", "risk"],
        start_date=_START,
        end_date=_END,
    )
    assert (
        text.index("财务表现与增长驱动")
        < text.index("主营业务与竞争格局")
        < text.index("主要风险因素")
    )
    assert text.count("财务表现与增长驱动") == 1


def test_template_empty_modules_fallback_phrase() -> None:
    text = build_intent_template(company=_COMPANY, modules=[], start_date=_START, end_date=_END)
    assert "主营业务、财务表现与主要风险" in text


def test_template_within_length_bound() -> None:
    text = build_intent_template(
        company=_COMPANY,
        modules=["business", "financial", "events", "macro", "risk"],
        start_date=_START,
        end_date=_END,
    )
    assert len(text) <= MAX_GENERATED_QUESTION_LENGTH


# ---------------------------------------------------------------- generator


@pytest.mark.asyncio
async def test_generate_returns_template_without_enhancement() -> None:
    generator = DefaultResearchIntentGenerator()
    text = await generator.generate(
        company=_COMPANY, modules=["business"], start_date=_START, end_date=_END
    )
    assert text == build_intent_template(
        company=_COMPANY, modules=["business"], start_date=_START, end_date=_END
    )
    assert generator.enhancement_model is None


@pytest.mark.asyncio
async def test_generate_uses_enhancement_output() -> None:
    fake = FakeIntentEnhancementModel(output="宁德时代的盈利能力与增长驱动如何变化？")
    generator = DefaultResearchIntentGenerator(enhancement_model=fake)
    text = await generator.generate(
        company=_COMPANY, modules=["business"], start_date=_START, end_date=_END
    )
    assert text == "宁德时代的盈利能力与增长驱动如何变化？"
    assert len(fake.calls) == 1
    assert fake.calls[0].company == _COMPANY
    assert fake.calls[0].modules == ["business"]


@pytest.mark.asyncio
async def test_generate_falls_back_on_enhancement_failure() -> None:
    fake = FakeIntentEnhancementModel(fail_with=IntentEnhancementUnavailable("boom"))
    generator = DefaultResearchIntentGenerator(enhancement_model=fake)
    text = await generator.generate(
        company=_COMPANY, modules=["business"], start_date=_START, end_date=_END
    )
    assert text == build_intent_template(
        company=_COMPANY, modules=["business"], start_date=_START, end_date=_END
    )


@pytest.mark.asyncio
async def test_generate_never_raises_on_unexpected_enhancement_error() -> None:
    # 非 Unavailable 异常（如编程错误）不吞噬到调用方？——按契约：模型层必须把
    # 一切失败翻译为 IntentEnhancementUnavailable；这里验证 generator 只按契约降级。
    fake = FakeIntentEnhancementModel(fail_with=IntentEnhancementUnavailable("provider down"))
    generator = DefaultResearchIntentGenerator(enhancement_model=fake)
    text = await generator.generate(company=_COMPANY, modules=[], start_date=_START, end_date=_END)
    assert "主营业务、财务表现与主要风险" in text


# ---------------------------------------------------------------- output validation


def test_enhancement_output_accepts_valid_question() -> None:
    output = IntentEnhancementOutput(research_question="  宁德时代近年盈利能力如何变化？  ")
    assert output.research_question == "宁德时代近年盈利能力如何变化？"


def test_enhancement_output_rejects_blank() -> None:
    with pytest.raises(ValidationError):
        IntentEnhancementOutput(research_question="   ")


def test_enhancement_output_rejects_oversized() -> None:
    with pytest.raises(ValidationError):
        IntentEnhancementOutput(research_question="长" * (MAX_GENERATED_QUESTION_LENGTH + 1))


def test_enhancement_output_rejects_internal_id() -> None:
    with pytest.raises(ValidationError):
        IntentEnhancementOutput(
            research_question="请研究 123e4567-e89b-12d3-a456-426614174000 的基本面"
        )
    with pytest.raises(ValidationError):
        IntentEnhancementOutput(
            research_question="a" * 64  # 64-hex fingerprint 形态
        )


def test_enhancement_output_rejects_non_string() -> None:
    with pytest.raises(ValidationError):
        IntentEnhancementOutput(research_question=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------- messages


def test_build_intent_messages_contains_only_semantic_input() -> None:
    messages = build_intent_messages(
        IntentEnhancementRequest(
            company=_COMPANY, modules=["financial"], start_date=_START, end_date=_END
        )
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "300750" in user
    assert "宁德时代" in user
    assert "financial" in user
    assert "2023-01-01" in user
    assert "123e4567" not in user  # 无内部 ID


# ---------------------------------------------------------------- factory switch


def test_factory_disabled_by_default() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
        intent_llm_enhancement=False,
    )
    assert create_intent_enhancement_model(settings) is None


def test_factory_enabled_for_deepseek() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
        intent_llm_enhancement=True,
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
    )
    model = create_intent_enhancement_model(settings)
    assert model is not None
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_factory_accepts_any_nonempty_provider() -> None:
    # v1.2.8：非空 provider 直接视为 wrapper（openai-compatible 语义）。
    settings = Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
        intent_llm_enhancement=True,
        llm_provider="unknown",
    )
    model = create_intent_enhancement_model(settings)
    assert model is not None
    assert model.model_id == "unknown:deepseek-v4-flash"
