"""Production LLM adapter (stage 3C.2.1): ChatDeepSeek → EvidenceExtractionModel.

把 3C.2 的可选 adapter 收口为**真正可运行**的 DeepSeek 生产 adapter：

- 使用相同 `prompt.py`（system / user 分离，source 只在 user data delimiter 内）；
- `async invoke` + `with_structured_output(EvidenceExtractionDecision)`，
  `temperature=0`、`timeout` / `max_retries` 来自 Settings；
- 只启用 structured-output 机制，**不绑定 tools / 不开 web search**（非 agentic）；
- provider / API / schema 异常映射：
  - provider API / 认证 / 网络异常 → `EvidenceExtractorUnavailable`；
  - 输出无法解析为 `EvidenceExtractionDecision`（schema 校验失败）→
    `EvidenceExtractionMalformedOutput`；
  - **不泄露** raw provider response / key / 完整 prompt；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-chat`）；provider 无
  immutable revision 时**不伪造 revision**（spec 11）。

自动测试仍用 `FakeEvidenceExtractionModel`；真实调用只用于受控 smoke。
"""

from app.core.config import Settings
from app.evidence.extractor.contracts import EvidenceExtractionDecision
from app.evidence.extractor.errors import (
    EvidenceExtractionMalformedOutput,
    EvidenceExtractorUnavailable,
)
from app.evidence.extractor.prompt import ExtractionContext, build_extraction_messages
from app.rag.retrieval.contracts import RetrievalHit


class DeepSeekEvidenceExtractionModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 EvidenceExtractionModel。

    langchain SDK 只在 `extract()` 真正调用时懒加载（import 本模块 / 构造
    adapter 不依赖 langchain 已安装）。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"

    @property
    def model_id(self) -> str:
        """稳定 identifier：provider:model（无 immutable revision，不伪造 @rev）。"""
        return self._model_id

    async def extract(
        self,
        research_question: str,
        retrieval_hit: RetrievalHit,
    ) -> EvidenceExtractionDecision:
        try:
            from langchain_core.exceptions import OutputParserException  # noqa: F401
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise EvidenceExtractorUnavailable("langchain-deepseek 未安装") from exc

        context = self._context_from_hit(retrieval_hit)
        messages = build_extraction_messages(
            research_question=research_question,
            chunk_text=retrieval_hit.text,
            context=context,
        )
        api_key = self._settings.deepseek_api_key
        llm = ChatDeepSeek(
            model=self._settings.llm_model,
            temperature=0.0,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
            api_key=api_key.get_secret_value() if api_key is not None else None,
            # 只启用 structured-output；不绑定 tools / web search / function side effects。
        )
        structured = llm.with_structured_output(EvidenceExtractionDecision)
        try:
            return await structured.ainvoke(messages)
        except OutputParserException as exc:
            raise EvidenceExtractionMalformedOutput() from exc
        except Exception as exc:
            raise EvidenceExtractorUnavailable("LLM structured-output 调用失败") from exc

    @staticmethod
    def _context_from_hit(hit: RetrievalHit) -> ExtractionContext:
        return ExtractionContext(
            source_title=hit.source_title,
            provider_key=hit.provider_key,
            document_type=hit.document_type,
            published_at=hit.published_at,
            reporting_period_end=hit.reporting_period_end,
        )
