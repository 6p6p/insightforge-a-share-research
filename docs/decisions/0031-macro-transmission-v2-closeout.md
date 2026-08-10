# ADR-0031：Macro Transmission v2 Closeout（阶段 4C.1A Final）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 背景

ADR-0030 建立了 Macro Claim 的传导 provenance 基础（migration 0023，Claim →
MacroTransmissionChain → {Macro Evidence, Company Exposure Evidence, Observed
Effect}）。Final Closeout 审计真实代码发现 4 处结构性边界问题，本 ADR 通过
**migration 0024 + schema v5/v2 + 确定性策略收紧**修正，并冻结语义，作为
4C.1A 的 **FINAL** 状态。**不改动 generic Claim schema / 不批量改写历史
v4/v1 行 / 不覆盖 ADR-0030 的历史设计**。

## 决策

1. **Transmission fingerprint ownership（migration 0024）**：
   `macro_transmission_chains.transmission_fingerprint` 的 **global UNIQUE 是错的**：
   相同 transmission semantics + 不同的 claim statement / analyst_version 必须
   允许 **new Claim + new MacroTransmissionChain**（fingerprint 可相同），旧记录
   保留。0024 **DROP** `uq_macro_transmission_chains_transmission_fingerprint`，
   改为普通索引 `ix_macro_transmission_chains_transmission_fingerprint`
   （查询 / 审计用）。identity 由 `claims.claim_fingerprint` UNIQUE 负责；
   `macro_transmission_chains.claim_id` UNIQUE 保证"一个 Macro Claim 一条链"。
   **不引入 transmission ↔ claim many-to-many join table**。仓库层
   `create_or_get` 改为 `create`（plain INSERT，fingerprint 不再参与
   ON CONFLICT），`get_by_fingerprint` 加 `.limit(1)` 注明非 identity 查询。

2. **Version boundary（v5/v2 当前，v4/v1 冻结 legacy）**：
   `MACRO_CLAIM_SCHEMA_VERSION=5`、`MACRO_TRANSMISSION_SCHEMA_VERSION=2`；
   `MACRO_CLAIM_SCHEMA_VERSION_V4=4`、`MACRO_TRANSMISSION_SCHEMA_VERSION_V1=1`
   冻结不改写。**两个 fingerprint 的 payload 都包含 schema version**——
   v4/v5、v1/v2 永不误 collision：版本升级 = 新 fingerprint = 新 Claim + 新链，
   历史 v4/v1 对象原样保留。**不污染 generic ClaimDraft / FinancialClaimDraft**。

3. **Macro driver v2（external event document 资格）**：macro_driver 允许
   (A) origin_type=macro_observation，或 (B) 经过明确筛选的 external event
   document Evidence：`SourceRecord.document_type=news_article` **且**
   `evidence_type ∈ {event, fact, statement}`（`_DOCUMENT_DRIVER_EVIDENCE_TYPES`，
   **不含 context——背景不是 driver；不含 metric——结构化数值优先
   MacroObservation**）。**不凭空新增 policy_document / geopolitical_document
   等未存在的 enum**。同一 Evidence 不能同时作为 macro_driver 与
   company_exposure / observed_effect（沿用 draft 角色互斥）。

4. **Information availability（no-lookahead，v2 收口）**：
   **全部**进入 Claim 的 Evidence（transmission roles + additional）都必须
   `availability_at.date() <= analysis_as_of`，未来 → `MacroClaimFutureEvidence`。
   - document 卡：`SourceRecord.published_at`（真实发布时间）；为 NULL 时用
     `SourceRecord.acquired_at` 保守 fallback；**绝不用 reporting_period_end**；
   - macro 卡：`MacroDatasetSnapshot.fetched_at`——系统最晚何时已取得该观测；
     **绝不用 normalized_period_start**（那是排序 / 索引用的占位年首，不是真实
     可用时间）。
   任何卡无可用时间 → `MacroClaimTemporalEvidenceInsufficient`，**不伪造缺失日期**。

