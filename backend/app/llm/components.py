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
# P0：Default Research Intent Generator 的 optional LLM enhancement（11 个
# production adapter；`tests/llm/test_component_inventory.py` 冻结校验）。
COMPONENT_INTENT_ENHANCEMENT = "intent_enhancement"
# P2：Model Assisted Discovery Node——LLM 只做候选发现（12 个 production
# adapter；`tests/llm/test_component_inventory.py` 冻结校验）。
COMPONENT_SEARCH_DISCOVERY = "search_discovery"

# eval-only component：single_rag variant 的一次 RAG 回答生成。**不**加入
# `INSTRUMENTED_LLM_COMPONENTS`（那是 10-component production pipeline registry，
# 被 `tests/llm/test_component_inventory.py` 冻结校验）。eval 侧经
# `invoke_structured_with_usage` 上报 usage 时用它作为 component_name，使
# usage 可归因，但**不计入** production adapter 审计集合。
COMPONENT_EVAL_SINGLE_RAG_ANSWER = "eval_single_rag_answer"

# eval-only component：semantic judge 的一次结构化评分。同样**不**加入
# `INSTRUMENTED_LLM_COMPONENTS`（judge 属 scoring layer，非 production pipeline
# 组件）。
COMPONENT_EVAL_JUDGE = "eval_judge"

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
    COMPONENT_INTENT_ENHANCEMENT,
    COMPONENT_SEARCH_DISCOVERY,
)
