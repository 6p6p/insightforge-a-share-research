"""Stage 4 analysis workflow dependencies (DI container, spec L).

`Stage4AnalysisDependencies` 集中持有现有 Analysis / Synthesis Services；
graph nodes 通过它 dispatch，**不**在 node 内重新初始化 model factory。
自动测试一律用 Fake models + 现有 Services（不访问真实 LLM）。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.financial.service import FinancialAnalysisService
from app.analysis.macro.service import MacroAnalysisService
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.analysis.valuation.service import ValuationAnalysisService
from app.core.config import Settings
from app.synthesis.service import SynthesisService


@dataclass(frozen=True)
class Stage4AnalysisDependencies:
    """一次 Stage 4 分析工作流所需的全部 Application Services。"""

    sessionmaker: async_sessionmaker
    claim_analysis_service: ClaimAnalysisService
    financial_analysis_service: FinancialAnalysisService
    macro_analysis_service: MacroAnalysisService
    valuation_analysis_service: ValuationAnalysisService
    synthesis_service: SynthesisService
    synthesis_analysis_service: SynthesisAnalysisService


def create_stage4_dependencies(
    settings: Settings,
    sessionmaker: async_sessionmaker,
) -> Stage4AnalysisDependencies:
    """生产 factory：Settings → 现有 model factories → Services → deps。

    只在 graph 之外构建一次（runner 持有）；node 内不重新初始化 model。
    """
    from app.analysis.claims.factory import create_claim_analysis_model
    from app.analysis.financial.factory import create_financial_analysis_model
    from app.analysis.macro.factory import create_macro_analysis_model
    from app.analysis.synthesis.factory import create_synthesis_analysis_model
    from app.analysis.valuation.factory import create_valuation_analysis_model

    return Stage4AnalysisDependencies(
        sessionmaker=sessionmaker,
        claim_analysis_service=ClaimAnalysisService(
            sessionmaker, create_claim_analysis_model(settings)
        ),
        financial_analysis_service=FinancialAnalysisService(
            sessionmaker, create_financial_analysis_model(settings)
        ),
        macro_analysis_service=MacroAnalysisService(
            sessionmaker, create_macro_analysis_model(settings)
        ),
        valuation_analysis_service=ValuationAnalysisService(
            sessionmaker, create_valuation_analysis_model(settings)
        ),
        synthesis_service=SynthesisService(sessionmaker),
        synthesis_analysis_service=SynthesisAnalysisService(
            sessionmaker, create_synthesis_analysis_model(settings)
        ),
    )
