"""Production LLM adapter (stage 4C.2B.2): ChatDeepSeek → ValuationAnalysisModel。

- 复用 3C.2.1 的 DeepSeek runtime（ChatDeepSeek + with_structured_output）与
  4B.2C.2 的 financial adapter 同款配置；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-v4-flash`）；provider 无
  immutable revision 时**不伪造 revision**；
- **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`）：
  DeepSeek V4 Flash 默认开启 thinking，但结构化 Valuation 分析需要稳定、低成本
  的受约束输出且不产生 `reasoning_content`；`temperature=0` 不等于关闭 thinking，
  必须显式传参（`thinking` 非标准 OpenAI 参数，经 `extra_body` 传递）；
- 只启用 structured-output 机制，**不绑定 tools / 不开 web search**（非 agentic）；
- 异常映射：provider / API / 网络异常 → `ValuationAnalysisModelUnavailable`；
  输出无法解析为 `ValuationAnalysisDecision` → `ValuationAnalysisMalformedOutput`；
- **不泄露** raw provider response / key / 完整 prompt。

自动测试仍用 FakeValuationAnalysisModel；真实调用只用于受控 smoke。
"""

from app.analysis.valuation.contracts import (
    ValuationAnalysisContext,
    ValuationAnalysisDecision,
)
from app.analysis.valuation.errors import (
    ValuationAnalysisMalformedOutput,
    ValuationAnalysisModelUnavailable,
)
from app.analysis.valuation.packs import ValuationComparisonPack
from app.analysis.valuation.prompt import build_analysis_messages
from app.core.config import Settings
from app.llm.base import get_active_llm
from app.llm.components import COMPONENT_VALUATION_ANALYSIS
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage


class DeepSeekValuationAnalysisModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 ValuationAnalysisModel。

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
        context: ValuationAnalysisContext,
        comparison_pack: ValuationComparisonPack,
    ) -> ValuationAnalysisDecision:
        try:
            from langchain_core.exceptions import OutputParserException  # noqa: F401
        except ImportError as exc:
            raise ValuationAnalysisModelUnavailable("langchain-deepseek 未安装") from exc

        messages = build_analysis_messages(
            context=context,
            comparison_pack=comparison_pack,
        )
        llm = get_active_llm(self._settings, temperature=0.0)
        try:
            return await invoke_structured_with_usage(
                llm,
                ValuationAnalysisDecision,
                messages,
                component_name=COMPONENT_VALUATION_ANALYSIS,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except OutputParserException as exc:
            raise ValuationAnalysisMalformedOutput() from exc
        except Exception as exc:
            raise ValuationAnalysisModelUnavailable("LLM structured-output 调用失败") from exc
