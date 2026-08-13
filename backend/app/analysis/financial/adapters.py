"""Production LLM adapter (stage 4B.2C.2): ChatDeepSeek → FinancialAnalysisModel。

- 复用 3C.2.1 的 DeepSeek runtime（ChatDeepSeek + with_structured_output）；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-v4-flash`）；provider 无
  immutable revision 时**不伪造 revision**；
- **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`）：
  DeepSeek V4 Flash 默认开启 thinking，但结构化 Financial 分析需要稳定、低成本
  的受约束输出且不产生 `reasoning_content`；`temperature=0` 不等于关闭 thinking，
  必须显式传参（`thinking` 非标准 OpenAI 参数，按 langchain-deepseek==1.1.0
  公开接口经 `extra_body` 传递）；
- 只启用 structured-output 机制，**不绑定 tools / 不开 web search**（非 agentic）；
- 异常映射：provider / API / 网络异常 → `FinancialAnalysisModelUnavailable`；
  输出无法解析为 `FinancialAnalysisDecision` → `FinancialAnalysisMalformedOutput`；
- **不泄露** raw provider response / key / 完整 prompt。

自动测试仍用 `FakeFinancialAnalysisModel`；真实调用只用于受控 smoke。
"""

from app.analysis.claims.contracts import EvidencePack
from app.analysis.financial.contracts import (
    CalculationPack,
    FinancialAnalysisContext,
    FinancialAnalysisDecision,
)
from app.analysis.financial.errors import (
    FinancialAnalysisMalformedOutput,
    FinancialAnalysisModelUnavailable,
)
from app.analysis.financial.prompt import build_analysis_messages
from app.core.config import Settings
from app.llm.components import COMPONENT_FINANCIAL_ANALYSIS
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage


class DeepSeekFinancialAnalysisModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 FinancialAnalysisModel。

    langchain SDK 只在 `analyze()` 真正调用时懒加载（import 本模块 / 构造
    adapter 不依赖 langchain 已安装）。
    """

    def __init__(self, settings: Settings, usage_observer: LlmUsageObserver | None = None) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"
        self._usage_observer = usage_observer

    @property
    def model_id(self) -> str:
        """稳定 identifier：provider:model（无 immutable revision，不伪造 @rev）。"""
        return self._model_id

    async def analyze(
        self,
        context: FinancialAnalysisContext,
        calculation_pack: CalculationPack,
        evidence_pack: EvidencePack,
    ) -> FinancialAnalysisDecision:
        try:
            from langchain_core.exceptions import OutputParserException  # noqa: F401
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise FinancialAnalysisModelUnavailable("langchain-deepseek 未安装") from exc

        messages = build_analysis_messages(
            context=context,
            calculation_pack=calculation_pack,
            evidence_pack=evidence_pack,
        )
        api_key = self._settings.deepseek_api_key
        llm = ChatDeepSeek(
            model=self._settings.llm_model,
            temperature=0.0,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
            api_key=api_key.get_secret_value() if api_key is not None else None,
            # 显式关闭 thinking：DeepSeek V4 Flash 默认 thinking，但结构化 Financial
            # 分析需要稳定受约束输出（无 reasoning_content）；temperature=0 不等于
            # 关闭 thinking。thinking 非标准 OpenAI 参数，经 extra_body 传递。
            extra_body={"thinking": {"type": "disabled"}},
            # 只启用 structured-output；不绑定 tools / web search / function side effects。
        )
        try:
            return await invoke_structured_with_usage(
                llm,
                FinancialAnalysisDecision,
                messages,
                component_name=COMPONENT_FINANCIAL_ANALYSIS,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except OutputParserException as exc:
            raise FinancialAnalysisMalformedOutput() from exc
        except Exception as exc:
            raise FinancialAnalysisModelUnavailable("LLM structured-output 调用失败") from exc
