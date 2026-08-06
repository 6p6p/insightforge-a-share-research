# 阶段 1 计划概览

> 本文件只概述阶段 1 各子阶段的目标，不提前写尚未确定的详细字段。

## 1A：ResearchTask 与任务 API（已完成）

- ResearchTask 领域契约（TaskStatus / TaskStage / ResearchModule）。
- `research_tasks` 表、Repository、Service、创建/查询 API 与幂等创建。

## 1B：WorkflowRun、LangGraph State、模拟节点、Postgres Checkpointer（当前进行中）

- WorkflowRun 领域契约、`workflow_runs` 表与迁移。
- LangGraph State、确定性模拟节点与 PostgreSQL Checkpointer。
- WorkflowRunner 短事务边界；Checkpointer setup 显式执行，vendor 表归第三方包管理。

## 1C：后台执行、WorkflowEvent、SSE（尚未开始）

- 后台执行任务、工作流事件模型、SSE 推送；详细接口在 1C 阶段再冻结。

## 1D：Interrupt、恢复、取消、故障注入和验收（尚未开始）

- Interrupt / 暂停 / 恢复 / 取消，故障注入与阶段 1 验收。
