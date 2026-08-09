# ADR-0026：Financial Metric Observation Foundation（阶段 4B.2A）

- 状态：已接受
- 日期：2026-08-09
- 决策人：InsightForge 项目

## 决策

1. **4B.2A 状态：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Metric HTTP 端点）**。4B.2A 是财务分析链条（4B.2A Observation → 4B.2B Deterministic Calculation → 4B.2C Financial Analyst）的**确定性第一环**：把来源于真实财务 Evidence 的**原始财务数值**登记为 `FinancialMetricObservation`（**Document Evidence → Observation**），**不计算**同比 / 环比 / margin / ratio、**不调用 LLM**、不自动从 PDF 表格猜财务数字。角色边界：**代码登记数值，LLM 不参与数值输入**——后续（4B.2B/4B.2C）的 Financial 计算与分析只能基于这些确定性 Observation，不能另起炉灶。

2. **无 HTTP 端点、无 LangGraph 节点**：4B.2A 只提供 `FinancialMetricService.create_observation(draft)`（Python service 层，调用方如 4B.2B 的确定性计算服务直接调用），不开放 API、不接 LangGraph 顶层编排。**不创建 Claim / Report / ReviewIssue**（`claims` / `claim_evidence_links` 允许存在，`report_outlines` / `report_sections` / `reports` / `review_issues` 不得存在）。

3. **Migration 0020（`financial_metric_observations` 表）**：metric_observation_id（UUID PK）、company_id（FK companies RESTRICT）、source_evidence_card_id（FK evidence_cards RESTRICT）、metric_code、statement_scope、period_start（NULL 允许）、period_end、period_kind、source_value_text、raw_value（Numeric(38,12)）、raw_unit、normalized_value_cny（Numeric(38,12)）、metric_schema_version、metric_fingerprint（CHAR(64) UNIQUE）、created_at。8 个 CHECK（statement_scope / period_kind / raw_unit 枚举、period_consistency、fingerprint `^[0-9a-f]{64}$`、schema ≥ 1、metric_code / source_value_text 非空）+ 4 个索引（company_id / source_evidence_card_id / metric_code / period_end）。**downgrade guard**：有行时拒绝回滚（删除表会静默丢弃已登记数值事实），无数据时允许回到 0019（isolated 临时 PG 验证两条路径）。

4. **Metric v1 taxonomy（冻结）**：`FINANCIAL_METRIC_SCHEMA_VERSION = 1`；**11 个 metric_code**（revenue / operating_cost / operating_profit / profit_before_tax / net_profit / net_profit_parent / net_profit_parent_excl_nonrecurring / operating_cash_flow_net / total_assets / total_liabilities / equity_parent），先少而精、不一次写几十个科目；`statement_scope`（consolidated / parent）；`period_kind`（duration / instant）；`raw_unit` 4 档（yuan / thousand_yuan / ten_thousand_yuan / hundred_million_yuan）；**货币 v1 只支持 CNY**（`normalized_value_cny`）。metric_code → statement family 用**确定性 mapping**（`statement_family`：income statement / cash flow / balance sheet），**不让 caller 传 statement_type**，避免 metric/type 不一致。

5. **Period 规则（确定性）**：balance sheet metric → `period_kind=instant`、`period_start=NULL`；income statement / cash flow metric → `period_kind=duration`、`period_start NOT NULL 且 <= period_end`。Service 根据 metric_code 的 family 确定 expected period_kind，违反抛 `FinancialMetricPeriodError`（DB 侧 `ck_financial_metric_observations_period_consistency` 再兜底）。

6. **Source value exactness（I）**：`source_value_text` 必须是 `EvidenceCard.quote_text` 的 **exact substring**——`quote_text.count(source_value_text)`；**0 次 → `FinancialMetricValueNotFound`、>1 次 → `FinancialMetricValueAmbiguous`**。不做 fuzzy / normalize / 自动纠错 / LLM 修正；不信任 caller 提供的 raw_value / normalized_value_cny（draft 里根本不提供这两个字段）。

7. **确定性数字解析（J）**：`parse_financial_number`（`app/financial/number_parser.py`）用 `Decimal` 全程、**零 float**；v1 严格语法（可选千分位 + 可选小数 + 可选正负号/括号负号 + 首尾空白），**拒绝**科学计数法 / 百分号 / 中文数字 / 约数 / 带单位 / 畸形千分位 / 双小数点 / 括号不平衡 / 括号带符号 / 内部空白 / 非字符串。`scaleb` 处理小数位，`str(Decimal)` 保留 scale（指纹序列化语义一致）。

8. **单位归一化（K）**：`normalize_value_cny(raw_value, raw_unit)` = raw_value × 系数（yuan ×1 / thousand_yuan ×1000 / ten_thousand_yuan ×10000 / hundred_million_yuan ×100000000），全 Decimal 无 float；未知单位 → `FinancialMetricValueNotNumeric`。`raw_value` / `raw_unit` / `normalized_value_cny` 三者都落库（可审计换算过程）。

9. **Provenance（L）**：只通过 `source_evidence_card_id`（FK evidence_cards RESTRICT）回溯：Observation → EvidenceCard → DocumentChunk → ChunkSet → ParsedSource → SourceRecord → RawArtifact。**不复制 locator_refs** 到本表；不访问 Chroma / BGE / LLM / RawArtifact bytes。company 归属由 EvidenceCard.company_id 决定（Service 校验与 draft.company_id 一致）。

