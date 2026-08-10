"""SynthesisAnalysisModel Protocol (stage 4D.1B).

与 Macro/Financial 相同，LLM abstraction 独立成 `model.py`：domain 只依赖协议，
不直接依赖具体 provider；自动测试一律用 FakeSynthesisAnalysisModel。
"""

from typing import Protocol, runtime_checkable

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisContext,
    SynthesisAnalysisOutput,
)
from app.analysis.synthesis.packs import SynthesisClaimPack


@runtime_checkable
class SynthesisAnalysisModel(Protocol):
    """LLM abstraction：把综合上下文 + Claim Pack 抽成结构化综合输出。

    - `model_id`：稳定 identifier（provider:model，不伪造 revision）；由
      SynthesisAnalysisService 持久化到 claim_synthesis_results.analyst_model_id；
    - `analyze`：接收 SynthesisAnalysisContext 与 Claim Pack，返回
      SynthesisAnalysisOutput；provider 失败翻译为
      SynthesisAnalysisModelUnavailable；
    - 实现不得启用 tools / web search / function side effects。
    """

    @property
    def model_id(self) -> str: ...

    async def analyze(
        self,
        context: SynthesisAnalysisContext,
        claim_pack: SynthesisClaimPack,
    ) -> SynthesisAnalysisOutput: ...
