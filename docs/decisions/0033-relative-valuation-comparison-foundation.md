# ADR-0033：Relative Valuation Comparison Foundation（阶段 4C.2A）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 背景

4C.1B 关闭后，4C 只剩 Valuation。阶段 4C.2 的目标是把相对估值做成**证据驱动**
的比较基础，而不是又一个 LLM 输出：先把真实文档中**原文出现的原始估值倍数**
确定性登记为 Observation，再对**显式 peer 集合**用冻结公式计算 deterministic
比较统计，为 4C.2B（Relative Valuation Claim，Analyst 只做判断）提供可审计的
数值地基。

本阶段（4C.2A）只做**数据与比较基础**：`Source → EvidenceCard(metric) →
ValuationMetricObservation → RelativeValuationComparison`。**不做分类 / 不做
Claim / 不调 LLM**。比较结果是程序确定性派生的分析事实，**不是 EvidenceCard**
（EvidenceCard 只登记来源原话；comparison 引用已登记的 Observation）。

**修正记录**：实现中期发现 `ValuationObservationService._derive` 先做 exact
token-match 再做 numeric parse，导致 `ValuationValueNotNumeric` 对任何非法字面量
（如 `"100万"`）都被 `ValuationValueNotFound` 抢先覆盖而不可达。修正为
**先 grammar parse（`parse_valuation_number` → `ValuationValueNotNumeric`），再
exact-match（0 → `ValuationValueNotFound`，>1 → `ValuationValueAmbiguous`）**：
`NotNumeric` = "本身不是合法纯十进制字面量"，`NotFound` = "是合法数字但不在
quote 里作为完整 token 出现"，两者语义正交、全部可达。该修正已由
`test_value_not_numeric_rejected` 复现验证。

## 决策

1. **架构与角色边界**：`ValuationMetricObservation` = 绑定到真实 metric
   EvidenceCard 的**原始估值倍数事实**（v1 只登记 `pe_ttm` / `pb_mrq` /
   `ps_ttm`；允许 0 / 负倍数作为来源快照，是否可比较由 comparison 阶段校验）。
   `RelativeValuationComparison` = 对显式 peer 集合计算的确定性派生比较事实
   （peer_median / peer_min / peer_max / premium_discount_to_median），**不是
   EvidenceCard**、不复制 locator / quote；PG EvidenceCard 是 provenance truth
   source。**不计算** DCF / PEG / EV / EBITDA / FCFF / FCFE / target price /
   dividend model；不做买卖建议 / 绝对公允价值 / 分类（分类在 4C.2B）。

2. **Migration 0026（三表，全部带 CHECK / UNIQUE / INDEX）**：
   `valuation_metric_observations`（company_id + source_evidence_card_id +
   metric_code(16) + metric_as_of + source_value_text + metric_value
   NUMERIC(38,12) + schema_version + fingerprint CHAR(64) UNIQUE；CHECK
   metric_code ∈ pe_ttm/pb_mrq/ps_ttm、schema_version=1、source_value_text
   non-empty、fingerprint 64-hex）、`relative_valuation_comparisons`
   （target_company_id + target_observation_id + metric_code + metric_as_of +
   analysis_as_of + comparison_method(32) + peer_count + peer_median/min/max +
   premium_discount_to_median NUMERIC(38,12) + comparison_schema_version +
   formula_version + fingerprint CHAR(64) UNIQUE；CHECK metric_code 白名单 /
   method ∈ peer_median / peer_count 3..20 / schema & formula >= 1 /
   analysis_as_of >= metric_as_of / fingerprint 64-hex）、
   `relative_valuation_comparison_peers`（comparison_id FK +
   peer_company_id + peer_observation_id；PK (comparison_id,
   peer_observation_id)）。downgrade guard：**任一表有行 → 拒绝回滚**
   （`RuntimeError`，alembic_version 保持 0026，不删数据）；三表全空 → 回滚
   0025 成功。

