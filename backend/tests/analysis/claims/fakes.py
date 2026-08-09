"""FakeClaimAnalysisModel：自动化测试用的确定性 claim analysis model（spec 4B.1）。

- 可配置固定决策（ClaimAnalysisDecision 或 dict；dict 用于模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用 / 网络异常）；
- `model_id` 稳定可断言（写入 Claim.analyst_model_id）；
- 记录每次调用的 (ClaimAnalysisContext, EvidencePack)（断言注入边界 / 最小投影）。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.analysis.claims.contracts import (
    ClaimAnalysisContext,
    ClaimAnalysisDecision,
    EvidencePack,
)


class FakeClaimAnalysisModel:
    """Deterministic fake claim analysis model（结构性满足 ClaimAnalysisModel）。"""

    def __init__(
        self,
        *,
        decision: ClaimAnalysisDecision | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self.calls: list[tuple[ClaimAnalysisContext, EvidencePack]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def analyze(
        self,
        context: ClaimAnalysisContext,
        evidence_pack: EvidencePack,
    ) -> ClaimAnalysisDecision:
        self.calls.append((context, evidence_pack))
        if self._error is not None:
            raise self._error()
        return self._decision
