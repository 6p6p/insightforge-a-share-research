"""LLM instrumentation component identity (stage 7B.1.2B).

冻结 production LLM component 集合（= Part G 审计出的 10 个 DeepSeek adapter）。
每个 adapter 在调用 `invoke_structured_with_usage` 时用对应常量作为
`component_name`，让 usage 记录可归因到 pipeline component。

新增 production DeepSeek adapter 时**必须**：
1. 在此登记 component 常量；
2. 把常量加入 `INSTRUMENTED_LLM_COMPONENTS`；
3. 让 adapter 经 `invoke_structured_with_usage` 上报 usage。

`tests/llm/test_component_inventory.py` 会校验 registry == 审计冻结集合，并在
出现「用 `ChatDeepSeek` 但未走 instrumentation」的文件时告警。
"""

COMPONENT_EVIDENCE_EXTRACTION = "evidence_extraction"
COMPONENT_CLAIM_ANALYSIS = "claim_analysis"
COMPONENT_FINANCIAL_ANALYSIS = "financial_analysis"
COMPONENT_MACRO_ANALYSIS = "macro_analysis"
COMPONENT_SYNTHESIS_ANALYSIS = "synthesis_analysis"
COMPONENT_VALUATION_ANALYSIS = "valuation_analysis"
COMPONENT_DRAFT_SECTION_WRITER = "draft_section_writer"
COMPONENT_AUDIT = "audit"
COMPONENT_REVISION_WRITER = "revision_writer"
COMPONENT_RESEARCH_PLANNER = "research_planner"

# 顺序稳定：与 audit 的顺序一致（evidence → claim → ... → planner）。
INSTRUMENTED_LLM_COMPONENTS: tuple[str, ...] = (
    COMPONENT_EVIDENCE_EXTRACTION,
    COMPONENT_CLAIM_ANALYSIS,
    COMPONENT_FINANCIAL_ANALYSIS,
    COMPONENT_MACRO_ANALYSIS,
    COMPONENT_SYNTHESIS_ANALYSIS,
    COMPONENT_VALUATION_ANALYSIS,
    COMPONENT_DRAFT_SECTION_WRITER,
    COMPONENT_AUDIT,
    COMPONENT_REVISION_WRITER,
    COMPONENT_RESEARCH_PLANNER,
)
