# 阶段 1 验收：研究任务与模拟工作流

> 日期：2026-08-06
> 状态：已通过

## 已验证能力

1. **ResearchTask 创建与查询**：POST /tasks（幂等创建）、GET /tasks、GET /tasks/{id}、分页。
2. **并发 Idempotency-Key**：真实并发下一次 replayed=false、一次 replayed=true，同 task_id，单条记录。
3. **WorkflowRun 与 thread_id**：thread_id = run_id，唯一；同一时刻一个任务仅一个 active run。
4. **PostgreSQL Checkpoint**：LangGraph vendor 表由 checkpointer.setup() 管理；状态跨进程可读。
5. **后台执行**：asyncio 单进程 ExecutionManager；HTTP 202 启动。
6. **WorkflowEvent 与 SSE**：事件先持久化后推送；SSE 含 keep-alive 与 Last-Event-ID 回放。
7. **Last-Event-ID**：中间断点增量回放；event_id 单调但可能有间隙。
8. **Interrupt 与跨重启恢复**：计划审批暂停 → waiting_human → approve 后同一 run/thread 恢复完成（Manager 关闭重开验证）。
9. **cancel 与 retry**：取消 waiting_human → cancelled；重试生成新 run/thread。
10. **orphan reconciliation**：启动时 pending/running 标记 worker_restarted failed；waiting_human 保留；幂等。
11. **测试数量**：单元 138 passed；集成 19 passed。
12. **日志脱敏**：无数据库 URI、密码、Checkpoint 内容、请求正文；无未消费 Task 异常。
13. **明确未实现**：LLM/Agent、资料采集、RAG、Source/Evidence/Claim/Report 表、前端、分布式任务队列。

## 边界

- 模拟工作流不改变 ResearchTask（保持 pending/created/0）。
- 后台执行为单实例方案，多实例调度未实现。

## 1D.1 收口验证

- waiting_human 阻止第二个 active run（HTTP 409 active_workflow_run_exists；数据库唯一约束生效）。
- 并发 approve：一个 202、一个 409（HTTP 层与真实 PostgreSQL 均验证）。
- HumanAction / run_resumed / run_completed 均只有一份。
- `ck_workflow_runs_pending_action_consistency` CHECK 通过验证（等待/非等待组合）。
