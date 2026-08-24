"""Default research intent generator (P0: Company Only Research Flow).

任务可以不带 research_question（`TaskCreateRequest.questions` 为空）——
`ResearchPlanningService.create_plan` 在无问题时调用本生成器，派生默认
研究意图（research_question），再进入既有 `ResearchPlannerInputSnapshot`
冻结路径（P0 设计：**不改 snapshot schema**，生成的 question 与用户输入的
question 一样参与 `compute_planner_input_fingerprint_v2`）。

设计：
- **确定性 template 主路径**：基于公司语义身份 + 用户模块 + 日期窗口生成
  固定中文问题文本。同输入 → 同文本 → 同 fingerprint → replay 命中（0 次
  额外 LLM，与 planner replay 语义一致）；
- **optional LLM enhancement**（`intent_llm_enhancement` settings 开关，
  默认关闭）：`IntentEnhancementModel` 对 template 做增强改写。开启后输出
  进入 snapshot（enhancement 本身是非确定性的，与 planner LLM 一致——同
  输入可能生成不同问题 → 不同 fingerprint → 新 plan 行，属预期行为）；
  enhancement 失败 / 输出非法 → **降级返回 template**（绝不抛到编排层）；
- **hard 边界**：
  - 生成的 question 非空、≤ 500 字符（与 `TaskCreateRequest` 对齐）；
  - 拒绝 internal ID-like 文本（UUID / 64-hex fingerprint），与
    `research_planning.contracts` 的 `_reject_internal_ids` 同一规则；
  - **0 事实编造**：template 只引用公司身份与日期窗口，不产生任何断言；
    enhancement 的 system 规则禁止编造数字 / 事实 / URL / 内部 ID；
  - **0 tools / 0 web / 0 retrieval**：enhancement 只做结构化文本输出。
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.config import Settings
from app.llm.base import get_active_llm
from app.llm.components import COMPONENT_INTENT_ENHANCEMENT
from app.llm.errors import UnsupportedLLMProviderError
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage
from app.research_planning.contracts import CompanyIdentitySnapshot

# intent 生成器身份（与 planner 身份同模式；enhancement 输出进入 snapshot，
# 因此 enhancement prompt/策略版本变化会反映在 fingerprint 中——通过实际输出
# 文本变化体现，无需持久化本常量）。
INTENT_GENERATOR_NAME = "default_intent_generator"
INTENT_GENERATOR_VERSION = 1
INTENT_ENHANCEMENT_STRATEGY_VERSION = 1

# 与 TaskCreateRequest._MAX_QUESTION_LENGTH 对齐。
MAX_GENERATED_QUESTION_LENGTH = 500

# 禁止的 internal ID-like 形态（与 contracts._reject_internal_ids 同一规则）。
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

# 用户 ResearchModule → 默认问题的短语（顺序 = 用户选择顺序，确定性）。
_MODULE_PHRASES: dict[str, str] = {
    "company_profile": "公司概况与主营业务",
    "business": "主营业务与竞争格局",
    "financial": "财务表现与增长驱动",
    "events": "重大事件及其影响",
    "macro": "所处宏观环境",
    "risk": "主要风险因素",
}

# modules 为空（历史任务 / eval runner）时的通用短语。
_FALLBACK_PHRASES = "主营业务、财务表现与主要风险"


def _validate_question_text(text: str) -> str:
    """trim + 非空 + 长度 + internal ID-like 校验（enhancement 输出共用）。"""
    question = text.strip()
    if not question:
        raise ValueError("生成的 research_question 不能为空（trim 后）")
    if len(question) > MAX_GENERATED_QUESTION_LENGTH:
        raise ValueError(f"生成的 research_question 最长 {MAX_GENERATED_QUESTION_LENGTH} 字符")
    if _UUID_PATTERN.search(question):
        raise ValueError("生成的 research_question 不得包含 UUID-like 内部 ID")
    if _HEX64_PATTERN.fullmatch(question):
        raise ValueError("生成的 research_question 不得包含 fingerprint-like 内部 ID")
    return question


class IntentEnhancementOutput(BaseModel):
    """enhancement 的结构化输出（单字段；validator 强制 hard 边界）。"""

    model_config = ConfigDict(frozen=True)

    research_question: str

    @field_validator("research_question", mode="before")
    @classmethod
    def _validate_question(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("research_question 必须是字符串")
        return _validate_question_text(value)


class IntentEnhancementUnavailable(RuntimeError):
    """enhancement 不可用 / 失败 / 输出非法（调用方降级 template，不抛出）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class IntentEnhancementRequest:
    """enhancement 的语义输入（不含内部 UUID / fingerprint / metadata）。"""

    company: CompanyIdentitySnapshot
    modules: list[str]
    start_date: date
    end_date: date


class IntentEnhancementModel(Protocol):
    """LLM abstraction：request → 增强后的 research_question。

    - `model_id`：稳定 identifier（provider:model）；
    - `enhance`：返回已通过 `_validate_question_text` 校验的问题文本；
      任何失败（provider / 网络 / 输出非法）→ `IntentEnhancementUnavailable`；
    - 实现不得启用 tools / web search / retrieval / function side effects。
    """

    @property
    def model_id(self) -> str: ...

    async def enhance(self, request: IntentEnhancementRequest) -> str: ...


