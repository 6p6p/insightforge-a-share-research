# InsightForge Product Hardening Report

日期：2026-08-17 · v0.4 Autonomous Research Pipeline 加固 + 前端 UX 简化

## 1. Numeric violation root cause

- Financial Analyst（DeepSeek structured output）在生成 claim statement 时偶发写入未绑定证据的数字/年份/百分比（如 2025 年收入增长 54%），违反 numeric-literal guard（statement 必须纯定性，定量事实只能通过 C 编号引用）。
- 旧行为：guard 违规 → 直接 FinancialAnalysisNumericLiteralForbidden → stage4_execution_failed（单次 LLM 输出问题导致整个 Stage4 失败）。

## 2. 修复方案（Part 1）

- Violation detection → Automatic repair → Retry → 成功继续：
  1. 第一次检测到 unbound numeric literal → 进入 repair flow：重新调用 LLM，system message 追加修复指令（NUMERIC_REPAIR_HINT：删除未绑定数字或改为引用 C/E 编号），最多 3 次 repair（共 4 次调用）；
  2. 3 次 repair 仍失败 → 降级：返回 0-claims 定性结果（relevant=false + reason_code=numeric_reference_downgraded），记录 analysis_warning 日志；**不阻断 Stage4**，synthesis 继续；
  3. 模型瞬时错误（ModelUnavailable）仍走 5 次有界重试（与 repair 流程独立）。
- 变更文件：contracts.py（reason 枚举 + protocol hint 参数）、prompt.py（NUMERIC_REPAIR_HINT + build_analysis_messages 支持 correction_hint）、adapters.py、service.py（repair 循环 + 降级）、fakes + 4 个集成测试文件（fake analyze 签名）。

## 3. 为什么没有降低证据可信度

- numeric validator 完整保留：guard 不删数字、不改写、不绕过——违规输出永远不可能产生 claim；
- 降级路径产出 0 claims（relevant=false），不引入任何无来源数字；修复成功路径下数字只通过 C 编号（确定性 Calculation）进入 claim 关系；
- provenance / financial observation traceability / numeric validation / audit integrity 全部不变（修复前后同一套校验链）。

## 4. UI 修改列表（Parts 2-5，仅 frontend display）

- Part 2 Progress UI：任务进度不再显示百分比 → 状态（未开始/进行中/已完成/等待确认/失败）；移除进度条组件；事件时间线去掉进度百分比（WorkflowProgressPanel / TaskListPage / EventTimeline）；
- Part 3 来源页面中文化：来源类型（annual_report→年度报告等）与提供方（eastmoney→东方财富等）display mapping（frontend/src/utils/display.ts，后端枚举未改）；
- Part 4 证据页面中文化：evidence type（statement→财务报表等）与 origin_type（document_chunk→文档内容等）display mapping；
- Part 5 财务数据页面简化：标题改「补充财务数据（可选）」；隐藏报表口径/文档类型/证据陈述等内部字段（前端确定性派生）；保留 指标/期间/数值/单位/引文/来源说明。

## 5. 测试结果

- 后端单元：2457 passed；集成：1132 passed（含 Part 1 新回归：repair 后成功 / 3 次 repair 降级不阻断 / 旧 guard 测试更新为新语义；5 个 fake 签名修复测试）；
- 前端：95 passed + typecheck + production build 通过；
- ruff 全量干净；alembic head 0050。

## 6. 真实流程（宁德时代 company name only）

- planning → source acquisition（6 来源：3 年报/季报/半年报/IR）→ financial extraction（38 证据卡）→ stage4 → stage5（7 节报告：主营业务/盈利能力/成本产能/研发投入等，数字全部来自证据）→ audit（human_review 路由）→ **waiting_human / awaiting_stage5**（报告完成，等待人工裁决）。
- 相比加固前（research_backflow 卡死），本次到达 awaiting_stage5（最完整自动闭环）。

## 7. Commit hash

- 6a68607 / d652125：fix: harden research flow and simplify product UX
- 5948b67：Parts 2-5（progress UX / display localization / simplified form）
- daa017d：Part 1（numeric auto-repair flow）
- HEAD 当前工作树 clean。
