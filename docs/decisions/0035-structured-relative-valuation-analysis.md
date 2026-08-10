# ADR-0035：Structured Relative Valuation Analysis（阶段 4C.2B.2）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 背景

4C.2B.1（ADR-0034）建立了 Relative Valuation Claim 的 provenance：Claim →
ClaimRelativeValuationComparisonLink → RelativeValuationComparison →
ValuationMetricObservation → EvidenceCard → Source 完整可重算证据链。
4C.2B.2 把这条链**接上 LLM 分析判断**，形成 **Structured Relative Valuation
Analyst**：调用方提供 `RelativeValuationComparison[] + research question +
analysis_as_of`，模型只给出方向性 assessment / confidence / importance 与
comparison relations，程序**确定性**完成 V alias（V1..Vn）、ref resolution、
no-cherry-picking 覆盖、direction consistency、statement 渲染与 v7 Claim 持久化。

**角色边界（Analyst 只做判断，确定性交给代码）**：
- Analyst 负责：relevant 判断、assessment（relative_high / broadly_in_line /
  relative_low / mixed / uncertain）、confidence / importance、每条 comparison
  归入 supports / contradicts / context（V 编号引用）；
- 确定性代码负责：V1..Vn alias（按 metric_code pe_ttm/pb_mrq/ps_ttm 排序）、
  ref resolution（V → comparison_id）、未知引用 / 跨 relation / 遗漏 input
  comparison 拒绝、direction consistency（无 hidden thresholds）、uncertain
  importance policy、`render_valuation_claim_statement(assessment, metric_codes)`
  确定性渲染（v2）、
  v7 Claim 创建 / replay；
- **LLM 不**：计算 median / premium / percent、选择 peers、生成任何数值、
  生成 Claim statement、生成 target price / fair value / 交易建议 / 买入卖出评级。

一个 request **最多生成 1 个 Valuation Claim**（一个明确 peer universe +
同一日期 + PE/PB/PS 综合成一个 relative valuation assessment）。

## 决策

1. **Gate 0（spec A，稳定 public comparison integrity contract）**：审计并
   稳定 `RelativeValuationComparisonService.verify_comparison_integrity(...)`
   的错误边界——comparison missing → 返回 `None`；comparison **存在但**任何
   内部 provenance / peer links / formula / stats / fingerprint 损坏 →
   `ValuationIntegrityError`，**不泄漏** `ValuationInputError` 等普通 input
   error；保留 `raise ... from exc`。**不改变** `create_comparison(new draft)`
   正常用户输入错误的 taxonomy（只修 replay/integrity API）。由
   `tests/integration/test_valuation_comparison_service.py` 4 个新测试验证：
   missing → None / verified 返回 / 删 peer link → IntegrityError / target 混入
   peer links → IntegrityError。

2. **无新 migration（Alembic head 保持 0027）**：4C.2B.2 复用 4C.2B.1 的 v7
   Claim schema（`VALUATION_CLAIM_SCHEMA_VERSION=7` /
   `VALUATION_CLAIM_PROFILE_SCHEMA_VERSION=1`）与既有
   `relative_valuation_claim_profiles` 表。本阶段不建新表、不改既有 schema。

3. **`ValuationAnalysisRequest`（spec C）**：company_id + research_question
   （trim 非空）+ analysis_as_of（**必填 date**，与全部 comparison.
   analysis_as_of 完全一致）+ comparison_ids（**1..3**，去重 + canonical sort）。
   **不接 additional Evidence**（v1 Analyst 只做纯相对估值判断；成长性 / 盈利
   质量 / 业务背景留给 4D Claim Synthesis，防止 Valuation Agent 再次发明基本
   面事实）。

4. **`ValuationAnalysisDecision`（spec G，结构化输出）**：Pydantic frozen +
   model_validator。relevant=false → assessment / confidence / importance 全
   None、全部 refs 空、reason_code 可选（not_relevant / insufficient_comparisons /
   insufficient_consistency）；relevant=true → 三者**必填**、support refs >= 1、
   reason_code=None。全部 V ref 必须是 `V<number>` 格式；同 relation 组内不
   允许重复。违反 → ValidationError → 服务层翻译为 `MalformedOutput`。