10. **Fingerprint / replay（M）**：`compute_metric_fingerprint` = canonical JSON（sort_keys + 固定 separators + UTF-8）+ SHA-256，含 metric_schema_version / company_id / source_evidence_card_id / metric_code / statement_scope / period_start / period_end / period_kind / source_value_text / raw_value（str()）/ raw_unit / normalized_value_cny（str()）；**不含 metric_observation_id / created_at**。同一完全相同 observation → 同一指纹 → replay 同一行（PG `ON CONFLICT(metric_fingerprint) DO NOTHING`，并发幂等、无进程锁）；value / unit / period / metric code / scope / source evidence / company 任一变化 → 新指纹 → **新行，旧行保留**（修订 = 新 observation）；**无 update API**。Replay 完整性：重新加载 EvidenceCard + 重新派生 13 项逐字段比对（company / source evidence / metric code / scope / period / value / unit / normalized / schema version / fingerprint），任一损坏 → `FinancialMetricIntegrityError`，**不自动 repair**。

11. **FinancialMetricService（`app/financial/service.py`）**：构造函数**只持有 sessionmaker**（不持 DB 连接、不持 LLM/chroma 客户端）。`create_observation(draft)`：短 DB session 加载校验 EvidenceCard（缺失 / origin_type != document_chunk / evidence_type != metric / company 不一致 → `FinancialMetricEvidenceMismatch`，不自动修复）→ 纯函数 `_derive`（exact-match → parse → expected period_kind 校验 → normalize → fingerprint）→ 短 transaction `create_or_get` + replay `_verify_replay`；`except FinancialMetricIntegrityError → await session.rollback(); raise`；`except SQLAlchemyError → rollback + FinancialMetricPersistenceFailed`。

12. **错误分类（`app/financial/errors.py`）**：`FinancialMetricError`（基类）+ `FinancialMetricInputError` / `FinancialMetricEvidenceMismatch` / `FinancialMetricValueNotFound` / `FinancialMetricValueAmbiguous` / `FinancialMetricValueNotNumeric` / `FinancialMetricPeriodError` / `FinancialMetricIntegrityError` / `FinancialMetricPersistenceFailed`。错误消息不包含：evidence 正文、完整 prompt、API key、DB URL、raw content。

13. **测试**：**56 项单元**（`tests/financial/test_contracts.py` 27：taxonomy 冻结 / family mapping / expected period kind / draft 输入防御 / fingerprint 确定性与敏感性 + `tests/financial/test_number_parser.py` 29：正例 8 + 反例 12 + normalize 9，零 DB/零 LLM）+ **22 项集成**（`tests/integration/test_financial_metric_service.py`，真实 PG + 真实 HTML/PDF 服务链，零 Chroma/LLM：HTML / PDF 创建字段与换算、missing / wrong company / non-document origin / non-metric evidence 拒绝、value not found / ambiguous、balance instant / duration requires period_start、replay 复用、**并发 → 1 行**、value / unit / period 变化 → 新行、**replay 篡改 raw_value / normalized_value → IntegrityError**、EvidenceCard 行永不修改、provenance 全链路回溯到 RawArtifact、精确阶段边界 `claims==2 / report 表==0`、Service 只持有 sessionmaker）+ **2 项 migration 0020 downgrade guard**（isolated 临时 PG：无数据降级成功、有 observation 数据降级拒绝且行保留）。全程 **0 LLM / 0 Chroma / 0 Claim / 0 Report**。全量测试：**1311 非集成 + 323 集成通过**，ruff 零告警，`pip check` 通过。

14. **文档边界（Q）**：stage-4-plan.md / README 统一为 **4B.2A completed**、4B.2B next=Deterministic Financial Calculation、4B.2C later=Financial Analyst、4C later=Macro Context / Valuation、4D later=Claim Synthesis / Conflict / Evidence Gap、Stage 5=Report + Audit（不提前标记）。Alembic head = 0020。

## 后果

- **数字事实可追溯、可回放**：所有 Observation 绑定到真实 Evidence quote 的 exact substring，可回溯到 RawArtifact；replay 校验把"登记值"与"从 Evidence 派生值"逐字段比对，损坏即报 IntegrityError，不静默修复。
- **LLM 不碰数值输入**：draft 只允许语义输入（company / evidence / metric_code / scope / period / source_value_text / raw_unit），raw_value / normalized_value_cny / fingerprint 全由确定性代码从真实 Evidence 派生——LLM（4B.2C）只能解释这些已登记的确定性数值，不能自行输出 numeric metric 或算 normalized value。
- **零 partial writes / 零 update API**：任一校验失败 → 0 写；修订 = 新 fingerprint + 新行，旧行保留（审计可回溯）。
- **并发幂等**：PG `ON CONFLICT DO NOTHING`（无进程锁），重复 / 并发 create_observation 只产生一行。
- **DB 层双重防线**：period 一致性 / 枚举 / fingerprint 格式 / schema ≥ 1 全部下沉为 CHECK 约束，应用层校验只是第一道。

## 明确不做（边界）

不实现 Financial calculation（同比 / 环比 / margin / ratio，属于 4B.2B）；不实现 Financial Analyst（4B.2C）；不实现 Macro Analyst / Valuation Analyst（4C）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不生成 Report / DraftSection / ReviewIssue / Audit（Stage 5）；不接 LangGraph 分析节点；不调用 Retrieval / Chroma / BGE / LLM / RawArtifact bytes；不开放 HTTP API；不自动从 PDF 表格猜财务数字（只登记已作为 Evidence quote 的明确数值）；**不开始 4B.2B**。
