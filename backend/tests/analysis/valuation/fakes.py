"""FakeValuationAnalysisModel：自动化测试用的确定性 valuation analysis model（4C.2B.2）。

- 可配置固定决策（ValuationAnalysisDecision 或 dict；dict 用于模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用 / 网络异常）；
- `model_id` 稳定可断言（写入 Claim.analyst_model_id）；
- 记录每次调用的 (ValuationAnalysisContext, ValuationComparisonPack)（断言注入
  边界 / 最小投影 / V alias 稳定性）。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.analysis.valuation.contracts import (
    ValuationAnalysisContext,
    ValuationAnalysisDecision,
)
from app.analysis.valuation.packs import ValuationComparisonPack


class FakeValuationAnalysisModel:
    """Deterministic fake valuation analysis model（结构性满足 ValuationAnalysisModel）。"""

    def __init__(
        self,
        *,
        decision: ValuationAnalysisDecision | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self.calls: list[tuple[ValuationAnalysisContext, ValuationComparisonPack]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def analyze(
        self,
        context: ValuationAnalysisContext,
        comparison_pack: ValuationComparisonPack,
    ) -> ValuationAnalysisDecision:
        self.calls.append((context, comparison_pack))
        if self._error is not None:
            raise self._error()
        return self._decision
