# ADR-0027：Deterministic Financial Calculation（阶段 4B.2B）

- 状态：已接受
- 日期：2026-08-09
- 决策人：InsightForge 项目

## 决策

1. **4B.2B 状态：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Calculation HTTP 端点）**。4B.2B 是财务分析链条（4B.2A Observation → 4B.2B Deterministic Calculation → 4B.2C Financial Analyst）的**确定性计算环**：把已登记的 `FinancialMetricObservation` 通过**冻结公式**计算为派生财务事实（同比 / 环比 / margin / ratio），形成 **Calculation → Observation → EvidenceCard → Source** 证据链。本阶段 **0 LLM / 0 Chroma / 0 Analyst / 0 Claim / 0 Report / 0 Audit / 0 LangGraph analysis node**；**不开放 API、不接 LangGraph 顶层编排**（`financial_calculations` / `financial_calculation_inputs` 允许存在；`report_outlines` / `report_sections` / `reports` / `review_issues` 不得存在）。

2. **Migration 0021（`financial_calculations` + `financial_calculation_inputs` 表）**：
   - `financial_calculations`：calculation_id（UUID PK）、company_id（FK companies RESTRICT）、calculation_code、result_value（NUMERIC(38,12)）、result_unit（cny / ratio，ratio 存 0.1234 而非 12.34）、calculation_schema_version、formula_version、calculation_fingerprint（CHAR(64) UNIQUE）、created_at。5 个 CHECK（calculation_code 白名单 7 个 / result_unit 枚举 / fingerprint `^[0-9a-f]{64}$` / schema ≥ 1 / formula ≥ 1 / code 非空，实际 6 个）+ 2 索引（company_id / calculation_code）。
   - `financial_calculation_inputs`：calculation_id（FK financial_calculations CASCADE）、input_role、metric_observation_id（FK financial_metric_observations RESTRICT）；PK(calculation_id, input_role) 名 `pk_financial_calculation_inputs`；CHECK（input_role 白名单 8 个 + 非空）；索引 metric_observation_id。
   - **downgrade guard**：两表任一有行时拒绝回滚（删除会静默丢弃已计算的派生事实），无数据时允许回到 0020（isolated 临时 PG 验证两条路径）。

3. **冻结契约（`app/financial/calculations/contracts.py`）**：`FINANCIAL_CALCULATION_SCHEMA_VERSION = 1`、`FORMULA_VERSION = 1`；**7 个 calculation_code**（absolute_change_cny / yoy_growth_rate / qoq_growth_rate / gross_margin / operating_margin / net_margin_parent / debt_to_assets_ratio）；`CalculationResultUnit`（cny / ratio）；`InputRole` 8 个（current / baseline / revenue / operating_cost / operating_profit / net_profit_parent / total_assets / total_liabilities）。**deterministic mapping**：`calculation_input_roles(code)`（每个 code → 固定 role 集合）、`expected_metric_code(role)`（fixed role → 期望 metric_code；current / baseline → None，由输入一致决定）、`calculation_result_unit(code)`。

4. **`FinancialCalculationDraft` 只允许语义输入**：company_id / calculation_code / input_observation_ids（每个 input_role **恰好**一个已登记 Observation 的 ID）。构造时校验 role 集合必须与 code 的 `calculation_input_roles` **完全一致**（不多不少）、company / obs_id 必须是 UUID（bool 拒绝）。调用方**不得提供** result_value / result_unit / formula / Evidence ID / source ID / period metadata / fingerprint——全部由 Service 从已登记 Observation **确定性派生**。draft 没有 result_value / normalized_value 字段（不存在"手工传结果"通道）。

5. **Comparability 规则（I）**（`_validate_comparability`，不自动纠错）：每个 Observation 的 `company_id` 必须 == draft.company_id（否则 `FinancialCalculationCompanyMismatch`）；全部输入 `statement_scope` 必须完全相同（`FinancialCalculationScopeMismatch`）；growth 类（absolute / YoY / QoQ）要求 current 与 baseline 的 `metric_code` 相同（`FinancialCalculationInputMismatch`），fixed-role 类要求每个 role 的 `metric_code` 精确等于 `expected_metric_code`（`FinancialCalculationInputMismatch`）。

