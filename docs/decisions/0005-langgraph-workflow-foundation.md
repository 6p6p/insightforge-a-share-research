# ADR-0005：LangGraph 工作流基础

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 背景

阶段 1B 需要建立 ResearchTask 与工作流执行之间的清晰关系，用不依赖 LLM 的确定性节点跑通 LangGraph，并用 PostgreSQL Checkpointer 持久化状态。

## 决策

1. **ResearchTask 与 WorkflowRun 区别**。
   - ResearchTask 描述"用户要研究什么"（用户契约）；WorkflowRun 描述"一次工作流执行"（运行记录）。两者通过 `task_id` 关联。
2. **一个 ResearchTask 可以有多个历史 WorkflowRun**。
   - 任务可多次执行（历史运行、重跑）；workflow_runs 是 1:N 到 research_tasks。
3. **同一时刻一个任务只允许一个 active run**。
   - 用 PostgreSQL partial unique index 约束 `status IN ('pending','running')` 的 run 按 task_id 唯一，防止并发重复执行。
4. **thread_id 使用 run_id，而不是 task_id**。
   - LangGraph Checkpoint 的 thread_id 对应一次执行的运行轨迹；用 run_id 天然唯一且可回溯到 workflow_runs，不因任务重跑而复用/覆盖旧轨迹。
5. **Checkpoint 表不由 Alembic 管理**。
   - langgraph-checkpoint-postgres 的 vendor 表由该第三方包自己的 migration（checkpointer.setup()）创建；Alembic 只管 InsightForge 业务表，避免两套迁移系统争用。
6. **Checkpointer setup 显式执行**。
   - 通过 `python -m app.cli.setup_checkpointer` 或集成测试显式调用；幂等可重复。
7. **不在应用启动时连接或迁移 Checkpointer**。
   - FastAPI lifespan 只创建 Manager 对象（延迟连接），不强制连接 PostgreSQL、不执行 setup；避免依赖不可用时应用无法启动。
8. **Graph 节点不直接写数据库**。
   - 节点是纯状态转换（确定性模拟）；持久化由 Runner 在节点外协调。
9. **Graph 执行期间不保持数据库事务**。
   - Runner 用短事务读取任务上下文后即关闭 Session，再运行 Graph；执行结束后新开短事务标记结果。绝不让一个数据库事务跨越整个 LangGraph 执行。
10. **阶段 1B 使用确定性模拟节点**。
    - 不接 LLM、不采集资料，节点输出由输入决定，可测试、可复现，只验证编排与持久化。
11. **严格 MsgPack 安全策略**。
    - 通过 `JsonPlusSerializer(allowed_msgpack_modules=None)` 显式启用严格模式（仅内置安全类型），不依赖环境变量；State 只含 JSON/MsgPack 安全类型，不使用任意反序列化。
12. **阶段 1C 接入方式**。
    - 1C 将引入后台执行与事件/SSE，基于现有 WorkflowRun 与 Checkpointer 扩展；此处不提前设计详细接口。

## 后果

- 一个任务的执行轨迹由 run_id 唯一定位，Checkpoint 状态可跨进程恢复读取。
- 后续真实研究节点只要保持"纯状态转换 + Runner 协调持久化"的边界，即可替换模拟节点。
