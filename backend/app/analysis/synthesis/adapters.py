"""Production LLM adapter (stage 4D.1B): ChatDeepSeek → SynthesisAnalysisModel.

- 复用 4C.1B 的 DeepSeek runtime（ChatDeepSeek + with_structured_output）；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-v4-flash`）；provider 无
  immutable revision 时**不伪造 revision**；
- **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`）：
  DeepSeek V4 Flash 默认开启 thinking，但结构化综合分析需要稳定、低成本的受
  约束输出且不产生 `reasoning_content`；`temperature=0` 不等于关闭 thinking，
  必须显式传参（`thinking` 非标准 OpenAI 参数，按 langchain-deepseek==1.1.0
  公开接口经 `extra_body` 传递）；
- 只启用 structured-output 机制，**不绑定 tools / 不开 web search**（非 agentic）；
- 异常映射：provider / API / 网络异常 → `SynthesisAnalysisModelUnavailable`；
  输出无法解析为 `SynthesisAnalysisOutput` → `SynthesisAnalysisMalformedOutput`；
- **不泄露** raw provider response / key / 完整 prompt / reasoning_content。

自动测试仍用 `FakeSynthesisAnalysisModel`；真实调用只用于受控 smoke。
"""

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisContext,
    SynthesisAnalysisOutput,
)
from app.analysis.synthesis.errors import (
    SynthesisAnalysisMalformedOutput,
    SynthesisAnalysisModelUnavailable,
)
from app.analysis.synthesis.packs import SynthesisClaimPack
from app.analysis.synthesis.prompt import build_analysis_messages
from app.core.config import Settings
from app.llm.components import COMPONENT_SYNTHESIS_ANALYSIS
from app.llm.base import get_active_llm
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage


class DeepSeekSynthesisAnalysisModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 SynthesisAnalysisModel。

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
        context: SynthesisAnalysisContext,
        claim_pack: SynthesisClaimPack,
    ) -> SynthesisAnalysisOutput:
        try:
            from langchain_core.exceptions import OutputParserException  # noqa: F401
        except ImportError as exc:
            raise SynthesisAnalysisModelUnavailable("langchain-deepseek 未安装") from exc

        messages = build_analysis_messages(context=context, claim_pack=claim_pack)
        llm = get_active_llm(self._settings, temperature=0.0)
        try:
            return await invoke_structured_with_usage(
                llm,
                SynthesisAnalysisOutput,
                messages,
                component_name=COMPONENT_SYNTHESIS_ANALYSIS,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except OutputParserException as exc:
            raise SynthesisAnalysisMalformedOutput() from exc
        except Exception as exc:
            raise SynthesisAnalysisModelUnavailable("LLM structured-output 调用失败") from exc
