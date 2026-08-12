"""Top-level research orchestration (stage 7A.2B.1).

`research_orchestration_runs` 承载一次 **top-level orchestration** 的生命周期
（**不是 WorkflowRun**）：Plan → Route → Prepare → Fulfill → Stage4 child →
Synthesis → awaiting_stage5。顶层是真实 LangGraph graph（PG Checkpointer、
`thread_id = orchestration_id`）；Stage4/5 保持独立 WorkflowRun、`thread_id =
run_id`、独立 checkpoint / recovery / action 语义。

- `contracts`：status / current_phase / child stage 枚举 + orchestrator 身份 +
  input fingerprint（spec F）；
- `errors`：`ResearchOrchestrationError` 错误树；
- `repository`：`research_orchestration_runs` / `research_orchestration_child_runs`
  访问（create_or_get replay + exact child ownership lookup，spec D）；
- `service`：`create_or_get_orchestration` / `get_orchestration` /
  `verify_orchestration_integrity` / `cancel_orchestration`。
"""
