"""Production LLM adapter (stage 5E.2A): ChatDeepSeek → RevisionWriterModel.

- 复用 5B 的 DeepSeek runtime 约定（ChatDeepSeek + with_structured_output）；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-v4-flash`）；
- **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`）：
  不产生 `reasoning_content`（spec F：不持久化 reasoning_content）；
  `temperature=0` 不等于关闭 thinking，必须显式传参；
- 只启用 structured-output 机制，**不绑定 tools / 不开 web search**（非
  agentic，spec F/J：Revision Writer 不 retrieval / 不联网 / 不调函数）；
- 异常映射：provider / API / 网络异常 → `RevisionWriterModelUnavailable`；输出
  无法解析为 `WriterDecision` → `RevisionWriterMalformedOutput`；
- **不泄露** raw provider response / key / 完整 prompt / reasoning_content。

自动测试仍用 Fake 模型；真实调用只用于受控 smoke。
"""

from app.core.config import Settings
from app.draft_section.contracts import WriterDecision
from app.llm.components import COMPONENT_REVISION_WRITER
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage
from app.revision.errors import (
    RevisionWriterMalformedOutput,
    RevisionWriterModelUnavailable,
)
from app.revision.packs import RevisionInputPack
from app.revision.prompt import build_revision_writer_messages


class DeepSeekRevisionWriterModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 RevisionWriterModel。

    langchain SDK 只在 `rewrite()` 真正调用时懒加载（import 本模块 / 构造
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

    async def rewrite(self, pack: RevisionInputPack) -> WriterDecision:
        try:
            from langchain_core.exceptions import OutputParserException  # noqa: F401
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise RevisionWriterModelUnavailable("langchain-deepseek 未安装") from exc

        messages = build_revision_writer_messages(pack)
        api_key = self._settings.deepseek_api_key
        llm = ChatDeepSeek(
            model=self._settings.llm_model,
            temperature=0.0,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
            api_key=api_key.get_secret_value() if api_key is not None else None,
            # 显式关闭 thinking：DeepSeek V4 Flash 默认 thinking，但 Evidence-bound
            # Rewriter 需要稳定受约束输出（无 reasoning_content）；temperature=0 不
            # 等于关闭 thinking。thinking 非标准 OpenAI 参数，经 extra_body 传递。
            extra_body={"thinking": {"type": "disabled"}},
            # 只启用 structured-output；不绑定 tools / web search / function side effects。
        )
        try:
            return await invoke_structured_with_usage(
                llm,
                WriterDecision,
                messages,
                component_name=COMPONENT_REVISION_WRITER,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except OutputParserException as exc:
            raise RevisionWriterMalformedOutput() from exc
        except Exception as exc:
            raise RevisionWriterModelUnavailable("LLM structured-output 调用失败") from exc
