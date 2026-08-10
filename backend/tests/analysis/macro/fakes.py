"""FakeMacroAnalysisModel：自动化测试用的确定性 macro analysis model（stage 4C.1B）。

- 可配置固定决策（MacroAnalysisDecision 或 dict；dict 用于模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用 / 网络异常）；
- `model_id` 稳定可断言（写入 Claim.analyst_model_id）；
- 记录每次调用的 (MacroAnalysisContext, MacroDriverPack, CompanyEvidencePack)
  （断言注入边界 / 最小投影 / M-E alias 稳定性）。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.analysis.macro.contracts import (
    MacroAnalysisContext,
    MacroAnalysisDecision,
)
from app.analysis.macro.packs import CompanyEvidencePack, MacroDriverPack


class FakeMacroAnalysisModel:
    """Deterministic fake macro analysis model（结构性满足 MacroAnalysisModel）。"""

    def __init__(
        self,
        *,
        decision: MacroAnalysisDecision | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self.calls: list[tuple[MacroAnalysisContext, MacroDriverPack, CompanyEvidencePack]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def analyze(
        self,
        context: MacroAnalysisContext,
        driver_pack: MacroDriverPack,
        company_pack: CompanyEvidencePack,
    ) -> MacroAnalysisDecision:
        self.calls.append((context, driver_pack, company_pack))
        if self._error is not None:
            raise self._error()
        return self._decision
