# ADR-0034：Relative Valuation Claim Provenance（阶段 4C.2B.1）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 背景

4C.2A（ADR-0033）建立了相对估值的数据与比较基础：`Source → EvidenceCard(metric)
→ ValuationMetricObservation → RelativeValuationComparison`。4C.2B 的目标是把
这些数值接上分析判断，形成 Relative Valuation Claim。

4C.2B 拆分为两个子阶段：

- **4C.2B.1（本阶段，Relative Valuation Claim Provenance）**：把**引用已登记
  `RelativeValuationComparison`** 的 Relative Valuation Claim 确定性登记为
  Claim + ClaimProfile + Comparison links + 自动展开的 Evidence links，形成
  **Claim → ClaimRelativeValuationComparisonLink → RelativeValuationComparison
  → ValuationMetricObservation → EvidenceCard → Source** 完整可重算证据链，
  使 Audit 能重算 peer median / premium 并知道 judgment 基于哪些 peer
  comparisons。**0 LLM / 0 Chroma / 0 LangGraph / 0 Report / 0 Audit**。
- **4C.2B.2（next）**：把 provenance 基础接上 LLM 分析（未来阶段，不在本
  ADR 范围）。

**Comparison 不是 EvidenceCard**：Comparison = derived deterministic fact
（4C.2A 程序公式输出）；EvidenceCard = source-backed fact（来源原话）。本阶段
保持分层，不把 Comparison 伪装成 EvidenceCard。

## 决策

1. **Migration 0027（两张表，全部带 CHECK / UNIQUE / INDEX）**：
   `claim_relative_valuation_comparison_links`（claim_id UUID PK FK claims
   CASCADE + comparison_id UUID PK FK relative_valuation_comparisons RESTRICT +
   relation VARCHAR(16) PK；CHECK relation ∈ supports/contradicts/context；
   UNIQUE(claim_id, comparison_id)；INDEX comparison_id）与
   `relative_valuation_claim_profiles`（claim_id UUID PK FK claims CASCADE；
   assessment VARCHAR(32) + analysis_as_of DATE +
   profile_schema_version INTEGER + created_at；CHECK assessment ∈
   relative_high/broadly_in_line/relative_low/mixed/uncertain、
   profile_schema_version >= 1）。**downgrade guard**：profiles 或 links 任一
   表有行 → 拒绝回滚（`RuntimeError`，alembic_version 保持 0027，数据完整
   保留）；两表全空 → 回滚 0026 成功。

2. **Version boundary**：`VALUATION_CLAIM_SCHEMA_VERSION=7`（claims.
   claim_schema_version 的当前值，含 Comparison links + Claim profile）、
   `VALUATION_CLAIM_PROFILE_SCHEMA_VERSION=1`。**不改** generic Claim v1 /
   Financial v2/v3 / Macro v4/v5/v6。两个 fingerprint 的 payload 都含 schema
   version——升级 = 新指纹 = 新行，历史行原样保留（无 update API）。

3. **ValuationClaimDraft（专用新类，不污染 ClaimDraft / FinancialClaimDraft）**
   ：只允许语义输入（company_id / research_question / analysis_as_of /
   statement / assessment / confidence / importance / support/contradict/context
   comparison ids / additional support/contradict/context evidence ids /
   analyst_name / analyst_version / analyst_model_id optional）。**固定
   analysis_domain=valuation、claim_kind=relative_valuation**（draft 不提供这
   两个字段，Service 强制）。**至少 1 个 support_comparison_id**；同一 comparison
   不能跨 relation 重复；**v1 最多 3 个 comparison**（PE / PB / PS 各最多 1 个）。
   derived Evidence IDs / fingerprint / created_at 一律由 Service 从真实数据
   确定性派生，调用方不得手工伪造。

4. **Comparison integrity（复用真实服务，不复制 formula / replay logic）**：
   通过 `RelativeValuationComparisonService.verify_comparison_integrity(...)`
   （4C.2B.1 新增的 **public shared helper**，返回
   `VerifiedComparison | None`）逐条重放校验。comparison 缺失 →
   `ValuationClaimComparisonNotFound`；target_company_id != draft.company_id →
   `ValuationClaimComparisonMismatch`；重放损坏 → `ValuationClaimIntegrityError`
   （**不 repair**）。

5. **Dates（严格一致，不自动对齐）**：所有 comparison.analysis_as_of ==
   draft.analysis_as_of（→ `ValuationClaimAnalysisDateMismatch`）；所有
   metric_as_of 相同（→ `ValuationClaimMetricDateMismatch`）。

6. **Peer-set 一致性（无 silent intersection/union）**：所有 comparison 必须
   使用完全相同的 peer_company_id set（→ `ValuationClaimPeerSetMismatch`）。
   程序不自动做集合合并 / 交集。

7. **Metric uniqueness**：metric_code 不得重复（→
   `ValuationClaimDuplicateMetric`）；v1 最多 PE / PB / PS 三个 comparison。

8. **Relation semantics（程序不根据 premium 自动决定 relation）**：
   Comparison 承担 supports / contradicts / context 三个 relation，由 draft
   显式指定；程序**不**写 hidden thresholds、不从 premium_discount_to_median
   自动推导 relation。

