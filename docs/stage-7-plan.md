# 阶段 7 计划概览

> 阶段 7 目标：**自动研究规划与执行编排**——在证据驱动基本面研究闭环之上，让系统能根据研究任务自动构造研究计划（ResearchPlan）、确定性地路由到现有数据获取能力、自动解析已有 artifacts 并构造 Stage 4 WorkPlan，从而逐步接近「自动 Source Planning → 自动研究 → 报告 → 审核」的完整闭环。**Stage 7 不引入自动交易 / 技术分析 / 买卖建议**。

## 7A.1：Automatic Research Planning Foundation（completed，FINAL）

- **ResearchPlanner（spec A/C/D/E/F/G/H/I）**：Planner 只输出**语义研究需求**（不输出抓取指令 / 不读 artifact / 不做检索）；`ResearchPlanPayload` schema v1 使用 bounded vocabulary（`source_type` 取真实 `ResearchDocumentNeedType`、financial metric 取 `MetricCode`、valuation metric 仅 `pe_ttm`/`pb_mrq`/`ps_ttm`、analysis modules 仅已实现 Analyst，max counts）；Planner 运行时 `deepseek-v4-flash` + thinking disabled + temperature=0 + structured output，**0 tools / 0 web / 0 retrieval**；Migration `0040` 建 `research_plans` 表（不 UNIQUE(task_id)，replay 由 `planner_input_fingerprint` 唯一性保证）；input / plan fingerprint = canonical JSON SHA-256；`verify_research_plan_integrity` 只重放 stored payload（tamper → `ResearchPlanIntegrityError`，不 repair）。
- **Deterministic SourceRouter（spec J/K）**：0 LLM；`SourceRoutePlan` v1 把每个 need 确定性映射到 `SourceCapability` 现有能力（ISSUER_IR 当前无 provider → `provider_unavailable`）；provider 快照记录路由当时的 registry 状态；持久化 `research_plan_routes`（`UNIQUE(research_plan_id, router_version)`）；`verify_research_plan_route_integrity` 不重新 route / 不查 registry。
- **ResearchPreparation（spec L/M/N/O/P/Q/S）**：按 company / cutoff / period / provenance 从现有 artifacts 解析 needs；`MissingResearchNeed`（`not_found` / `insufficient_evidence` / `missing_period` / `missing_metric` / `missing_macro_observation` / `missing_valuation_comparison` / `provider_unavailable` / `unsupported_need`）；数据不足 → `ready_for_analysis=false`（**不伪造数据**）；只按 plan 声明的 modules 构造输入；尊重 `critical_claim_eligible` / authority-tier / no-lookahead；ready=true → 构造有效 `Stage4WorkflowRequest`（auto work plan）。
- **测试（spec T/U/W）**：planner contracts 单测（14）+ service/router/preparation 集成（23）+ E2E（真实 PG + Fake planner + 真实 Stage4WorkflowRunner 到 SynthesisResult；缺失 valuation comparison → ready=false → 0 Stage4 run）+ Migration 0040 downgrade guard（空表通过 / 有行拒绝）。
- **API（spec R）**：本轮未新增 research-plan API（推迟 7A.2）。

## 7A.2A：Research Need Fulfillment（completed）

- **Fulfillment 服务（spec G/H/I）**：`ResearchFulfillmentService.fulfill_research_needs` —— verify Plan + verify Route + `prepare_research()` → 只消费 `missing_needs` → 按 `need_kind` 分发 executor → 重跑 prepare；`FulfillmentResult`（schema v1，`attempts` / `preparation_before/after` / `ready_for_analysis` / `stage4_request`，仅 application output，不持久化 raw exception / prompt / API response）。
- **Executors（spec J/M/N/O/P）**：`DocumentNeedExecutor`（确定性 RetrievalQuery → Retrieval → `EvidenceExtractionService` → EvidenceCard，source 无 ready index 可确定性补建；SOURCE_NOT_FOUND / INDEX_NOT_READY / EVIDENCE_NOT_EXTRACTED / PROVIDER_UNAVAILABLE）；`FinancialNeedExecutor`（calculation-centric，Observation → `create_calculation`；MISSING_UNDERLYING_OBSERVATION 不凭空造数）；`MacroNeedExecutor`（macro Evidence replay；MACRO_DATA_UNAVAILABLE 不 live fetch）；`ValuationNeedExecutor`（恒 manual_required + EXPLICIT_PEER_SET_REQUIRED，不自动 peer）。
- **幂等（spec Q）**：底层 create_or_get（EvidenceCard / Calculation / macro Evidence）按 fingerprint replay → 第 2 次 fulfill 0 新增写。
- **测试（spec R/S/T/U/V）**：executor + E2E 集成 24（document/event 11 + financial/macro/valuation 9 + 全链 service 4；真实 PG + Fake planner / FakeRetrieval / FakeEvidenceExtractionModel，**0 真实 DeepSeek / 0 Retrieval / 0 Chroma / 0 Web**）；Migration 0041 downgrade guard（空表 / v1 new-field-safe 通过，v2 snapshot 拒绝）；全量回归（非集成 1924 + 集成 941）+ ruff + alembic check 干净。
- **API**：本轮未新增 research-plan API（推迟 7A.2B）。