5. **Time alignment / overclaim policy v2（确定性一致性，不自动猜 lag）**：
   (1) `impact_status=observed_impact` 必须 `time_alignment=aligned`（声称"影响
   已发生"不能同时说"时间对齐不确定"）；
   (2) `time_alignment=uncertain` 只允许 `plausible_impact` + `claim_kind=risk` +
   `importance=normal`（不确定 → 不能创建 critical / 不能声称已发生因果）；
   (3) `importance=critical` 需要 `time_alignment=aligned` **且**
   `effect_direction != uncertain`，并保持 eligible 双腿（macro_driver +
   company_exposure，observed_impact 时额外 observed_effect；additional support
   不能替代）。违反 → `MacroClaimTimeAlignmentPolicy` / `MacroClaimCriticalEvidenceInsufficient`
   （新稳定 code：`macro_claim_time_alignment_policy`）。**不自动降级、不猜 lag**。

6. **ClaimEvidence relation 语义（沿用，Closeout 冻结）**：macro_driver /
   company_exposure / observed_effect 自动展开为 ClaimEvidenceLinks 一律
   relation=context（单条证据不能独立证明因果，因果语义由 Chain 承载）；
   additional 保持调用方指定的 supports/contradicts/context。

7. **Replay integrity 版本感知（不 repair）**：已有 fingerprint 时按既有
   Claim 的 `claim_schema_version` 分叉重放——`==5` → v2 当前规则（含 document
   driver 资格 + availability + time-alignment policy）；`==4` → legacy v1/v4
   历史规则（macro_driver 必须 macro_observation、可用时间用
   normalized_period_start / source_published_at / reporting_period_end、不做 v2
   document driver 与 time-alignment policy，**防止把旧历史对象误判损坏**）；
   其他值 → `MacroClaimIntegrityError`。任一损坏 → `MacroClaimIntegrityError`，
   **不自动 repair**（修改 = 新 Claim = 新链 = 新行）。

8. **Migration 0024 downgrade guard（数据安全）**：仅当不存在 (a) 任何
   `transmission_schema_version >= 2` 的链、(b) 任何 `analysis_domain='macro'
   AND claim_schema_version >= 5` 的 Claim、(c) 任何重复
   `transmission_fingerprint` 时，才允许恢复 UNIQUE 回到 0023；否则显式拒绝
   （**不删除数据 / 不修改 fingerprint / 不静默合并链**），`alembic_version`
   保持 0024。isolated 临时 PG 验证三条拒绝路径 + safe legacy v1/v4 恢复路径。

9. **Schema finding：TemporalEvidenceInsufficient 是防御性分支**：
   `source_records.acquired_at` 与 `macro_dataset_snapshots.fetched_at` 在 schema
   中都是 NOT NULL → v2 下 availability 总是可解析，该错误类在正常数据下不可达，
   保留为防御性不变量（未来若放开 NULL 仍有确定语义）。

## 后果

- **同指纹多链正确表达**：相同传导语义 + 不同 statement / analyst_version →
  new Claim + new Chain，历史链保留，可追溯。
- **历史对象稳定**：v4/v1 数据不批量改写；replay 用 legacy 规则不误判损坏；
  版本升级 = 新 fingerprint = 新对象，永不 collision。
- **no-lookahead 收口**：availability 全部来自真实取得时间（published_at /
  acquired_at / fetched_at），绝不把占位期日当可用时间。
- **overclaim 防御**：observed 必须 aligned、uncertain 只允许
  plausible+risk+normal、critical 需要 aligned + 已知方向——确定性一致性由
  Service 保证，不自动降级、不猜 lag。
- **FINAL 状态**：4C.1A 以 4C.1A FINAL 关闭；Alembic head = **0024**。

## 明确不做（边界）

不实现 4C.1B Structured Macro Context Analyst（**不开始 4C.1B**）；不实现
Valuation（4C.2）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不接
LangGraph 分析节点；不生成 Report / DraftSection / ReviewIssue / Audit（Stage 5）；
不开放 Macro Claim HTTP API；不改动 generic Claim schema；不批量 update 历史
v4/v1 rows；不引入 transmission ↔ claim many-to-many join table；不新增
policy_document / geopolitical_document 等未存在的 enum；不新增从未捕获过的
provider_release_at；不实现自动交易 / 技术分析 / 短期预测 / 买卖建议。
