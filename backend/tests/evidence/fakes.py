"""FakeEvidenceExtractionModel：自动化测试用的确定性 extraction model（spec 3/12）。

- 可配置固定决策（EvidenceExtractionDecision 或 dict；dict 用于模拟
  malformed output）；
- 可配置抛错（模拟 provider 不可用 / 异常）；
- `model_id` 稳定可断言（写入 EvidenceCard.extractor_model_id）；
- 记录每次调用的 (research_question, RetrievalHit)。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.evidence.extractor.contracts import EvidenceExtractionDecision
from app.rag.retrieval.contracts import RetrievalHit


class FakeEvidenceExtractionModel:
    """Deterministic fake extraction model（结构性满足 EvidenceExtractionModel）。"""

    def __init__(
        self,
        *,
        decision: EvidenceExtractionDecision | dict | None = None,
        model_id: str = "fake/structured-llm@1",
        error: type[Exception] | None = None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self.calls: list[tuple[str, RetrievalHit]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def extract(
        self, research_question: str, retrieval_hit: RetrievalHit
    ) -> EvidenceExtractionDecision:
        self.calls.append((research_question, retrieval_hit))
        if self._error is not None:
            raise self._error()
        return self._decision
