"""FakeSynthesisAnalysisModel：自动化测试用的确定性 synthesis model（stage 4D.1B）。

- 可配置固定输出（SynthesisAnalysisOutput 或 dict；dict 用于模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用 / 网络异常）；
- `model_id` 稳定可断言（写入 claim_synthesis_results.analyst_model_id）；
- 记录每次调用的 (SynthesisAnalysisContext, SynthesisClaimPack)（断言注入边界 /
  C alias 稳定性 / LLM 永不看 UUID）。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisContext,
    SynthesisAnalysisOutput,
)
from app.analysis.synthesis.packs import SynthesisClaimPack


class FakeSynthesisAnalysisModel:
    """Deterministic fake synthesis analysis model（结构性满足 SynthesisAnalysisModel）。"""

    def __init__(
        self,
        *,
        output: SynthesisAnalysisOutput | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
    ) -> None:
        self._output = output
        self._model_id = model_id
        self._error = error
        self.calls: list[tuple[SynthesisAnalysisContext, SynthesisClaimPack]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def analyze(
        self,
        context: SynthesisAnalysisContext,
        claim_pack: SynthesisClaimPack,
    ) -> SynthesisAnalysisOutput:
        self.calls.append((context, claim_pack))
        if self._error is not None:
            raise self._error()
        return self._output
