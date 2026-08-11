# 阶段 6 计划概览

> 阶段 6 目标：**Web 工作台**——把已 FINAL 的证据链（Source → Evidence → Claim → Report → Audit → ReviewAction → Revision / ResearchBackflow）暴露为真实可操作的前端界面，供分析师创建研究任务、观察 LangGraph 执行进度并完成人工确认。**Stage 6 不做 LLM 功能开发、不做 Stage 7 evaluation**；本轮（6A）只做**工作台基础 + Task/Progress 垂直切片**。
>
> 总进度：**6A = completed（FINAL）**；**6B.1 = completed（FINAL，Research Artifact Workspace）**；**6B.2 = completed（FINAL，Citation Navigation + Provenance Viewer）**；**6C = next（工作台增强，见下）**。

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

## 6B.1：Research Artifact Workspace（completed，FINAL）

- **任务级只读 artifact workspace**：`GET /tasks/{id}/sources|evidence|analysis|report|reviews` 从 LangGraph PG Checkpointer 精确恢复产物 ID 集合（sources / evidence 分页信封 `{items,total,limit,offset}`），`GET /tasks/{id}/workspace` 的产物计数改为任务级（与各 tab 共用同一 helper，保证一致）。
- **canonical synthesis lineage anchor**：canonical synthesis = 最新 Stage5 checkpoint 的 `synthesis_result_id`；只有 `synthesis_result_id` 与之匹配的 Stage4 run 才成为 `matched_stage4_run` 暴露 `work_items`。研究回流（ResearchBackflow）的新 Synthesis 无匹配 Stage4 → `work_items=[]` 且 `work_items_available=false`，**绝不混用旧 Stage4 工作项**；无 Stage5 时 canonical analysis anchor = 最新 Stage4。
- **完整性语义**：artifact 缺失 → 200 空 / null；artifact ID 存在但 `verify_*_integrity` 重建失败 → `TaskArtifactIntegrityError`（HTTP 409，统一 `{error:{code,message,request_id}}` 信封，不泄漏 SQL / stack，不 repair）。只读路径 **0 LLM / 0 网络**，缺 `DEEPSEEK_API_KEY` 不失败。
- **dual-origin sources**：document（Evidence→SourceRecord→RawArtifact）与 macro（Evidence→MacroObservation→Snapshot→Series→SourceProvider→RawArtifact，source_id=NULL），投影含 source_identity / origin_type / source_type / provider_key / title / label / fetched_at / authority_tier / locator_summary。
- **evidence relations**：`used_by_claim_ids`（必填）+ `claim_relations:[{claim_id,relation}]`；analysis 投影含 themes / conflicts / evidence_gaps（alias refs 解析为真实 claim_ids）；report 投影含真实 body（sections[].paragraphs[]）；reviews 分层投影（Deterministic Check + Agent Audit + ReviewAction + Human Review + Research Backflow，缺失层=null）。
- 前端：工作台 antd Tabs（概览 / 来源 / 证据 / 分析 / 报告 / 审核），惰性挂载，5 个 artifact tab 组件渲染新字段；完整性错误显示「产物完整性校验失败」。后端回归 1891 非集成 + 834 集成；前端 typecheck + 35 tests + build 全绿；alembic 保持 0038（0 新迁移）。

## 6B.2：Citation Navigation + Evidence/Claim Provenance（completed，FINAL）

- **共享只读 provenance 服务**：`EvidenceProvenanceService`（`app/evidence/provenance_service.py`）从 audit provenance 提取为最小公共只读路径，Document（EvidenceCard→DocumentChunk→ChunkSet→ParsedSource→SourceRecord→RawArtifact→SourceProvider）与 Macro（EvidenceCard→MacroObservation→Snapshot→Series→SourceProvider + MacroSnapshotArtifact links→RawArtifact）共用同一条 verified provenance 链；`document_closure` / `macro_closure` 做真实闭包校验（macro FK 非空不够，删 artifact links → False）。
- **`TaskCitationService`（task-scoped 只读）**：给定 task_id + evidence_card_id / claim_id，先从 TaskArtifactService canonical lineage 得到 allowed evidence/claim ID 集，跨 task Evidence / 跨 canonical Claim → `CitationNotFound` 404；不允许凭任意 UUID 直接读全库。
- **Citation API**：`GET /tasks/{id}/citations/evidence/{card}`（evidence 头 + claim_relations（保留 supports / contradicts / context）+ discriminated-union provenance）与 `GET /tasks/{id}/citations/claims/{claim}`（仅 canonical synthesis input claim）。任一 hop 缺失或 tamper → `TaskArtifactIntegrityError` 409，不 repair。
- **原文打开策略**：后端 content 端点仅服务 PDF（非 PDF 415）；前端只在 `media_type === 'application/pdf'` 时显示「打开原文 PDF」（新标签页打开后端流式端点）。
- **前端**：`CitationDrawer`（evidence/claim citation 只读视图，Document/Macro provenance 分派，relation 导航 evidence⇄claim）；ReportTab 观点/证据 Tag 可点击、EvidenceTab「查看引用」、ReviewsTab「定位报告」（`?tab=report&section=&paragraph=` URL 定位 → 高亮并滚动）。
- 后端 8 个 R-scenario 集成测试 + E2E（真实 PostgreSQL + Fake models + FastAPI，0 real DeepSeek）；前端 48 tests + typecheck + build 全绿；alembic 保持 0038（0 新迁移）。

## 6C（next）：工作台增强

- Task 列表分页 / 搜索 / 状态筛选。
- workspace 产物卡片按 Stage 4/5 类型细化（evidence cards / calculations / comparisons / outline / draft sections / report）。
- 多 run 历史视图 + 每 run 事件明细检索。
- 执行前 Stage 4 work plan 的**前端校验**（ID 数量区间、必填字段）与后端 422 错误逐字段映射。
- 真实 DeepSeek 接入前的 UI 演练开关（明确标注非生产 LLM 路径）。
- 已知限制：WorkPlanEditor 的 TextArea 为受控输入、每次击键规范化（splitIds/join），逐字符连续输入 ID 可能被截断——6B 建议改为「整段提交 / blur 规范化」。