5. **V alias Pack（spec E/F，确定性）**：`build_valuation_comparison_pack` 把
   已通过 integrity 校验的 comparison 投影为最小模型输入。V1..Vn **按
   metric_code 排序**（`_VALUATION_METRIC_ORDER`：pe_ttm → pb_mrq → ps_ttm，
   只存在则编号），同 comparison 集合 → 相同映射（可复现）。每个 V item 只含
   valuation_ref / metric_code / target_value / peer_median / peer_min /
   peer_max / premium_discount_to_median / position_vs_median（程序判定：
   premium>0→above、<0→below、==0→equal）/ peer_count / metric_as_of /
   analysis_as_of / comparison_method / formula_version /
   deterministic_display_premium（程序生成，如 `+50.00%`）。**不发送** comparison
   UUID / observation UUID / Evidence UUID / fingerprint / RawArtifact /
   locator / Chroma metadata。

6. **`_decimal_str` 数值规范化**：DB `numeric(14,12)` 读出的 Decimal 会带 12 位
   尾零（如 `Decimal("15.300000000000")`），直接 `str()` 会把尾零带进模型输入。
   `_decimal_str` 用 `format(value, "f")` + 去尾零，保证同一数值在不同来源
   （单测直接构造 vs DB 读出）渲染一致，且不丢有效位、不落科学计数法。这是真实
   bug（单测与集成渲染不一致），不是测试问题。

7. **Ref resolution（spec G，0 写失败）**：`resolve_decision_refs` 把 V refs
   解析为 comparison_id。未知 ref（不在 pack）→ `UnknownRef`；同一 V ref 跨
   relation 重复 → `RelationConflict`；**no-cherry-picking 硬边界**：relevant=true
   时 support ∪ contradict ∪ context 必须**恰好等于** request 全部 comparison
   aliases，遗漏任一 input → `ComparisonOmitted`。组内去重 + canonical 排序
   （与 ValuationClaimDraft normalization 一致）。不 fuzzy resolve、不自动猜 UUID。

8. **Direction / uncertain 共享策略（spec I，单份实现）**：跨 comparison 一致性
   （analysis_as_of / metric_as_of / peer set / metric 唯一性 / 数量上限）与
   direction / uncertain-importance 策略集中放在
   `app/valuation/claim_policy.py`（**纯函数**，`ValuationClaimPolicyReason`
   8 个稳定 reason），由 ValuationClaimService（4C.2B.1）与
   ValuationAnalysisService（4C.2B.2）**共用**，禁止复制两套规则。方向策略
   **无 hidden thresholds**：relative_high 要求全部 support premium > 0、
   relative_low 要求全部 < 0（否则 `DirectionConflict`）；mixed 要求 support
   中正负都有（否则 `MixedEvidenceInsufficient`）；broadly_in_line **不设
   threshold**（属于 Analyst judgement）；uncertain 不做方向 threshold 但
   importance 必须 normal（否则 `UncertainImportancePolicy`）。contradict /
   context 允许任意 sign（反证 / 背景）。

9. **确定性 statement renderer（spec C）**：`render_valuation_claim_statement
   (assessment, metric_codes)`（v2）用**冻结中文映射**渲染最终 Claim statement。
   `metric_codes` 来自**实际 selected verified Comparisons**（supports ∪
   contradicts ∪ context 的 metric_code，**不是模型输出**），按 metric 数量区分
   scope：single PE/PB/PS → "基于市盈率/市净率/市销率比较……"；multi → "基于所选
   估值指标综合比较……"（mixed="不同估值指标对公司的相对估值判断存在分化。"、
   uncertain="现有估值指标比较不足以形成明确的方向性判断。"）。single metric
   不可能合法 mixed（mixed policy 要求 support 正负方向都有）→ 稳定 policy
   error。**LLM 不生成 statement**；statement 不含任何数字 / 百分比 / threshold，
   也不带 company / peer 名称插值（避免把模型输出或未审计文本引入 Claim）。未知
   assessment / metric_code → `ValuationClaimDraftError`。`VALUATION_ANALYST_
   VERSION=2`（v1=historical pre-final，无 metric-scope 区分；历史 v1 Claim
   **不修改 / 不 backfill**）。

10. **Model boundary（spec H）**：`ValuationAnalysisModel` Protocol 放独立
    `model.py`（避免 packs↔contracts 循环导入）；实现契约 = `model_id` 稳定
    identifier（provider:model，不伪造 revision）+ `analyze(context, pack)` →
    ValuationAnalysisDecision，provider 失败 → `ModelUnavailable`、输出无法解析
    → `MalformedOutput`，不得启用 tools / web search / function side effects。

