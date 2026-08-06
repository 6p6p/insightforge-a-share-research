# 阶段 1 计划概览

> 本文件只概述阶段 1 各子阶段的目标，不提前写尚未确定的详细字段。

## 1A：ResearchTask 与任务 API（当前）

- ResearchTask 领域契约（TaskStatus / TaskStage / ResearchModule）。
- `research_tasks` 表、Repository、Service、创建/查询 API 与幂等创建。

## 1B：WorkflowRun、LangGraph State、模拟节点、Postgres Checkpointer

- 引入 WorkflowRun（与 ResearchTask 解耦）。
- LangGraph State、Postgres Checkpointer，以模拟节点跑通最小工作流。

## 1C：后台执行、WorkflowEvent、SSE

- 后台执行任务、工作流事件模型、SSE 推送。

## 1D：Interrupt、恢复、取消、故障注入和验收

- Interrupt / 暂停 / 恢复 / 取消，故障注入与阶段 1 验收。
