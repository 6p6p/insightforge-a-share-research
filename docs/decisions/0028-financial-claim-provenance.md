# ADR-0028：Financial Claim Provenance（阶段 4B.2C.1）

- 状态：已接受
- 日期：2026-08-09
- 决策人：InsightForge 项目

## 决策

1. **4B.2C.1 状态：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Claim HTTP 端点）**。4B.2C.1 是 4B.2C Financial Analyst 的**第一环（provenance 基础）**：把引用已登记 `FinancialCalculation` 的 Financial Claim 确定性登记为 Claim + 自动展开的 Evidence 链接 + Calculation 链接，形成 **Claim → ClaimFinancialCalculationLink → FinancialCalculation → FinancialMetricObservation → EvidenceCard → Source** 完整可重算证据链。**0 LLM / 0 Chroma query / 0 Retrieval / 0 LangGraph analysis node / 0 Report / 0 Audit**；**不开放 API**。`claims` / `claim_evidence_links` / `claim_financial_calculation_links` 允许存在；`report_outlines` / `report_sections` / `reports` / `review_issues` 不得存在。

2. **不把 FinancialCalculation 伪装成 EvidenceCard**：Calculation = derived deterministic fact，EvidenceCard = source-backed fact，两者保持分层。调用方/未来 LLM 只选 Calculation refs，程序自动加载 source Evidence；**caller/LLM 不得手工伪造 derived Evidence IDs**（没有接受手工 derived Evidence ID 的通道）。

3. **Migration 0022**（`alembic current` = 0022 head，down_revision = 0021）：
   - `claim_financial_calculation_links`：claim_id（FK claims **CASCADE**，删 Claim 删 links）、calculation_id（FK financial_calculations **RESTRICT**，计算存在期间 link 不静默消失）、relation（supports / contradicts / context）、created_at；**PK(claim_id, calculation_id, relation)**、**UNIQUE(claim_id, calculation_id)**（`uq_claim_financial_calculation_links_claim_calculation`，同一 Calculation 对同一 Claim 只能一种 relation）、CHECK relation 白名单、索引 calculation_id（反向查所有引用某 Calculation 的 Claims）。
   - **Gate 0 C**：`financial_calculation_inputs` 增加 `uq_financial_calculation_inputs_calc_observation`（UNIQUE(calculation_id, metric_observation_id)）——同一 calculation 内同一 Observation 只能绑定一个 role，杜绝同源数值被重复当作两个输入。**不修改已落地的 0021**，在 0022 中追加。
   - **downgrade guard**：claim_financial_calculation_links 有行时拒绝回滚（不静默丢弃 Claim ↔ Calculation 链接）；无数据时才允许回到 0021（isolated 临时 PG 验证）。

