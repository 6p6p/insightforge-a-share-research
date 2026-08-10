"""ValuationAnalysisModel Protocol（stage 4C.2B.2）。

放在独立模块避免 `packs → contracts` 与 `contracts → packs` 的循环导入：
- `packs.py` 依赖 `contracts.py`（ValuationAnalysisDecision / Reason）；
- `model.py` 依赖两者（context/decision + comparison pack），不反向。

实现契约：
- `model_id`：稳定 identifier（provider:model，不伪造 revision）；由
  ValuationAnalysisService 持久化到 Claim.analyst_model_id；
- `analyze`：接收 ValuationAnalysisContext 与 ValuationComparisonPack，返回
  ValuationAnalysisDecision；provider 失败翻译为 ValuationAnalysisModelUnavailable；
  输出无法解析为 ValuationAnalysisDecision → ValuationAnalysisMalformedOutput；
- 实现不得启用 tools / web search / function side effects。

自动测试一律使用 `tests/analysis/valuation/fakes.FakeValuationAnalysisModel`，
不访问任何真实 LLM / 网络 / provider。
"""

from typing import Protocol, runtime_checkable

from app.analysis.valuation.contracts import (
    ValuationAnalysisContext,
    ValuationAnalysisDecision,
)
from app.analysis.valuation.packs import ValuationComparisonPack


@runtime_checkable
class ValuationAnalysisModel(Protocol):
    """LLM abstraction：把分析上下文 + Comparison Pack 抽成结构化决策。"""

    @property
    def model_id(self) -> str: ...

    async def analyze(
        self,
        context: ValuationAnalysisContext,
        comparison_pack: ValuationComparisonPack,
    ) -> ValuationAnalysisDecision: ...
