# ADR-0007：Interrupt、恢复与取消

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 背景

阶段 1D 需要支持研究计划人工确认（LangGraph interrupt）、跨 backend 重启恢复、取消、失败重试与重启遗留协调。

## 决策

1. **人工审批使用 LangGraph interrupt**。
   - `request_plan_approval` 节点在 `require_plan_approval=true` 时调用 `interrupt` 暂停图执行；审批后以 `Command(resume=...)` 恢复。
2. **resume 复用同一 run_id/thread_id**。
   - 中断状态保存在该 thread 的 Checkpoint 中，恢复用同一轨迹继续，不创建新 run/thread；backend 重启后用同一 thread_id 也可恢复。
3. **retry 创建新 run/thread**。
   - failed/cancelled 后的重试是全新执行，生成新 run_id/thread_id 与新 Checkpoint 历史；原 run 保持不变。
4. **取消不删除 Checkpoint**。
   - 历史事件与 Checkpoint 状态保留，便于审计；取消只把 run 标记为 cancelled。
5. **waiting_human 不属于 terminal**。
   - 它可被恢复或取消；terminal 仅 `completed / failed / cancelled`。SSE 在 waiting_human 时保持连接。
6. **HumanAction 单独持久化**。
   - `human_actions` 记录每次人工动作；`UNIQUE(run_id, interrupt_key)` 是重复提交的最终防线。
7. **启动协调将 pending/running 标记 failed**。
   - 单实例架构下，backend 重启后遗留的 pending/running run 不可能仍在旧进程执行，原子标记为 `worker_restarted`；`waiting_human` 可通过 Checkpoint 在新进程恢复，不受影响。
8. **该策略只适用于单 backend 实例**。
   - 多实例不能直接"启动时全部标记失败"，需要分布式 lease/选举（阶段 2 或更后）。
9. **当前不引入任务队列**。
   - 单进程 asyncio 调度足够；分布式队列留到真正需要时。
10. **阶段 2 接入真实资料管线前必须保持的边界**。
    - 事件不保存完整 State/Checkpoint；Graph 执行期间不持有数据库事务；不伪造 Source/Evidence/Claim/Report。
11. **waiting_human 属于 active run**。
    - 与 pending/running 一样阻止同一任务创建第二个 active run。
12. **部分唯一索引覆盖 pending/running/waiting_human**。
    - `uq_workflow_runs_one_active_per_task` 的 WHERE 条件包含 waiting_human。
13. **approve 必须先原子接受再调度 Graph**。
    - `prepare_resume` 在同一短事务完成 claim_waiting_human + HumanAction + run_resumed；成功后才 `asyncio.create_task(continue_resume)`。
14. **不能先返回 202 再在后台判断能否恢复**。
    - 接受失败直接向 HTTP 返回 404/409，不创建后台 Task；并发 approve 只允许一个 202。
15. **pending_action 与 status 由数据库 CHECK 保证一致**。
    - `ck_workflow_runs_pending_action_consistency`：waiting_human 必须带 pending_action，非 waiting_human 必须为空。

## 后果

- 人工确认、取消、重试与重启恢复在确定性模拟图上端到端可用，为阶段 2 真实管线提供一致的执行/审计基础。
