"""Research backflow contract (stage 5E.2B).

Stage5 不负责真正执行 Stage2/3/4 research。本模块只产生**可验证** research handoff
（research_required run → review action ± human decision → source Report → 确定性
身份 / cutoff → structured request payload），并消费 upstream 返回的**新**
SynthesisResult（continuation identity / no-progress 政策 → fulfillment → 续跑
Stage5WorkflowRequest）。

7A.2B.3 补充研究计划（`derive_research_backflow_plan_payload`）也只做**确定性**
派生：need_specs 的 retrieval_queries 是冻结模板（section context + research
question / Claim statement + need 描述），**不是 LLM 生成**。
**0 LLM / 0 检索 / 0 Chroma**（查询派生阶段）。
"""