6. **Period 规则（H-K，确定性）**：
   - **absolute_change**：current 与 baseline 的 `period_kind` 必须相同（duration / instant），不做其它 period 校验。
   - **YoY**：baseline 年份 = current 年份 - 1，且月/日对应（`_same_month_day`：duration 同时要求 period_start 月/日对应；instant 两者 period_start 均为 None）。
   - **QoQ**：duration 必须是**标准单季度**（period_start 为季首日 01/04/07/10 的 1 号、period_end 为同一季末 03-31 / 06-30 / 09-30 / 12-31）；instant 的 period_end 必须是 03-31 / 06-30 / 09-30 / 12-31 且 period_start 为 None。两者都必须**连续季度**（`_quarter_index = year*4 + quarter`，baseline = current - 1，跨年正确）。
   - margin / ratio 类**无 period 要求**。
   - 任一违反 → `FinancialCalculationPeriodMismatch`。

7. **公式（L-M，`app/financial/calculations/formulas.py`，全程 Decimal、禁止 float）**：
   - `absolute_change_cny` = current - baseline（精确减法，输入已是 NUMERIC(38,12) 值，结果小数位 ≤ 12，无需 quantize）；
   - `growth_rate` = (current - baseline) / baseline，**baseline 必须 > 0**（`FinancialCalculationGrowthBaseNotPositive`）；
   - `gross_margin` = (revenue - operating_cost) / revenue、`operating_margin` = operating_profit / revenue、`net_margin_parent` = net_profit_parent / revenue、`debt_to_assets_ratio` = total_liabilities / total_assets；四个 ratio 的**分母必须 > 0**（`FinancialCalculationZeroDenominator`）。
   - **除法统一 quantize 到 `CALCULATION_SCALE = 12` 位、`ROUND_HALF_EVEN`**（`quantize(Decimal("0.000000000001"))`，银行家舍入——1/3 → 0.333333333333、(4-3)/3 → 0.333333333333）。
   - **ratio 结果存小数形式（0.1234），不存 12.34**（result_unit = ratio）。

8. **Storage contract（N 前置）**：`result_value` 落库前必须 `fits_numeric_38_12`（小数位 ≤ 12 且 abs < 10^26），不满足 → `FinancialCalculationStorageRangeError`（**禁止静默 quantize / round / truncate**，不让 PG 自动 rounding / overflow）。复用 `app/financial/number_parser.py` 的 bool 判断（metric 侧抛 `FinancialMetricStorageRangeError`，calculation 侧复用同一 bool + 自有错误类）。

9. **Fingerprint（N）**：`compute_calculation_fingerprint` = canonical JSON（sort_keys + 固定 separators + ensure_ascii=False + UTF-8）+ SHA-256，含 calculation_schema_version / formula_version / company_id / calculation_code / **按 input_role 排序的 (role, observation_id, observation fingerprint)** / result_value（str()，canonical decimal string）/ result_unit；**不含 calculation_id / created_at**。同一完全相同输入 → 同一指纹 → replay 同一行；**输入任一变化（含上游 Observation 指纹变化）→ 新指纹 → 新行，旧行保留**（**无 update API**）。

10. **Replay（O，不 repair）**：已有 fingerprint 时，`_verify_replay` **重新加载 Observation + 重新派生**（检测上游 Observation 被篡改导致结果不再有效），再逐项核实 persisted calculation 的 company / code / result_value / result_unit / schema_version / formula_version / fingerprint 与其 inputs 绑定（`input_role → metric_observation_id`）与 draft 完全一致；任一损坏 → `FinancialCalculationIntegrityError`，**不自动 repair**（修改 = 新 calculation）。

11. **Persistence（P）**：`FinancialCalculationService.create_calculation(draft)` 三步（镜像 FinancialMetricService）：
    - **短 DB session**：按 role 加载每个 Observation（缺失 → `FinancialCalculationObservationNotFound`），连接即刻关闭（纯函数阶段不持 DB 连接）；
    - **纯函数派生**（无 DB）：comparability → period → 公式 → storage bounds → fingerprint；
    - **短 DB transaction**：`FinancialCalculationRepository.create_or_get`（PG `ON CONFLICT(calculation_fingerprint) DO NOTHING RETURNING`，**无进程锁**）→ created=True 时插入 inputs（`FinancialCalculationInputRepository.insert_inputs`）；created=False 时 `_verify_replay`；`FinancialCalculationIntegrityError → 显式 rollback + raise`；任一 `SQLAlchemyError → 整条 rollback + FinancialCalculationPersistenceFailed`（**0 partial write**）。**并发 → 最终 1 calculation**。

12. **Service / Repository 结构**：构造函数**只持有 sessionmaker**（不持 DB 连接 / LLM / chroma 客户端）。`app/financial/calculations/service.py` + `contracts.py` + `formulas.py` + `errors.py`；repositories 放 `app/repositories/`（`financial_calculation_repository.py` / `financial_calculation_input_repository.py`，与 metric observation repository 同层）；`financial_metric_observation_repository` 新增 `get_by_id`（按 metric_observation_id 查询，供按 role 加载）。

