"""Research backflow contract (stage 5E.2B).

Stage5 不负责真正执行 Stage2/3/4 research。本模块只产生**可验证** research handoff
（research_required run → review action ± human decision → source Report → 确定性
身份 / cutoff → structured request payload），并消费 upstream 返回的**新**
SynthesisResult（continuation identity / no-progress 政策 → fulfillment → 续跑
Stage5WorkflowRequest）。**0 LLM / 0 检索 / 0 Chroma / 0 query 生成**。
"""