## 7A.2B：Research Planning API + 规划驱动执行（planned，next；architecture decision frozen 2026-08-12）

### 7A.2B architecture decision（方案 D：Top-level LangGraph Orchestrator + 独立 Stage4/Stage5 WorkflowRuns）

- 新增一等公民 `research_orchestration_runs`（**非 WorkflowRun**）承载顶层 orchestration lifecycle；顶层必须是真实 LangGraph graph `stage7_top_level`，PG Checkpointer、`thread_id = orchestration_id`；节点序列 plan → route → prepare → fulfill → wait/manual → start/resume Stage4 → Synthesis → start/resume Stage5 → rewrite/human/research → research backflow → continuation → complete。
- 顶层 state 只存 ID / 小结构（task_id / research_plan_id / orchestration_id / current_stage / current_child_run_id / synthesis_result_id / research_request_id），不存正文 / Evidence body；backflow 只消费 immutable Request/Fulfillment 的 ID，不改写旧 artifact。
- **不改** `uq_workflow_runs_one_active_per_task` / `uq_workflow_runs_thread_id`：Stage4/5 保持独立 WorkflowRun、`thread_id=run_id`、独立 checkpoint / recovery / action 语义，**不改造为 subgraph**；顶层不在 workflow_runs，与 child 无 invariant 冲突。
- 顶层调用 child 必须 idempotent：节点重放先查该阶段已有 child run（task + graph + orchestration 锚定），已存在则 attach/recover，不重复 create。
- crash 恢复同 `orchestration_id` + 同顶层 thread；user retry 新建 orchestration_id / 顶层 thread（child 沿用现有 retry 语义）；Web 仍以 Stage4/5 WorkflowRun 展示具体执行阶段，可额外投影顶层 phase。
- 对比：A 不满足「LangGraph 唯一顶层编排器」；B 需把 Stage4/5 subgraph 化（recovery/workspace/action/既有集成测试全重锚定，风险最大）；C 需放宽 active-run index；D 仅新增一张增量表，不动现有 invariant。

- `POST /research-plan`（触发 create + route）、`GET /research-plan/{id}`、`POST /research-preparation`（或按任务聚合入口）；前端最小入口（如任务详情页展示 research plan / readiness / missing needs）。
- 规划驱动执行：ready=true 时自动进入 Stage 4 执行；ready=false 时展示 MissingResearchNeeds 供分析师补齐或触发受控的资料获取。

### 7A.2B.1：Orchestration Foundation（completed，本轮 FINAL）

