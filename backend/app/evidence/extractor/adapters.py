"""LangChain structured-output adapter (stage 3C.2, optional import).

自动测试一律使用 FakeEvidenceExtractionModel（spec 3 / 12）；本 adapter 只用于
"受控真实 LLM smoke"（spec 13）。**langchain 不是 InsightForge 核心依赖**：
本模块顶层不导入 langchain，只有真正实例化并调用时才懒加载；未安装 →
EvidenceExtractorUnavailable。

约束：
- temperature = 0（或 provider 支持的最小值）；
- 只启用 structured-output 机制，不启用 tools / web search / function side
  effects（非 agentic，单次结构化调用）；
- 不把 provider 名写死在 Evidence domain（provider 由构造参数传入，只用于
  组装 extractor_model_id）；
- extractor_model_id = provider:model@revision；provider 不提供 immutable
  revision 时保存明确 model id，**不伪造 revision**（spec 11）。
"""

from app.evidence.extractor.contracts import EvidenceExtractionDecision
from app.evidence.extractor.errors import EvidenceExtractorUnavailable
from app.evidence.extractor.prompt import ExtractionContext, build_extraction_messages
from app.rag.retrieval.contracts import RetrievalHit


class LangChainStructuredOutputAdapter:
    """把 LangChain 的 ChatOpenAI（OpenAI 兼容端点）包装为 EvidenceExtractionModel。

    构造参数显式传入 provider / model_name / revision / 凭证，代码里不写死
    任何 provider 名。真实 smoke 由操作者安装 langchain-openai 并注入凭证后
    才可运行。
    """

    def __init__(
        self,
        *,
        provider: str,
        model_name: str,
        model_revision: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not provider or not model_name:
            raise EvidenceExtractorUnavailable("adapter 需要 provider 与 model_name")
        self._provider = provider
        self._model_name = model_name
        self._model_revision = model_revision
        self._base_url = base_url
        self._api_key = api_key
        self._temperature = temperature
        self._timeout_seconds = timeout_seconds
        self._model_id = self._compose_model_id()

    @property
    def model_id(self) -> str:
        """稳定 identifier：provider:model@revision（无 revision 不伪造）。"""
        return self._model_id

    def _compose_model_id(self) -> str:
        base = f"{self._provider}:{self._model_name}"
        if self._model_revision:
            return f"{base}@{self._model_revision}"
        return base

    async def extract(
        self, research_question: str, retrieval_hit: RetrievalHit
    ) -> EvidenceExtractionDecision:
        try:
            from langchain_openai import ChatOpenAI  # 懒加载：langchain 非核心依赖
        except ImportError as exc:
            raise EvidenceExtractorUnavailable(
                "langchain-openai 未安装：仅真实 smoke 需要（自动测试用 Fake）"
            ) from exc

        context = self._context_from_hit(retrieval_hit)
        messages = build_extraction_messages(
            research_question=research_question,
            chunk_text=retrieval_hit.text,
            context=context,
        )
        llm = ChatOpenAI(
            model=self._model_name,
            temperature=self._temperature,
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            max_retries=0,
        )
        structured = llm.with_structured_output(EvidenceExtractionDecision)
        try:
            return await structured.ainvoke(messages)
        except Exception as exc:
            raise EvidenceExtractorUnavailable("LLM structured-output 调用失败") from exc

    def _context_from_hit(self, hit: RetrievalHit) -> ExtractionContext:
        return ExtractionContext(
            source_title=hit.source_title,
            provider_key=hit.provider_key,
            document_type=hit.document_type,
            published_at=hit.published_at,
            reporting_period_end=hit.reporting_period_end,
        )
