# 阶段 1 计划概览

> 本文件只概述阶段 1 各子阶段的目标，不提前写尚未确定的详细字段。

## 1A：ResearchTask 与任务 API（已完成）

- ResearchTask 领域契约（TaskStatus / TaskStage / ResearchModule）。
- `research_tasks` 表、Repository、Service、创建/查询 API 与幂等创建。

## 1B：WorkflowRun、LangGraph State、模拟节点、Postgres Checkpointer（已完成）

- WorkflowRun 领域契约、`workflow_runs` 表与迁移。
- LangGraph State、确定性模拟节点与 PostgreSQL Checkpointer。
- WorkflowRunner 短事务边界；Checkpointer setup 显式执行，vendor 表归第三方包管理。

## 1C：后台执行、WorkflowEvent、SSE（当前进行中）

- asyncio 单进程后台执行管理器（WorkflowExecutionManager）。
- `workflow_events` 持久化、WorkflowRun 查询与 SSE（Last-Event-ID 断线重连回放）。
- Checkpoint readiness 纳入健康检查。
- 详细接口已冻结；单进程方案局限记录于 ADR-0006。

## 1D：Interrupt、恢复、取消、故障注入和验收（尚未开始）

- stale run 处理、Interrupt / 暂停 / 恢复 / 取消，故障注入与阶段 1 验收。
