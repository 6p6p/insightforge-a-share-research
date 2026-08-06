# 阶段 1 计划概览

> 阶段 1 已全部完成并通过验收（见 docs/stage-1-acceptance.md）。

## 1A：ResearchTask 与任务 API（已完成）

- ResearchTask 领域契约（TaskStatus / TaskStage / ResearchModule）。
- `research_tasks` 表、Repository、Service、创建/查询 API 与幂等创建。

## 1B：WorkflowRun、LangGraph State、模拟节点、Postgres Checkpointer（已完成）

- WorkflowRun 领域契约、`workflow_runs` 表与迁移。
- LangGraph State、确定性模拟节点与 PostgreSQL Checkpointer。
- WorkflowRunner 短事务边界；Checkpointer setup 显式执行，vendor 表归第三方包管理。

## 1C：后台执行、WorkflowEvent、SSE（已完成）

- asyncio 单进程后台执行管理器（WorkflowExecutionManager）。
- `workflow_events` 持久化、WorkflowRun 查询与 SSE（Last-Event-ID 断线重连回放）。
- Checkpoint readiness 纳入健康检查。

## 1D：Interrupt、恢复、取消、故障注入和验收（已完成）

- 研究计划人工确认（LangGraph interrupt）、waiting_human 状态。
- approve 恢复（同 run/thread）、cancel、retry（新 run/thread）、启动 orphan reconciliation。
- 阶段 1 端到端验收完成（见 docs/stage-1-acceptance.md）。
