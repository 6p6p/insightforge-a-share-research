# ADR-0006：后台执行与 SSE

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 背景

阶段 1C 需要将模拟 WorkflowRun 以 HTTP 方式创建并后台执行，向客户端推送执行事件，并支持断线重连回放。

## 决策

1. **使用 asyncio 单进程后台管理器（WorkflowExecutionManager）**。
   - 用 `asyncio.create_task` 在当前 FastAPI 进程内调度 `execute_simulation`；这是单进程开发方案，不是分布式任务队列。
2. **当前不引入 Celery/Redis/RQ/Kafka**。
   - 本阶段工作流为确定性模拟、时长极短，单进程足够；分布式调度留到真正需要时再引入。
3. **WorkflowEvent 必须先持久化再通过 SSE 发送**。
   - 事件先写入 `workflow_events`（PostgreSQL），SSE 生成器从数据库查询后发送；数据库是事件的唯一真相来源。
4. **SSE 使用 `event_id` 作为 `Last-Event-ID`**。
   - `event_id` 是数据库 identity 主键、严格递增，天然可作为游标；客户端断线后用该值重连即可增量回放。
   - 注意：`event_id` 单调但**可能存在间隙**（identity 序列不保证连续）；客户端只能使用 `event_id > Last-Event-ID` 判断新事件，不得假设下一个 ID 是当前值 + 1。
5. **断线重连从 PostgreSQL 回放，不依赖内存 Queue**。
   - 服务重启后历史事件仍可回放；不把事件放在进程内存或浏览器内存。
6. **事件不保存完整 State 或 Checkpoint**。
   - `node_completed` 事件只带节点名、stage、progress 与受限 payload（completed_nodes / simulation_complete），不写 research_plan、完整 State 或 Checkpoint 内容。
7. **Graph 执行期间不持有数据库 Session**。
   - Runner 用短事务领取 run + 写 run_started，随后关闭事务执行 Graph，每个节点完成后用新的短事务写事件；绝不让一个事务跨越整个 Graph。
8. **单进程方案的局限**：
   - 进程崩溃时内存中的 asyncio Task 丢失。
   - `running` 状态的 run 不会自动恢复（stale run 处理留到 1D）。
   - 多实例调度尚未实现；数据库 partial unique index + `claim_pending` 原子领取是跨协程的最终防线。
9. **阶段 1D 将处理** stale run、Interrupt、恢复和取消（此处不提前实现）。
10. **ResearchTask 当前不随模拟 WorkflowRun 标记完成**。
    - 模拟 run 只更新自身与事件；ResearchTask 保持 pending/created/0，真实研究完成语义在后续阶段定义。

## 后果

- SSE 连接随时可断（客户端断开只结束生成器，不取消 WorkflowRun）。
- 所有事件可跨进程、跨重启回放，为后续真实工作流提供一致的进度/审计基础。
