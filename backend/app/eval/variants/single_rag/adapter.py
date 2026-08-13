"""Single RAG answer model production adapter (stage 7B.1.4C.1).

`DeepSeekSingleRagAnswerModel` 把底层 chat model（LangChain Runnable）包装为
`SingleRagAnswerModel`，经 `invoke_structured_with_usage` 上报 usage。

component_name = `eval_single_rag_answer`（**不**在 10-component production
registry `INSTRUMENTED_LLM_COMPONENTS` 中——它是 eval-only component，非
production pipeline 组件）。

底层 chat model 由调用方构造并注入（future full runner 持有 settings 并构造
`ChatDeepSeek`）；本适配器**不**在此实例化 `ChatDeepSeek`，避免
`tests/llm/test_component_inventory.py` 把 single_rag 计入 production 10-adapter
审计集合。本轮不跑真实 DeepSeek，测试用 `FakeSingleRagAnswerModel`。
"""

from __future__ import annotations

from app.eval.variants.single_rag.contracts import (
    SingleRagContextEntry,
    SingleRagModelOutput,
    build_single_rag_messages,
)
from app.llm.components import COMPONENT_EVAL_SINGLE_RAG_ANSWER
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage


class DeepSeekSingleRagAnswerModel:
    """把注入的 chat model 包装为 SingleRagAnswerModel（production path）。"""

    def __init__(self, model, *, provider: str, model_id: str) -> None:
        self._model = model
        self._provider = provider
        self._model_id = model_id

    async def answer(
        self,
        research_question: str,
        context_entries: tuple[SingleRagContextEntry, ...],
        *,
        usage_observer: LlmUsageObserver | None,
    ) -> SingleRagModelOutput:
        messages = build_single_rag_messages(
            research_question=research_question,
            context_entries=context_entries,
        )
        return await invoke_structured_with_usage(
            self._model,
            SingleRagModelOutput,
            messages,
            component_name=COMPONENT_EVAL_SINGLE_RAG_ANSWER,
            provider=self._provider,
            model_id=self._model_id,
            usage_observer=usage_observer,
        )