4. **Schema v2（`FINANCIAL_CLAIM_SCHEMA_VERSION = 2`）**：只有存在 FinancialCalculation links 的 financial Claim 用 v2（FinancialClaimService 总是 v2，因为至少 1 个 support_calculation）。**禁止回头改变 v1 Claim fingerprint**：已有 v1 Claims 继续用 `compute_claim_fingerprint`（`claims.claim_schema_version=1`）正常 replay，v2 是新函数新 payload（`compute_financial_claim_fingerprint`）。
   - **v2 fingerprint = v1 内容 + 按 relation 排序的 calculation lists**：`claim_schema_version=2` / company / research_question / statement / analysis_domain=financial / claim_kind / confidence / importance / analyst_name / analyst_version / analyst_model_id / 按 relation 分组的 ordered evidence_card_ids（自动展开 + additional 合并后的最终链路）/**supports_calculations / contradicts_calculations / context_calculations**（每 entry 至少 `{"calculation_id", "calculation_fingerprint"}`）。canonical JSON（sort_keys + 固定 separators + ensure_ascii=False）+ SHA-256；**不含 claim_id / created_at**。同一完全相同 Financial Claim → 同一指纹 → replay 同一行；任一变化 → 新指纹 → 新行，旧行保留（无 update API）。

5. **`FinancialClaimDraft` 专用新类**（`app/claims/financial_contracts.py`，**不污染 ClaimDraft**）：语义输入 = company_id / research_question / statement / confidence（FinancialClaimConfidence）/ importance（FinancialClaimImportance）/ claim_kind / support/contradict/context_calculation_ids / additional_support/contradict/context_evidence_ids / analyst_name / analyst_version / analyst_model_id（optional）。**固定 analysis_domain=financial**；claim_kind ∈ fact / inference / risk（**不做 relative_valuation**，估值留 4C）；**至少 1 个 support_calculation_id**；同一 calculation 不能跨 relation 重复；同一 additional Evidence 不能跨 relation 重复；所有 id list 去重 + canonical 排序。构造时校验（FinancialClaimDraftError），Service 不再重复校验。

6. **Automatic Evidence expansion（H）**：调用方只提供 Calculation refs；程序加载每个 Calculation 的 source Evidence（inputs → Observations → source_evidence_card_id），**自动把 source Evidence 加入 ClaimEvidenceLinks**（supports→supports / contradicts→contradicts / context→context）。`additional_support/contradict/context_evidence_ids` **仅用于管理层解释 / 业务事件 / 风险说明等额外定性 Evidence**，与自动展开的 source Evidence 分开管理。

7. **Relation propagation（I）**：Calculation relation → underlying source Evidence 使用**同 relation**；同一 Evidence 被多个 Calculations / additional Evidence 推导成**不同 relation → `FinancialClaimRelationConflict`**（**不静默选一个**）；同一 relation 重复（多个 Calculations 共享同一 Evidence）→ 幂等去重（一个 Evidence 只产生一条 link）。additional Evidence 与自动推导 relation 冲突 → 同样拒绝。

8. **Company / integrity validation（J，不 repair）**：加载全部 Calculation refs，逐个执行 `FinancialCalculationService.verify_calculation_integrity`（重新加载 inputs + Observations + 重新派生逐项核实）——缺失 → `FinancialClaimCalculationNotFound`；company != draft → `FinancialClaimCalculationMismatch`；重放损坏 → `FinancialClaimIntegrityError`；再加载 inputs → Observations（缺失 / company 不一致 → `FinancialClaimIntegrityError`）→ 自动展开 source Evidence（缺失 / 跨公司 → `FinancialClaimEvidenceCompanyMismatch`）；additional Evidence 同样校验。

9. **Critical policy（K）**：**FinancialCalculation 本身不能提升 source authority**；importance=critical 时仍需 **≥1 个最终 supports Evidence** 满足 `critical_claim_eligible_snapshot=true`（自动展开 + additional 合并后的最终链路），否则 `FinancialClaimCriticalEvidenceInsufficient`。复用 ClaimService 的 source policy 语义。

10. **Repository + Service**（`app/repositories/claim_financial_calculation_link_repository.py` / `app/services/financial_claim_service.py`）：
    - `ClaimFinancialCalculationLinkRepository`：`list_by_claim(claim_id)` + `bulk_insert(links)`。
    - `FinancialClaimService.create_claim(draft)`（构造函数**只持有 sessionmaker**）三步：**短 DB session 加载 + 校验**（J，连接即刻关闭，纯函数阶段不持 DB 连接）→ **纯函数派生**（自动展开 / relation 传播 / critical policy / v2 fingerprint，无 DB）→ **短 DB transaction**：`ClaimRepository.create_or_get`（PG `ON CONFLICT(claim_fingerprint) DO NOTHING RETURNING`，**无进程锁**）→ created=True 时 bulk insert evidence links + calculation links；created=False 时 `_verify_replay`；`FinancialClaimIntegrityError → 显式 rollback + raise`；任一 `SQLAlchemyError → 整条 rollback + FinancialClaimPersistenceFailed`（**0 partial write**：Claim + ClaimEvidenceLinks + ClaimFinancialCalculationLinks 一个事务）。**并发相同 fingerprint → 最终 1 Claim + 1 套 links**（输家 INSERT 阻塞至赢家 commit，然后 replay 看到 committed links）。**无 update API**。

11. **Replay（M，不 repair）**：schema v2 replay 时重新加载 Claim / evidence links / calculation links / Calculations / inputs / Observations / EvidenceCards，重新执行 Calculation integrity、自动 Evidence expansion、critical policy、relation conflict、v2 fingerprint，**逐项核实**（links 按 relation 排序比对 + claim 全部字段 + fingerprint）；任一损坏 → `FinancialClaimIntegrityError`，**不自动 repair**（修改 = 新 Claim = 新 fingerprint = 新行）。

12. **错误分类（`app/claims/financial_errors.py`，9 个 + 稳定 code）**：`FinancialClaimError`（基类）+ DraftError / CalculationNotFound / CalculationMismatch / EvidenceCompanyMismatch / RelationConflict / CriticalEvidenceInsufficient / IntegrityError / PersistenceFailed。错误消息不包含 evidence / calculation 正文、prompt、API key、DB URL、raw content。

13. **测试（21 项集成新增 + Gate 0 全套）**：`tests/integration/test_financial_claim_service.py`（真实 PG + 真实服务链 seed company/evidence/observation/calculation，零 Chroma/LLM）：valid persistence（Claim + evidence links + calculation links）、requires ≥1 support calculation（FinancialClaimDraftError）、calculation missing / company mismatch、calculation corruption → replay IntegrityError（不 repair）、automatic Evidence expansion、multiple calculations share Evidence same relation → dedupe、conflicting propagated relation → reject（FinancialClaimRelationConflict）、additional Evidence merged / conflict / missing、critical eligible accepted、critical without eligible source → reject、schema v2 fingerprint deterministic、calculation change → new Claim、relation change → new Claim、replay、concurrency → 1 Claim、corruption → integrity error、no EvidenceCard / FinancialCalculation modified、E2E provenance SQL trace（Claim → link → Calculation → inputs → Observation → EvidenceCard → Source 全链路 company 一致）+ 精确阶段边界。**Gate 0 追加**：同期间期 ratio（margin 跨期拒绝 / 同期望通过 / 必须 duration；debt_to_assets_ratio 跨时点拒绝 / 同时点通过 / 必须 instant）、metric/scope 政策（net_profit_parent / excl_nonrecurring / equity_parent 只允许 consolidated）、calculation input distinctness（draft 拒绝重复 observation + DB UNIQUE 拒绝直接 SQL 重复绑定）。**0 LLM / 0 Chroma query / 0 Retrieval / 0 LangGraph / 0 Report 表**。

14. **文档边界（O）**：stage-4-plan.md / README 统一为 **4B.2B completed**、**4B.2C.1 completed**、**4B.2C.2 next = Financial Analyst（LLM 解释数值）**、4C later=Macro Context / Valuation、4D later=Claim Synthesis / Conflict / Evidence Gap、Stage 5=Report + Audit（不提前标记）。Alembic head = 0022。

## 后果

- **完整可重算证据链**：Financial Claim 不再只指向 Evidence，而是指向可确定性重算的 Calculation——Audit 可重新执行 `verify_calculation_integrity` 验证"结论所依赖的财务数值"没有被篡改。
- **调用方零伪造空间**：derived Evidence IDs 一律由程序从 Calculation → inputs → Observations 自动展开；additional Evidence 白名单只放定性说明。任何派生 Evidence 无法手工注入。
- **v1 语义冻结不受影响**：已有 v1 generic Claims 继续用 v1 fingerprint 正常 replay；v2 是新 payload 新函数，不回头改 v1。
- **零 partial writes / 零 update API**：任一校验失败 → 0 写；修订 = 新 fingerprint + 新行，旧行保留（审计可回溯）。
- **并发幂等**：PG `ON CONFLICT DO NOTHING`（无进程锁），重复 / 并发 create_claim 只产生一行 + 一套 links。
- **DB 层双重防线**：relation 白名单 / UNIQUE(claim_id, calculation_id) / inputs UNIQUE(calculation_id, metric_observation_id) 全部下沉为约束，应用层校验只是第一道。

## 明确不做（边界）

不实现 4B.2C.2 Financial Analyst（LLM 解释数值，下一环）；不实现 Macro / Valuation（4C）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不生成 Report / DraftSection / ReviewIssue / Audit（Stage 5）；不接 LangGraph 分析节点；不调用 Retrieval / Chroma / BGE / LLM / DeepSeek / RawArtifact bytes；不开放 HTTP API；**不开始 4B.2C.2**。