13. **错误分类（`errors.py`，11 个 + 稳定 code）**：`FinancialCalculationError`（基类）+ InputError / ObservationNotFound / CompanyMismatch / ScopeMismatch / InputMismatch / PeriodMismatch / GrowthBaseNotPositive / ZeroDenominator / StorageRangeError / IntegrityError / PersistenceFailed。错误消息不包含 observation 正文 / evidence 正文 / prompt / API key / DB URL / raw content。

14. **测试（97 项新增）**：**78 项单元**（`tests/financial/calculations/test_formulas.py` 23：算术 / quantize 12 位 / ROUND_HALF_EVEN（half-even down/up）/ ratio 存 0.2 不存 20 / baseline>0 / 分母>0 / dispatch；`test_contracts.py` 32：版本冻结 / 7 codes / roles 映射 / draft 只允许语义输入（缺 role / 多 role / 非 UUID / bool / 非 dict）/ fingerprint 确定性·排序无关·敏感性·不含 id；`test_service_pure.py` 23：absolute / YoY / QoQ 各种 period、comparability、storage range、error paths，零 DB）+ **17 项集成**（`tests/integration/test_financial_calculation_service.py`，真实 PG + 真实服务链 seed company/evidence/observation，零 Chroma/LLM：absolute_change_cny、yoy ratio、gross_margin、inputs 绑定、replay 同 draft 返回同一行、输入变化新建旧行保留、并发 → 1 行、篡改 result_value → IntegrityError 不 repair、ObservationNotFound / CompanyMismatch / ScopeMismatch / InputMismatch / YoY period mismatch / GrowthBaseNotPositive / ZeroDenominator / StorageRangeError、删除 calculation CASCADE inputs）+ **2 项 migration 0021 downgrade guard**（isolated 临时 PG：无数据降级成功、有 observation + calculation + input 数据降级拒绝且行保留）。**0 LLM / 0 Chroma query / 0 Claim / 0 Report 表 / 0 LangGraph**。

15. **文档边界（Q）**：stage-4-plan.md / README 统一为 **4B.2B completed**、**4B.2C next = Financial Analyst**、4C later=Macro Context / Valuation、4D later=Claim Synthesis / Conflict / Evidence Gap、Stage 5=Report + Audit（不提前标记）。Alembic head = 0021。

16. **当前保证 / producer 输入边界**：已实现 = 确定性公式（Decimal / quantize / ROUND_HALF_EVEN）、comparability（company / scope / metric_code）、period 可比规则（absolute / YoY / QoQ）、storage bounds、fingerprint / replay（不 repair）、并发幂等（ON CONFLICT，无进程锁）、migration 0021 downgrade guard。**尚未实现** = 从年报自动识别 / 验证 Observation 的 `metric_code` / `statement_scope` / `period` / `raw_unit`（该边界属于 4B.2A producer 输入，4B.2B 只消费已登记的 Observation）；不实现 4B.2C Financial Analyst（LLM 解释数值）。

## 后果

- **派生事实可追溯、可回放**：每个 Calculation 绑定到已登记的 Observation（→ EvidenceCard → Source），replay 校验把"持久化结果"与"从 Observation 重新派生"逐字段比对，损坏即报 IntegrityError，不静默修复。
- **LLM 不碰数值结果**：draft 只允许语义输入（company / code / observation ids），result_value / result_unit / fingerprint 全由确定性公式从 Observation 派生——4B.2C Financial Analyst 只能解释这些已计算的确定性数值，不能自行计算 / 改写数值。
- **确定性 period 语义**：YoY 要求月/日对应 + 年份相邻，QoQ 只允许标准单季度且连续——杜绝"任意两个 period 相减"产生语义漂移的伪增长率。
- **零 partial writes / 零 update API**：任一校验失败 → 0 写；修订 = 新 fingerprint + 新行，旧行保留（审计可回溯）。
- **并发幂等**：PG `ON CONFLICT DO NOTHING`（无进程锁），重复 / 并发 create_calculation 只产生一行。
- **DB 层双重防线**：code / unit / role 枚举、fingerprint 格式、schema / formula ≥ 1 全部下沉为 CHECK 约束，应用层校验只是第一道。

## 明确不做（边界）

不实现 Financial Analyst（4B.2C，LLM 解释数值）；不实现 Macro Analyst / Valuation Analyst（4C）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不生成 Report / DraftSection / ReviewIssue / Audit（Stage 5）；不接 LangGraph 分析节点；不调用 Retrieval / Chroma / BGE / LLM / RawArtifact bytes；不开放 HTTP API；不自动从 PDF / 年报猜或验证财务数字（只消费已登记 Observation）；**不开始 4B.2C**。