- **Migration 0042 + 模型（spec B/C）**：新增 `research_orchestration_runs`（一等公民，**非 WorkflowRun**）与 `research_orchestration_child_runs`（exact ownership linkage）。`input_fingerprint` CHAR(64) UNIQUE（replay）；partial unique `(task_id) WHERE active`（同 task 至多一个 active orchestration，独立于 `uq_workflow_runs_one_active_per_task`，互不修改）；child `UNIQUE(workflow_run_id)` + `UNIQUE(orchestration_id, stage, attempt_no)`。downgrade guard（spec R）：任一表有行 → 拒绝 rollback。
- **顶层 LangGraph graph（spec I/N）**：真实 StateGraph `stage7_top_level`，PG Checkpointer、`thread_id = orchestration_id`（**顶层线程 != child Stage4 线程**，child 仍 `thread_id = run_id`）。节点序列 `START → ensure_plan → ensure_route → prepare → (ready: ensure_stage4_child | not_ready: fulfill) → fulfill → prepare_again → (ready: ensure_stage4_child | waiting_manual: waiting_manual END) → ensure_stage4_child → run_or_resume_stage4 → collect_synthesis → awaiting_stage5 END`。`awaiting_stage5` 是 7A.2B.1 正常 terminal phase（status 保持 running，等 7A.2B.2 接 Stage5）；`waiting_manual` 是 fulfill 后仍 not ready 的 terminal（status=waiting_human，0 个 WorkflowRun）。State 只存 ID / 小结构（spec H），节点**复用**既有 services（spec J：create_plan / route / prepare / fulfill / Stage4WorkflowRunner / SynthesisService），全部幂等（replay 安全）。
- **fingerprint / replay / user retry（spec F/P）**：`compute_orchestration_input_fingerprint` = schema + task_id + planner input fingerprint + orchestrator 身份（canonical SHA-256，**不含 API key / created_at**）。同输入 → `create_or_get_orchestration` replay 同一 orchestration（replayed=True）；改 task 输入 → 新 fingerprint → 新 orchestration，task 已有 active 时 → `ResearchOrchestrationActiveConflict`（409）。`verify_orchestration_integrity` 只重放 stored plan（不重新调用 planner / LLM）。
- **Stage4 exact child（spec D/K/L）**：child lookup 只允许 `(orchestration_id, stage4, attempt_no)`（**不用 latest task + graph_name 猜归属**）；`ResearchOrchestrationChildService.ensure_stage4_child` 同事务创建 WorkflowRun + child link（run create 与 link insert 之间无 crash 孤儿窗口；并发 IntegrityError → 重查 exact child → winner）。active WorkflowRun invariant 不变。
- **Runner + 失败投影（spec M）**：`ResearchOrchestrationRunner.run_orchestration` checkpoint-aware（已有顶层 checkpoint → 同 thread resume）；graph 异常 → orchestration failed、phase 保持 stage4、`error_code` 稳定投影（`stage4_execution_failed` / `orchestration_execution_failed`，只留异常类型名 ≤200），**不吞 child 错误**（child 自身 run 的 error_code/message 由 Stage4 runner 写在 WorkflowRun 行上）；CancelledError → 不投影失败（恢复路径处理）。
- **Recovery（spec E/O）**：`ResearchOrchestrationRecoveryCoordinator` 在 reconcile 之后用**同 orchestration_id + 同顶层 thread** 恢复（绝不新建 orchestration / 绝不换 thread）；`awaiting_stage5` 不恢复；`phase=stage4` 时 exact child 仍 `running`（live executor / rolling restart）→ 跳过不重复执行。legacy `ResearchExecutionRecoveryCoordinator` 候选排除 orchestration-owned children（SQL `NOT EXISTS`，spec E），不做 cross-stage continuation。
- **Cancel（spec Q）**：minimal + 幂等——先按现有 Stage4/WorkflowRun cancel 语义取消 active child，再 orchestration cancelled；cancelled → 原样返回；completed/failed → `AlreadyFinished`；不直接 SQL 删除 child / orchestration。
- **测试（spec T/U/S）**：单元 43（fingerprint / state / graph topology / runner 守卫 + 失败投影 / service replay-conflict-cancel-verify / child exact 复用与并发 winner / recovery 跳过与恢复判定，0 DB）+ 集成 6 Cases（happy path → awaiting_stage5、not-ready→fulfill→ready、fulfill 后仍 not-ready → waiting_manual、Stage4 child 失败投影、replay + active 409、worker restart 同 thread 恢复无重复产物；真实 PG + 真实 LangGraph + fake prepare/fulfill，**0 真实 DeepSeek**）。全量回归 + ruff + alembic check 干净。
- **本轮不做（边界）**：Stage5 auto-execution、HumanReview、ResearchBackflow auto-loop、Web 一键研究、Stage7B Evaluation；lifespan / production factory 未接线（本阶段无 API 入口创建 orchestration，恢复协调器与 legacy 边界已作为 spec'd 交付物落地，待入口接线时一并接入）。

## 7B：三路评估（3-way Evaluation，planned）

- 对自动编排产出的最终研究报告做**三路独立评估并汇合**，不伪造数据、不越过既有证据链：
  1. **事实核查评估**：复用 Stage5 Report Audit / Check 结果——issue 分布、severity、
     `recommended_route` 与最终修订是否闭合；
  2. **证据链评估**：Source → Evidence → Claim → Report → Audit 全程可追溯——
     report 关键论断逐条定位到 Evidence 与 Source，缺失/断裂回退为显式 gap；
  3. **人工/分析师复核**：分析师对证据充分性、判断稳健性与表述准确性给出独立判定。
- 三路汇合 → 结构化评估结论（每路 pass / needs_revision / blocked），持久化为一等公民
  evaluation artifact。
- **本轮不做**：7B 实现（含其依赖的 Stage7 全链自动执行），明确排期到后续阶段。

## 7C：受控资料获取 + Stage 7 整体闭环（planned）

- 对 `MissingResearchNeed` 提供受控获取入口：官方披露下载、新闻采集、宏观数据刷新、
  财务 / 估值对比补齐——全部经 router 能力白名单 + 人工确认（不自动静默抓取）。
- 自动 Source Planning → 自动研究 → 报告 → 审核 → 三路评估的端到端编排；
  ResearchBackflow 与规划反馈回路（不足的证据需求回流为新的研究计划）。