3. **Version boundary**：`VALUATION_OBSERVATION_SCHEMA_VERSION=1`、
   `RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION=1`、
   `VALUATION_FORMULA_VERSION=1`；comparison_method v1 = `peer_median`。
   **v1 metric_code 只有 `pe_ttm` / `pb_mrq` / `ps_ttm`**。两个 fingerprint 的
   payload 都含 schema version——升级 = 新指纹 = 新行，历史行原样保留（无
   update API）。

4. **Observation evidence policy（v1 严格）**：bind 真实 EvidenceCard；
   `evidence.company_id == draft.company_id`；`origin_type=document_chunk` 且
   `evidence_type=metric`；任何不匹配 → `ValuationObservationEvidenceMismatch`
   （缺失 / 跨公司 / 非 document / 非 metric 各自 code）。**source_value_text
   必须是 quote_text 中一个完整数字 token**：复用 Financial 同一 grammar 的
   `find_financial_number_tokens`；先 grammar parse（`parse_valuation_number`，
   非法字面量如 `"100万"` / `"abc"` / `"15.3%"` → `ValuationValueNotNumeric`），
   再 exact-match（0 个 → `ValuationValueNotFound`，>1 个 →
   `ValuationValueAmbiguous`）。禁止 substring partial match / fuzzy /
   normalize 后匹配 / 自动纠错：`"市盈率30倍"` 里 `"30"` 接受而 `"3"` / `"0"`
   拒绝；`"-123.45"` / `"(123.45)"` 的符号与括号属于 token。`metric_value`
   完全由 source_value_text 解析（`Decimal`，零 float），并通过
   `validate_valuation_decimal_storage`（复用 `fits_numeric_38_12`，小数位
   <= 12 且 abs < 10^26）→ 超界 `ValuationStorageRangeError`（禁止静默
   quantize / round / truncate）。**observation 允许 0 / 负倍数**（来源事实
   快照），可比较性由 comparison 校验。**producer 输入边界**：本 policy 的
   **deterministic 强验证**覆盖 Evidence/source provenance、company、
   document metric Evidence、exact numeric token、Decimal/storage bounds；
   `metric_code`（该数字是否 `pe_ttm` / `pb_mrq` / `ps_ttm`）与 `metric_as_of`
   （该数字的市场观测日）**当前来自 structured producer 语义输入**
   （`ValuationMetricDraft`），程序只确定性校验其取值范围（白名单 / date）与
   跨 observation 一致性，**不自动从 quote / 表格表头验证**"这个数字一定是
   PE_TTM / 这个日期一定是 metric_as_of"。automatic semantic extraction 是
   **未来** official market-data provider / deterministic table extractor 的
   职责（仍是确定性路径）；本阶段不实现中文 PE/PB/PS label NLP parser，也不把
   本阶段表述为"所有估值语义已从原文自动验证"。

5. **Peer 规则（显式 peer，程序不自动选）**：调用方传 `peer_observation_ids`
   （3..20，构造时校验去重、target 不在 peer 内）。Service 从真实 Observation
   确定性校验：peer 观察缺失 → `ValuationObservationNotFound`；target /
   peer 公司缺失 → `ValuationCompanyNotFound`；target observation 公司 !=
   draft.target_company_id → `ValuationCompanyMismatch`；peer 集合内**公司重复**
   → `ValuationPeerDuplicateError`（公司去重以真实 observation 的 company_id
   为准，不自动过滤）；peer 含 target 公司 → `ValuationPeerIncludesTargetError`。
   **不做**自动选 peer / LLM 选 peer。

6. **可比较性（同一集合全部同一指标 / 同一市场观测日）**：任何 peer 的
   metric_code != target → `ValuationMetricMismatch`；任何 metric_as_of !=
   target → `ValuationDateMismatch`（**严格 same-date，不就近交易日对齐**）；
   任一 metric_value <= 0 → `ValuationMetricNotComparable`（0 / 负倍数可存储
   但不可比较）。

7. **no-lookahead（spec S）**：`analysis_as_of >= metric_as_of`（否则
   `ValuationFutureEvidence`）；每个来源 availability <= analysis_as_of。
   availability 复用 `app/claims/macro_policy.py` 的 `resolve_availability`
   （document 用 `SourceRecord.published_at` 否则 `acquired_at`，**绝不用
   reporting_period_end**）；任何 future → `ValuationFutureEvidence`。

