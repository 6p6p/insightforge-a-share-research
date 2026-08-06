# ADR-0004：ResearchTask 数据契约

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 背景

阶段 1A 需要定义用户研究任务的稳定数据契约，并落地为可持久化、可查询、可幂等创建的 `research_tasks` 表。此契约将被后续 LangGraph State、Agent 与前端长期复用。

## 决策

1. **ResearchTask 是业务任务，不是 LangGraph Checkpoint**。
   - 它记录"用户要研究什么、任务当前处于什么状态"；Checkpoint 保存的是工作流运行内部状态（属于 WorkflowRun），两者不混用。
2. **TaskStatus 与 TaskStage 分离**。
   - `TaskStatus` 描述任务生命周期（pending/running/completed/failed…），`TaskStage` 描述研究管线阶段（planning/collecting/writing…）；不把阶段名混入状态枚举，避免枚举语义混乱。
3. **当前不创建 WorkflowRun / thread_id**。
   - 后台执行与 Checkpointer 属于阶段 1B/1C；1A 只保证任务可被可靠创建和查询。
4. **modules/questions 使用 JSONB，核心查询字段独立列**。
   - `company_query`、日期、`status`、`progress` 等参与查询/过滤/索引的字段用独立列；仅 `modules`、`questions` 这类小型结构化输入用 JSONB，避免整个请求塞进单个 JSONB。
5. **数据库不用 PostgreSQL native ENUM**。
   - 使用字符串 + CHECK constraint；新增枚举值时无需重建类型，降低迁移成本。
6. **任务创建支持可选 Idempotency-Key**。
   - 客户端重试时避免重复创建；相同 key + 相同请求指纹重放已有任务，key 冲突时返回 409。数据库唯一约束是并发场景的最终防线。
7. **阶段 1A 创建任务后不立即启动工作流**。
   - 创建与执行解耦；任务先落库为 pending，执行在后续阶段接入。
8. **后续允许通过 migration 演进**：
   - `current_stage` 随管线扩展；`progress` 语义细化；`modules`/`questions` 内容结构微调；新增任务类型字段（如公司标准化后的 `company_id`）属于阶段 2 的表设计，另行决策。

## 后果

- API、数据库、未来 LangGraph State 复用同一组 StrEnum，避免三层各自维护字符串常量。
- 幂等创建依赖请求指纹（规范化 JSON 的 SHA-256），指纹算法演进时旧指纹仍可读（仅影响新任务）。