11. **生产适配器（spec H）**：`DeepSeekValuationAnalysisModel` 懒加载
    `ChatDeepSeek`（`model_id = {provider}:{model}` =
    `deepseek:deepseek-v4-flash`），temperature=0 + **显式关闭 thinking**
    （`extra_body={"thinking": {"type": "disabled"}}`——DeepSeek V4 Flash 默认
    开启 thinking，temperature=0 不等于关闭，thinking 非标准 OpenAI 参数经
    extra_body 传递，且不产生 `reasoning_content`）+ `with_structured_output`
    （不绑定 tools / 不开 web search）。`OutputParserException` →
    `MalformedOutput`、其余异常 → `ModelUnavailable`。自动测试一律用
    `FakeValuationAnalysisModel`，真实调用只用于受控 smoke。

12. **Prompt 契约（spec B）**：system prompt 冻结（`VALUATION_ANALYSIS_SYSTEM_
    PROMPT`，安全边界 / 计算边界 / 判断边界 / 输出约束），不含任何 Comparison
    内容；Comparison 是程序已计算、已校验的 trusted derived data，用
    `<<<COMPARISON_DATA_START/END>>>` delimiter 包装进 **user** payload
    （绝不拼接进 system），模型把定界符内内容当作 DATA 而非指令。传给模型的最小
    上下文 = research_question + analysis_as_of + strategy + comparison pack。

13. **错误分类（spec J）**：`app/analysis/valuation/errors.py` 14 个稳定错误类
    （ValuationAnalysisError 基类 + InputError / ComparisonNotFound /
    ComparisonCompanyMismatch / ComparisonCorrupted / InputInvalid /
    MalformedOutput / ModelUnavailable / UnknownRef / RelationConflict /
    ComparisonOmitted / DirectionConflict / MixedEvidenceInsufficient /
    UncertainImportancePolicy / ClaimDraftError），全部带 `ValuationAnalysis`
    前缀。错误消息不包含 evidence 正文、provider raw response、API key、完整
    prompt、DB URL、raw content、UUID alias 映射。`_policy_to_analysis_error`
    把 shared policy reason 映射到 analysis 错误域。

14. **Service 流程（spec K，两步提交镜像 FinancialAnalysisService）**：
    `ValuationAnalysisService.analyze`：防御性 request 校验 → 短 DB session
    加载全部 Comparison 并逐条 replay 校验（`verify_comparison_integrity`：
    缺失 → ComparisonNotFound、跨公司 → CompanyMismatch、损坏 →
    ComparisonCorrupted，**不调用 LLM**）→ 复用 `check_comparison_set_consistency`
    跨 comparison 一致性（失败 → InputInvalid）→ **关闭 DB session（LLM 调用
    期间不持有 DB connection / transaction）** → 构造 Pack → 调模型 → 
    relevant=false → 0 写 → ref resolution → direction / uncertain 策略 →
    确定性 statement 渲染 + `ValuationClaimDraft(v7)` → 
    `ValuationClaimService.create_claim` 原子登记。任一失败 → **整次 0 写**。

15. **Claim / Profile / Comparison provenance 复用（spec L）**：分析产物复用
    4C.2B.1 的 v7 Claim schema 与持久化链。Claim 固定
    analysis_domain=valuation、claim_kind=relative_valuation；analyst 身份 =
    `VALUATION_ANALYST_NAME = "structured_relative_valuation_analyst"` /
    `VALUATION_ANALYST_VERSION=2`（v1=historical pre-final，无 metric-scope
    区分；历史 v1 Claim 不修改 / 不 backfill）；`analyst_model_id = model.model_id`（真实适配器 =
    deepseek:deepseek-v4-flash）；Profile（assessment / analysis_as_of /
    profile_schema_version=1）；Comparison links（supports / contradicts /
    context）；自动 context Evidence links（target + 全部 peers 的 source
    Evidence，relation=context）。**LLM 不生成 statement / 不选择 peers / 不
    计算任何数值**。

16. **Replay（spec R）**：同一决策（同 fingerprint）再次分析 →
    `ValuationClaimService.create_claim` 的 ON CONFLICT(claim_fingerprint)
    replay 语义 → 返回 replayed=True + 同一 claim_id，**无重复行**。修改 =
    新 Claim（无 update API）。