9. **Automatic Evidence expansion（spec N）**：每个 comparison → target
   Observation + 所有 peer Observations → source EvidenceCards 自动加入
   ClaimEvidenceLinks，**全部为 relation=context**。跨 comparison 对 shared
   Evidence context 幂等去重。

10. **Additional Evidence（spec O）**：保持 caller 指定的 supports /
    contradicts / context relation；Evidence 必须存在且 company_id ==
    draft.company_id（→ `ValuationClaimEvidenceCompanyMismatch`）；与自动
    expansion 的 context Evidence 关系冲突 → `ValuationClaimRelationConflict`
    （不静默选一个）。

11. **Assessment contract（spec P）**：`assessment` 是**分析判断**（结构化
    输入），**不是 deterministic formula 输出**；程序不写 hidden thresholds、
    不从 premium 自动推导（`premium>20%→relative_high` 之类）；**不做**
    buy/sell/bullish/bearish/cheap/expensive。

12. **Critical policy（spec Q）**：critical Claim 要求每个 support Comparison
    的 target Observation + 所有 peer Observations 的 source Evidence **全部**
    `critical_claim_eligible_snapshot=true`（→
    `ValuationClaimCriticalEvidenceInsufficient`）；additional supports **不能
    替代**。

13. **Fingerprint（spec R）**：canonical JSON + SHA-256（sort_keys + 固定
    separators + UTF-8），包含 claim_schema_version=7 / profile_schema_version=1
    / company_id / research_question / analysis_as_of / statement / assessment /
    confidence / importance / analyst 身份 / 按 relation 分组的 comparison 组
    （comparison_id + comparison_fingerprint）/ 按 relation 分组的 evidence 组
    （evidence_card_id + evidence_fingerprint，含自动展开 + additional）。
    **不含 claim_id / created_at**。同一完全相同 claim → 同一指纹 → replay
    同一行；任一变化 → 新指纹 → 新 Claim（修改 = 新 Claim，无 update API）。

14. **Persistence（spec S，两步提交，镜像 FinancialClaimService）**：
    (a) 短 DB session 加载全部 Comparison refs + additional Evidence refs 并
    完成全部校验与派生，立即关闭 connection（纯函数阶段不持有 DB 连接）；
    (b) 短 transaction：create_or_get Claim（ON CONFLICT(claim_fingerprint)，
    无进程锁）→ created=True 时同事务插入 Profile + Comparison links +
    Evidence links（任一失败 → 整条 rollback，**0 partial write**）；
    created=False 时重新加载全部行并重新派生逐项核实（损坏 →
    `ValuationClaimIntegrityError`，不自动 repair）。任何 SQLAlchemyError →
    整条 rollback + `ValuationClaimPersistenceFailed`。**并发 → 最终 1 Claim +
    1 Profile + 1 套 links**。无 update API。

15. **Batch（spec T）**：`create_claim_batch(drafts)` 支持 1..3 条（超出 →
    `ValuationClaimDraftError`）。**all-drafts-validate-first**：先对全部 drafts
    加载引用并完成派生（任一失败 → 整批拒绝，0 写），然后**单 transaction**
    逐个 create_or_get + links；后续失败 → 整批 rollback。items 按 input
    drafts 顺序返回（`ValuationClaimBatchResult`，含 created / replayed 分组
    派生属性）。

16. **Replay（spec U）**：schema v7 replay 重新加载 Claim / Profile /
    comparison links / evidence links / Comparisons / peers / Observations /
    EvidenceCards，重新派生并逐项核实；任一损坏 →
    `ValuationClaimIntegrityError`，**不 repair**。

17. **测试（spec W）**：`tests/integration/test_valuation_claim_service.py`
    （34 项集成：Gate / claim contract / comparison validation / automatic
    evidence / critical / persistence / batch / corruption / E2E provenance /
    boundary）+ `tests/integration/test_migration_0027_downgrade_guard.py`
    （3 项 migration downgrade guard，isolated 临时 PG）。全程 0 真实 LLM /
    0 Chroma / 0 LangGraph / 0 Report / 0 Audit。

18. **Docs（spec X）**：本 ADR；README + stage-4-plan 状态更新（4C.1=FINAL、
    4C.2A=FINAL、4C.2B.1=completed、4C.2B.2=next、4D=later）。

## 边界

- **不调用 LLM / 不调 DeepSeek / 不查 Chroma / 0 Retrieval**。
- **不开始 4C.2B.2**（LLM 分析阶段）。
- 不创建 Report / DraftSection / ReviewIssue / Audit；不接 LangGraph 分析
  节点；不开放 HTTP API。
- 不修改 generic Claim / Financial v2/v3 / Macro v4/v5/v6 schema。
- 不实现绝对公允价值 / target price / DCF / PEG / EV / EBITDA / FCFF / FCFE /
  dividend model / 买卖建议 / 短期预测。
- 不 batch update 历史 rows；不反推历史 cutoff。
- **不把 `RelativeValuationComparison` 伪装成 EvidenceCard**（保持分层）。