def build_intent_template(
    *,
    company: CompanyIdentitySnapshot,
    modules: list[str],
    start_date: date,
    end_date: date,
) -> str:
    """确定性 template：公司身份 + 模块短语 + 日期窗口（0 LLM / 0 事实编造）。"""
    seen: list[str] = []
    for module in modules:
        phrase = _MODULE_PHRASES.get(module)
        if phrase is not None and phrase not in seen:
            seen.append(phrase)
    phrases = "、".join(seen) if seen else _FALLBACK_PHRASES
    return (
        f"请研究{company.official_name}（{company.security_code}）"
        f"在{start_date.isoformat()}至{end_date.isoformat()}期间的基本面情况，"
        f"包括：{phrases}。"
    )


def build_intent_messages(request: IntentEnhancementRequest) -> list[dict]:
    """构造 enhancement 模型消息（system + 只含语义输入 user）。

    不发送内部 UUID / fingerprint / storage metadata。company 只发语义身份。
    """
    company = request.company
    company_lines = "\n".join(
        (
            f"- security_code: {company.security_code}",
            f"- official_name: {company.official_name}",
            f"- exchange: {company.exchange}",
            f"- board: {company.board}",
        )
        + (tuple(f"- alias: {alias}" for alias in company.aliases) if company.aliases else ())
    )
    modules_line = (
        "、".join(sorted(dict.fromkeys(request.modules))) if request.modules else "（未指定）"
    )
    user = (
        f"公司身份：\n{company_lines}\n"
        f"研究模块：{modules_line}\n"
        f"研究日期窗口：{request.start_date.isoformat()} 至 {request.end_date.isoformat()}\n"
        "请生成一个研究问题。"
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 InsightForge 的默认研究意图生成器。为一个 A 股上市公司生成一个"
                "研究问题（只输出问题本身，不要输出其他内容）。\n"
                "硬性规则：\n"
                "1. 只做 A 股基本面研究；禁止交易建议、技术分析、短期预测。\n"
                "2. 不编造数字与事实（不写「公司业绩增长」等断言）。\n"
                "3. 不得包含 URL / 内部 ID / fingerprint / 代码片段。\n"
                f"4. 问题不超过 {MAX_GENERATED_QUESTION_LENGTH} 字符。\n"
                "5. 基于给定的公司身份、研究模块与日期窗口生成问题。"
            ),
        },
        {"role": "user", "content": user},
    ]


class DefaultResearchIntentGenerator:
    """默认研究意图生成器：template 主路径 + optional LLM enhancement。

    构造 `enhancement_model=None`（默认）→ 纯 template（确定性，replay 兼容）。
    """

    def __init__(self, enhancement_model: IntentEnhancementModel | None = None) -> None:
        self._enhancement_model = enhancement_model

    @property
    def enhancement_model(self) -> IntentEnhancementModel | None:
        return self._enhancement_model

    async def generate(
        self,
        *,
        company: CompanyIdentitySnapshot,
        modules: list[str],
        start_date: date,
        end_date: date,
    ) -> str:
        """派生默认研究问题（永不抛；enhancement 失败 → template）。"""
        template = build_intent_template(
            company=company, modules=modules, start_date=start_date, end_date=end_date
        )
        _validate_question_text(template)
        if self._enhancement_model is None:
            return template
        try:
            return await self._enhancement_model.enhance(
                IntentEnhancementRequest(
                    company=company,
                    modules=modules,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        except IntentEnhancementUnavailable:
            return template


class DeepSeekIntentEnhancementModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 IntentEnhancementModel。

    langchain SDK 只在 `enhance()` 真正调用时懒加载（构造 adapter 不依赖
    langchain 已安装）。thinking 显式关闭（与 planner adapter 约定一致）。
    """

    def __init__(self, settings: Settings, usage_observer: LlmUsageObserver | None = None) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"
        self._usage_observer = usage_observer

    @property
    def model_id(self) -> str:
        return self._model_id

    async def enhance(self, request: IntentEnhancementRequest) -> str:
        messages = build_intent_messages(request)
        llm = get_active_llm(self._settings, temperature=0.0)
        try:
            output = await invoke_structured_with_usage(
                llm,
                IntentEnhancementOutput,
                messages,
                component_name=COMPONENT_INTENT_ENHANCEMENT,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except Exception as exc:
            # 含 ValidationError（输出非法，validator 拒绝）、
            # OutputParserException 与 provider/API 异常。
            raise IntentEnhancementUnavailable("intent enhancement 调用失败") from exc
        return output.research_question


def create_intent_enhancement_model(
    settings: Settings,
    usage_observer: LlmUsageObserver | None = None,
) -> IntentEnhancementModel | None:
    """settings.intent_llm_enhancement 开关 → enhancement model（False → None）。

    未知 provider → `UnsupportedLLMProviderError`（与 planner factory 一致）。
    """
    if not settings.intent_llm_enhancement:
        return None
    provider = (settings.llm_provider or "").strip().lower()
    if not provider:
        raise UnsupportedLLMProviderError("llm_provider is not configured")

    return DeepSeekIntentEnhancementModel(settings, usage_observer=usage_observer)
