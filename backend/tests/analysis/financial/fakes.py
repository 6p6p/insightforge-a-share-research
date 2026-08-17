"""FakeFinancialAnalysisModel：自动化测试用的确定性 financial analysis model（spec 4B.2C.2）。

- 可配置固定决策（FinancialAnalysisDecision 或 dict；dict 用于模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用 / 网络异常）；
- `model_id` 稳定可断言（写入 Claim.analyst_model_id）；
- 记录每次调用的 (FinancialAnalysisContext, CalculationPack, EvidencePack)（断言注入
  边界 / 最小投影 / C/E alias 稳定性）。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.analysis.claims.contracts import EvidencePack
from app.analysis.financial.contracts import (
    CalculationPack,
    FinancialAnalysisContext,
    FinancialAnalysisDecision,
)


class FakeFinancialAnalysisModel:
    """Deterministic fake financial analysis model（结构性满足 FinancialAnalysisModel）。"""

    def __init__(
        self,
        *,
        decision: FinancialAnalysisDecision | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
        decisions_by_round: list[FinancialAnalysisDecision] | None = None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self._decisions_by_round = decisions_by_round
        self.calls: list[tuple[FinancialAnalysisContext, CalculationPack, EvidencePack]] = []
        # Part 1 repair flow：记录每次调用收到的 correction_hint。
        self.correction_hints: list[str | None] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def analyze(
        self,
        context: FinancialAnalysisContext,
        calculation_pack: CalculationPack,
        evidence_pack: EvidencePack,
        correction_hint: str | None = None,
    ) -> FinancialAnalysisDecision:
        self.calls.append((context, calculation_pack, evidence_pack))
        self.correction_hints.append(correction_hint)
        if self._error is not None:
            raise self._error()
        if self._decisions_by_round is not None:
            index = len(self.calls) - 1
            if index < len(self._decisions_by_round):
                return self._decisions_by_round[index]
            return self._decision
        return self._decision
