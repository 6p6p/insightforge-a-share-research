# 阶段 7 计划概览

> 阶段 7 目标：**自动研究规划与执行编排**——在证据驱动基本面研究闭环之上，让系统能根据研究任务自动构造研究计划（ResearchPlan）、确定性地路由到现有数据获取能力、自动解析已有 artifacts 并构造 Stage 4 WorkPlan，从而逐步接近「自动 Source Planning → 自动研究 → 报告 → 审核」的完整闭环。**Stage 7 不引入自动交易 / 技术分析 / 买卖建议**。

## 7A.1：Automatic Research Planning Foundation（completed，FINAL）

- **ResearchPlanner（spec A/C/D/E/F/G/H/I）**：Planner 只输出**语义研究需求**（不输出抓取指令 / 不读 artifact / 不做检索）；`ResearchPlanPayload` schema v1 使用 bounded vocabulary（`source_type` 取真实 `ResearchDocumentNeedType`、financial metric 取 `MetricCode`、valuation metric 仅 `pe_ttm`/`pb_mrq`/`ps_ttm`、analysis modules 仅已实现 Analyst，max counts）；Planner 运行时 `deepseek-v4-flash` + thinking disabled + temperature=0 + structured output，**0 tools / 0 web / 0 retrieval**；Migration `0040` 建 `research_plans` 表（不 UNIQUE(task_id)，replay 由 `planner_input_fingerprint` 唯一性保证）；input / plan fingerprint = canonical JSON SHA-256；`verify_research_plan_integrity` 只重放 stored payload（tamper → `ResearchPlanIntegrityError`，不 repair）。
- **Deterministic SourceRouter（spec J/K）**：0 LLM；`SourceRoutePlan` v1 把每个 need 确定性映射到 `SourceCapability` 现有能力（ISSUER_IR 当前无 provider → `provider_unavailable`）；provider 快照记录路由当时的 registry 状态；持久化 `research_plan_routes`（`UNIQUE(research_plan_id, router_version)`）；`verify_research_plan_route_integrity` 不重新 route / 不查 registry。
- **ResearchPreparation（spec L/M/N/O/P/Q/S）**：按 company / cutoff / period / provenance 从现有 artifacts 解析 needs；`MissingResearchNeed`（`not_found` / `insufficient_evidence` / `missing_period` / `missing_metric` / `missing_macro_observation` / `missing_valuation_comparison` / `provider_unavailable` / `unsupported_need`）；数据不足 → `ready_for_analysis=false`（**不伪造数据**）；只按 plan 声明的 modules 构造输入；尊重 `critical_claim_eligible` / authority-tier / no-lookahead；ready=true → 构造有效 `Stage4WorkflowRequest`（auto work plan）。
- **测试（spec T/U/W）**：planner contracts 单测（14）+ service/router/preparation 集成（23）+ E2E（真实 PG + Fake planner + 真实 Stage4WorkflowRunner 到 SynthesisResult；缺失 valuation comparison → ready=false → 0 Stage4 run）+ Migration 0040 downgrade guard（空表通过 / 有行拒绝）。
- **API（spec R）**：本轮未新增 research-plan API（推迟 7A.2）。

## 7A.2：Research Planning API + 规划驱动执行（planned）

- `POST /research-plan`（触发 create + route）、`GET /research-plan/{id}`、`POST /research-preparation`（或按任务聚合入口）；前端最小入口（如任务详情页展示 research plan / readiness / missing needs）。
- 规划驱动执行：ready=true 时自动进入 Stage 4 执行；ready=false 时展示 MissingResearchNeeds 供分析师补齐或触发受控的资料获取。

## 7B：受控资料获取（planned）

- 对 `MissingResearchNeed` 提供受控获取入口：官方披露下载、新闻采集、宏观数据刷新、财务 / 估值对比补齐——全部经 router 能力白名单 + 人工确认（不自动静默抓取）。

## 7C：Stage 7 整体闭环（planned）

- 自动 Source Planning → 自动研究 → 报告 → 审核的端到端编排；ResearchBackflow 与规划反馈回路（不足的证据需求回流为新的研究计划）。