17. **测试（spec W）**：
    - **46 项单元**（`tests/analysis/valuation/`：test_packs 13 /
      test_contracts 23 / test_prompt 10，零 DB + fake）；
    - **18 项集成**（`tests/integration/test_valuation_analysis_service.py`，
      真实 PG + 真实服务链）：happy path v7 claim（analyst 身份 / profile /
      comp links / 4 context evidence links）、broadly_in_line no-threshold、
      最小 pack 投影、V alias 排序（pe→V1、pb→V2，提交顺序无关）、
      relevant=false 0 写、comparison missing / company-mismatch / corrupted
      （**不调用 LLM**，fake.calls==[]）、unknown ref / cross-relation /
      omitted / direction conflict / mixed insufficient / uncertain
      importance / malformed output / model unavailable（0 写）、replay；
    - **Gate 0 4 项**（`tests/integration/test_valuation_comparison_service.py`）。
    全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表。

18. **Docs（spec X）**：本 ADR；README + stage-4-plan 状态更新（4C.1=FINAL、
    4C.2A=completed、4C.2B.1=completed、4C.2B.2=completed、4D=later）。

## Runtime acceptance（spec A closeout，2026-08-10）

4C final acceptance 的 runtime 部分已完成并如实记录（本阶段**不运行新的真实
LLM smoke**）：

- **Windows runtime**：在 HEAD（含本 ADR 对应代码）上运行
  `python -m app.cli.run_backend`，live / ready 各 **5×200**；ready 五项
  checks（configuration / database / chroma / checkpoint / raw_storage）
  全部 ok。停止 host backend。
- **Docker rebuild**：`docker compose up -d --build backend` 重建当前代码，
  容器 healthy，live / ready 各 5×200、五项 checks ok；从 Docker runtime
  **实际读取** `alembic_version=0027` 与三张 valuation 表
  （relative_valuation_comparisons /
  claim_relative_valuation_comparison_links / relative_valuation_claim_profiles），
  表存在且可读。
- **版本边界（spec D）**：`VALUATION_ANALYST_VERSION=2`（v1=historical
  pre-final，无 metric-scope 区分；历史 v1 Claim **不修改 / 不 backfill**）；
  v2 = current statement-scope-safe version。Claim schema 仍 v7、无新 migration。

## 受控 smoke（spec 允许一次 controlled DeepSeek V4 Flash smoke）

`app/cli/smoke_valuation_analysis.py`（exit 0 通过）：seed 真实 HTML 链 →
target PE=30 / peers 18·20·22 → RelativeValuationComparison（premium +50% →
`_EXPECTED_ASSESSMENT=relative_high`）→ `ValuationAnalysisService.analyze` 走
生产适配器 `DeepSeekValuationAnalysisModel` → 校验 schema / direction policy /
statement 渲染 / fingerprint replay → 打印摘要（provider / model / latency_ms /
relevant / claim_id / replayed / assessment / deterministic_statement /
assessment_matches_expected / cleanup_success）→ **清理全部 seed 数据并实际查询
0 残留**（`_cleanup` / `_residual_counts` 只删 scratch companies；UUID 直接绑定
对象，psycopg 原生支持）。

实跑情况（透明说明）：**2 次真实 DeepSeek 调用**——第一次完整证明 pipeline
（relative_high、v7 claim 落库、cleanup 0 残留）但最后因打印 bug（
`claim.analysis_as_of` 不存在于 claim model）退出码 1；移除该 print 行后**重跑
确认 exit 0**。第二次为同一次受控 smoke 的调试重跑，非新功能验证。provider=
deepseek、model=`deepseek:deepseek-v4-flash`、assessment=relative_high、
assessment_matches_expected=true、cleanup_success=true、0 残留。

## 边界

- **LLM 不**计算 median / premium / percent、不选择 peers、不生成任何数值、
  不生成 Claim statement、不生成 target price / fair value / 交易建议 /
  买入卖出评级 / 短期预测。
- **不做** 4D Claim Synthesis / LangGraph integration / Report / Audit /
  HTTP API / Chroma / Retrieval / DCF / 绝对公允价值。
- 一个 request 最多 1 个 Valuation Claim；不接 additional Evidence。
- 不创建 Report / DraftSection / ReviewIssue / Audit；不接 LangGraph 分析
  节点；不开放 HTTP API；不修改 generic / Financial / Macro 既有 schema。
- 不提交 `.env` / API key / 完整 prompt / raw provider response。
- **不把 `RelativeValuationComparison` 伪装成 EvidenceCard**（保持分层）。
