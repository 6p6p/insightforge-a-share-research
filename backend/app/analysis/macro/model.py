"""MacroAnalysisModel Protocol (stage 4C.1B).

与 Financial 相同，LLM abstraction 独立成 `model.py`：domain 只依赖协议，
不直接依赖具体 provider；自动测试一律用 FakeMacroAnalysisModel。
"""

from typing import Protocol, runtime_checkable

from app.analysis.macro.contracts import (
    MacroAnalysisContext,
    MacroAnalysisDecision,
)
from app.analysis.macro.packs import CompanyEvidencePack, MacroDriverPack


@runtime_checkable
class MacroAnalysisModel(Protocol):
    """LLM abstraction：把分析上下文 + MacroDriver Pack + Company Pack 抽成结构化决策。

    - `model_id`：稳定 identifier（provider:model，不伪造 revision）；由
      MacroAnalysisService 持久化到 MacroClaimDraft.analyst_model_id；
    - `analyze`：接收 MacroAnalysisContext 与 MacroDriver/Company Pack，
      返回 MacroAnalysisDecision；provider 失败翻译为
      MacroAnalysisModelUnavailable；
    - 实现不得启用 tools / web search / function side effects。
    """

    @property
    def model_id(self) -> str: ...

    async def analyze(
        self,
        context: MacroAnalysisContext,
        driver_pack: MacroDriverPack,
        company_pack: CompanyEvidencePack,
    ) -> MacroAnalysisDecision: ...
