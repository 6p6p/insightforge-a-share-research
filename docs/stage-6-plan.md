# 阶段 6 计划概览

> 阶段 6 目标：**Web 工作台**——把已 FINAL 的证据链（Source → Evidence → Claim → Report → Audit → ReviewAction → Revision / ResearchBackflow）暴露为真实可操作的前端界面，供分析师创建研究任务、观察 LangGraph 执行进度并完成人工确认。**Stage 6 不做 LLM 功能开发、不做 Stage 7 evaluation**；本轮（6A）只做**工作台基础 + Task/Progress 垂直切片**。
>
> 总进度：**6A = completed（Web Workbench Task Foundation，后端 API + 前端脚手架 + Task/Progress 垂直切片，Gate 验收完成）**；**6B = next（工作台增强，见下）**。

## 6A：Web Workbench Task Foundation（completed，FINAL）

- **范围**：React 工作台的前后端最小闭环。**0 real LLM**：后端 integration 垂直切片使用 Fake LLM（Stage 4 Fake Analysts + Stage 5 Fake Draft/Audit/Revision 模型），全程不调用真实 DeepSeek。
- **技术栈冻结**：React 18 + TypeScript + Vite + Ant Design 5 + TanStack Query（+ react-router-dom 6）。**不引入** Redux / MobX / Next.js / Tailwind / 复杂状态框架。
- **前端结构**（`frontend/src/`）：`api/`（client、tasks、workflow、events）、`components/`（StatusTag、EventTimeline、ArtifactSummaryCards、PageTitle）、`features/task-create/`（TaskCreateForm、WorkPlanEditor）、`features/workflow-progress/`（WorkflowProgressPanel、HumanActionCard、StartResearchPanel）、`pages/`（TaskList / TaskCreate / TaskWorkspace）、`routes/`、`hooks/useTaskEvents`、`types/`、`utils/`（status、sse、stage5）。所有 UI 文案中文。
- **路由**：`/tasks`（列表）、`/tasks/new`（新建）、`/tasks/:taskId`（工作台：task 摘要 + 当前 run + stage + 进度 + 事件时间线 + 待处理人工确认 + 错误）。
- **人工确认 UI**：按钮按真实 `pending_action` / `graph_name` 动态出现（Stage 5 human_review → approve / rewrite / research / cancel）；pending 期间禁用；409 状态变化 → 告警 + 自动刷新。
- **SSE**：原生 EventSource + `Last-Event-ID` 断线重连续传（后端 `parse_last_event_id(None)=0` → 首次连接从 0 重放全量历史）；事件按 `event_id` 去重合并（`utils/sse.ts` 纯函数）。
- **不假装自动 Source Planning 已完成**：没有完整 Stage 2→5 自动计划入口，工作台通过 `POST /tasks/{id}/execute` 接受**显式 Stage 4 work plan**（`analysis_work_items`，用户填写真实证据 / 计算 / 对比 ID）。UI 用 WorkPlanEditor 明确暴露该契约并提示「Stage 6A 不包含自动 Source Planning」。
- **无 migration**：本轮纯 Web/API，alembic head 保持 0038。
- **CORS**：`cors_allow_origins="http://localhost:5173"`（默认开发前端来源）+ `allow_credentials=true`；**禁止** `allow_origins=["*"]` + credentials=true。Docker 前端部署延后，`npm run dev` 独立启动。

### API–Frontend 边界

| 方向 | 端点 | 说明 |
| --- | --- | --- |
| 复用现有 | `POST /api/v1/tasks` | 创建任务（TaskCreateRequest 契约未变，未破坏兼容性） |
| 复用现有 | `GET /api/v1/tasks`、`GET /api/v1/tasks/{id}` | 任务查询 |
| 新增 | `GET /api/v1/tasks/{id}/workspace` | TaskWorkspace projection（task + 解析公司 + 当前 run + 产物计数） |
| 新增 | `POST /api/v1/tasks/{id}/execute` | 启动显式 Stage 4 work plan（返回 Stage 4 run，202） |
| 新增 | `GET /api/v1/tasks/{id}/events` | task 级 SSE 事件流（`Last-Event-ID` 续传） |
| 新增 | `GET /api/v1/workflow-runs/{id}` | 单 run 详情 |
| 新增 | `POST /api/v1/workflow-runs/{id}/actions` | 人工动作（approve / rewrite / research / cancel，active-run 409 保护） |
| **保留** | `GET /api/v1/workflow-runs/{id}/events` | 既有 run 级 SSE，**未删除** |

- 错误信封统一 `{error:{code,message,request_id}}`；FastAPI 422 保持 `{detail:[...]}`；前端 `ApiError` 解析两者并在 UI 显示中文错误。
- active-run 不变式不变：同一 task 同时只允许一个进行中的 run（`ActiveWorkflowRunExists` → 409）。

### 验证（Gate 实测结果）

- **后端**：`pytest -m integration backend/tests/integration/test_stage6_vertical_slice.py` → **3 passed**（happy path execute→waiting_human→approve→completed + SSE 含 run_waiting_human/run_completed；execute 与 active run 冲突 409；run 前 workspace 可用）。全程 Fake LLM，0 真实 DeepSeek。
- **前端**：`npx vitest run` → **16 passed**（status mapping、SSE parser/reducer、HumanActionCard 动态按钮 + 409、TaskCreateForm 提交链路）。
- **前端**：`tsc --noEmit` 干净；`vite build` 成功（antd 体积 chunk 警告为既有，非回归）。
- **回归**：Stage 2–5 集成测试不因本轮 API 扩展而破坏（复用既有 `POST /tasks` 契约）。

## 6B（next）：工作台增强

- Task 列表分页 / 搜索 / 状态筛选。
- workspace 产物卡片按 Stage 4/5 类型细化（evidence cards / calculations / comparisons / outline / draft sections / report）。
- 多 run 历史视图 + 每 run 事件明细检索。
- 执行前 Stage 4 work plan 的**前端校验**（ID 数量区间、必填字段）与后端 422 错误逐字段映射。
- 真实 DeepSeek 接入前的 UI 演练开关（明确标注非生产 LLM 路径）。
- 已知限制：WorkPlanEditor 的 TextArea 为受控输入、每次击键规范化（splitIds/join），逐字符连续输入 ID 可能被截断——6B 建议改为「整段提交 / blur 规范化」。