8. **确定性统计（纯函数，全程 Decimal）**：`compute_peer_median`（排序后
   奇数 → 中间值；偶数 → 两中位算术平均，`Decimal` 精确，非 statistics.median
   + float 混合）；`compute_comparison_stats` →
   peer_median / peer_min / peer_max / premium_discount_to_median =
   `(target - median) / median`。`CALCULATION_SCALE=12`、
   `_QUANTUM=Decimal("0.000000000001")`、显式局部 `ROUND_HALF_EVEN` 上下文；
   中位数总是有限小数（两个有限小数平均后仍是有限小数），ROUND_HALF_EVEN
   量化只作用于 premium 除法（如 -0.2/15.5 = -0.0129032258064516… → 12 位
   确定性舍入）。comparison_method 恒为 `peer_median`，无 LLM / 无分类。

9. **Fingerprint + replay**：canonical JSON（sort_keys + 固定 separators +
   UTF-8）+ SHA-256。observation fingerprint 至少覆盖 schema version /
   company / evidence card / metric_code / metric_as_of / source_value_text /
   metric_value；comparison fingerprint 至少覆盖 comparison schema version /
   formula version / method / target company / target observation /
   **target observation fingerprint** / metric / metric_as_of / analysis_as_of /
   peer list（**按 peer_company_id 排序**，每条含 peer_company_id /
   peer_observation_id / observation fingerprint）/ peer_median / min / max /
   premium。**均不含 comparison_id / created_at**。同一完全相同 → replay 同
   一行；任一输入变化 → 新指纹 → 新行，旧行保留。`create_or_get`（ON
   CONFLICT(fingerprint) DO NOTHING RETURNING，**无进程锁**）；replay 重新加载
   真实 DB 并逐项核实，发现损坏 → `ValuationIntegrityError`，**不自动 repair**
   （修改 = 新行，无 update API）。任何 SQLAlchemyError → 整批 rollback +
   `ValuationPersistenceFailed`（0 partial write）；并发 → 最终 1 行 + 1 套完整
   peer links。

10. **两阶段提交**：短 DB session 加载 + 校验（连接即刻关闭）→ 纯函数派生 →
    短 DB transaction create_or_get + replay 核实；Service 只持有 sessionmaker，
    不直接写 DB / 不持有连接。

## 后果

- **4C.2A = completed**：Valuation 证据链前两环确定性地接通；4C.1B 后续的
  Relative Valuation Claim（4C.2B）可直接基于 Observation + Comparison 数值做
  判断，无需再次触碰原始文档。
- **版本边界收口**：v1 schema / v1 formula / peer_median 冻结；升级 = 新指纹
  = 新行；历史数据不批量改写、replay 不误判损坏。
- **错误语义正交且全部可达**：`NotNumeric`（grammar）→ `NotFound` /
  `Ambiguous`（quote token）→ storage / peer / metric / date / future /
  integrity / persistence 各归各的稳定 `code`，供 4C.2B / Audit 稳定处理。
- **Alembic head = 0026**（Stage 4C 当前最新）。
- **0 LLM / 0 Chroma query / 0 Retrieval / 0 LangGraph / 0 Claim / 0 Report /
  0 Audit / 0 自动交易 / 0 目标价**（automated tests 全程不调真实 LLM）。

## 明确不做（边界）

不实现 4C.2B Relative Valuation Claim（**不开始 4C.2B**）；不做估值分类 /
绝对公允价值 / target price / DCF / PEG / EV / EBITDA / FCFF / FCFE /
dividend model；不自动选 peer / 不 LLM 选 peer；不做 PEG / growth-adjusted
比较；不修改 0023 / 0024 / 0025；不批量 update 历史 rows；不反推历史 cutoff；
不接 LangGraph 分析节点；不开放 HTTP API；不创建 Claim / Report /
DraftSection / ReviewIssue / Audit；不记录 API key / 完整 prompt /
reasoning_content / raw provider response；不实现自动交易 / 技术分析 / 短期
预测 / 买卖建议。
