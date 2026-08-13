# Stage 7B.1 — Evaluation Foundation

> 状态：**7B.1.0 契约 = FINAL**；**7B.1.1A Frozen Evaluation Bundle = FINAL**；
> **7B.1.1B PG→Frozen Snapshot Materializer = FINAL**；**7B.1.2A Deterministic Metrics
> Foundation = FINAL**；**7B.1.2B LLM Usage Instrumentation = FINAL**；
> **7B.1.2C Execution Runtime = FINAL**；**7B.1.3A Evaluation Execution Persistence
> = FINAL**；**7B.1.4A Frozen Runtime Replayability Gate = FINAL**；
> **7B.1.4B.1 Isolated Runtime Rehydration Foundation = FINAL**；
> **7B.1.4B.2 Macro Isolated Rehydration = FINAL**。
> 实现按 slice 逐块交付，每块小而完整。
> 范围：三路系统评估（single_rag / multi_stage_no_audit / insightforge_full）的
> 数据集契约、frozen snapshot、typed human label、variant 契约、确定性指标、
> 持久化、offline CLI、fingerprint。
> 明确不在 7B.1 内：Web Eval UI、真实大规模模型跑分、Fault Injection（= 7C）。

## 0. 审计结论（2026-08-13）

仓库**尚无** eval / telemetry / token-usage 基础：

- 无 `eval/`、`evaluation/`、`metrics/`、`telemetry/` 包。
- 无 token usage / cost 捕获；LLM 调用分散在 10 个独立 `DeepSeek*Model` adapter
  （evidence/extractor、analysis/{financial,macro,synthesis,valuation,claims}、
  audit、revision、draft_section、research_planner），各自 `with_structured_output(...)
  .ainvoke()` 并**丢弃** `usage_metadata`。**无统一调用收口**（→ 效率维度依赖 7B.1.2
  捕获层；已于 7B.1.2B 解决，见 §3.13）。
- 无 LangSmith / Langfuse / OpenTelemetry；只有 structlog JSON 日志。
- 可复用基础：`run_checks`（10 个确定性 check，见 `app/report/checks.py`）、
  canonical JSON SHA-256 fingerprint（20+ 处，见 `app/macro/fingerprint.py`、
  `app/evidence/contracts.py`）、`ReportCheckResult` 持久化范式、
  `workflow_runs`/`workflow_events` 生命周期锚点、`Settings.llm_provider/llm_model`
  配置源。

## 1. 冻结原则（贯穿全部 slice）

1. **三路对照，不是三种评估通道**。同一 frozen case / snapshot / question /
   label，分别跑三条 pipeline，比较端到端输出。
2. **Execution 与 Scoring 分离**：execution spec 只表达「系统实际看到什么 + 以什么
   配置运行」，**不含** human label / metric registry / judge；scoring spec 才绑定
   label + registry + judge。HumanLabel **绝不**进入 variant execution input。
3. **frozen snapshot 是原始 source 的字节寻址 manifest（document / macro /
   structured），不是已生成的 EvidenceCard/Claim**。三条 pipeline 各自从同一 raw
   snapshot 做自己的提取；single_rag / multi_stage_no_audit **不**继承 Full 的
   最终 EvidenceCards。
4. **指标来源三分，禁止混淆**：`deterministic` / `human_labeled` / `semantic_judge` /
   `runtime`（MetricKind）；比较不跨来源混合。`human_labeled` 必须是 typed label，
   **不是** generic free-text LabelEntry。
5. **model config 逐 variant 冻结**，judge 模型与 subject 模型分开固定（防自评）。
6. 确定性指标优先；human label 只覆盖必须人判的（financial accuracy、risk topic
   recall、macro causal error、overclaim）。
7. 不新增依赖；不在日志/契约里泄露 key / raw response / 完整 prompt。

## 2. 包结构（`backend/app/eval/`）

```
app/eval/
  __init__.py
  errors.py       # EvalError 层次（稳定 code，不塞 payload/label/raw source）
  variants.py     # EvalVariantId（3 项，无 noop）+ COMPARABLE_VARIANTS
  metrics.py      # MetricDimension/Kind/Status + MetricName + MetricSpec + MetricValue
  contracts.py    # snapshot / case / dataset / label / config / spec / output / component
  fingerprints.py # 8 个 fingerprint 函数
  canonical.py    # canonical JSON（sort_keys / compact / ensure_ascii=False）
  bundle/         # 7B.1.1A：frozen bundle 的 layout / writer / loader / integrity
    layout.py     #   path 派生 + 路径 segment 守卫（拒绝 traversal）
    writer.py     #   atomic 写 + replay（语义一致 no-op）+ content-address blob
    loader.py     #   identity → path → 读取（label leakage boundary）
    integrity.py  #   12 步 referential integrity 校验
    _io.py        #   底层读写 + 稳定错误包装
  materialization/# 7B.1.1B：PG + RawArtifactStore 物化为 frozen snapshot（依赖详见 §3.11）
    service.py    #   document / macro / structured 三路 payload 投影
    projections.py#   payload bytes + sha256 + semantic fingerprint
  replay/         # 7B.1.4B.1：Frozen Bundle → 隔离 PG + store 的运行时复现（依赖详见 §3.17）
    contracts.py  #   replay_v1 确定性 policy 常量 + RehydratedCase/Document 结果
    rehydrator.py #   EvaluationReplayRehydrator（只依赖隔离 target sessionmaker + store + loader）
  scoring/        # 7B.1.2A：cross-variant deterministic metrics
    context.py    #   EvalScoringContext（output + snapshot + exec fp，无 label）
    deterministic.py # citation_validity / citation_coverage v1 + 结构校验
    registry.py   #   deterministic calculator 注册表 + 可用集合
  usage/          # 7B.1.2B：LLM 调用 usage 收集 + 聚合
    collector.py  #   EvalLlmUsageCollector（按 exec fp / variant / case 绑定）
    aggregation.py#   aggregate_llm_usage → 4 个 runtime metric
  execution/      # 7B.1.2C：ExecutionSpec → Trial → Attempt 执行运行时
    contracts.py  #   EvalTrialSpec / EvalExecutionAttempt / EvalExecutionAttemptResult
                  #     + ExecutionAttemptStatus + compute_trial_fingerprint
    runner.py     #   VariantRunner Protocol（label leakage boundary）
    harness.py    #   execute_variant_attempt（collector 注入 + 身份校验 + 收敛 success/failed）
  persistence/    # 7B.1.3A：ExecutionSpec → Trial → Attempt → LLM Call Usage 持久化
    contracts.py  #   Verified{Spec,Trial,Attempt}Record read models（verified 只读）
    service.py    #   create-or-get + persist_attempt_result + verify_*_integrity
```
`app/llm/`（通用层，不在 eval 包内）：
```
app/llm/
  instrumentation.py # invoke_structured_with_usage 统一包装 + LlmCallUsageRecord
  components.py      # COMPONENT_* 组件名常量 + INSTRUMENTED_LLM_COMPONENTS
```

## 3. 冻结契约（7B.1.0）

### 3.1 EvalVariantId（`variants.py`）

```python
class EvalVariantId(StrEnum):
    SINGLE_RAG = "single_rag"
    MULTI_STAGE_NO_AUDIT = "multi_stage_no_audit"
    INSIGHTFORGE_FULL = "insightforge_full"

COMPARABLE_VARIANTS: tuple[EvalVariantId, ...] = tuple(EvalVariantId)
```

`noop` / `test` / `mock` 不进入 `EvalVariantId`；未来 dev/test runner 用独立 identity。

### 3.2 Frozen Source Snapshot（`contracts.py`，覆盖 document/macro/structured）

- `FrozenDocumentSourceRef`：`source_record_id UUID` / `raw_artifact_id UUID` /
  `content_sha256 64hex` / `provider_key` / `document_type` / `media_type` /
  `title` / `source_url` / `acquired_at` / `authority_tier_snapshot 1..4` /
  `critical_claim_eligible_snapshot` / `published_at?` / `reporting_period_start?` /
  `reporting_period_end?`（`title`/`source_url`/`acquired_at`/
  `authority_tier_snapshot`/`critical_claim_eligible_snapshot` 为 7B.1.4A 补齐，
  见 §3.16）。
- `FrozenMacroSnapshotRef`：`snapshot_id UUID` / `series_id UUID` /
  `snapshot_fingerprint 64hex` / `payload_sha256 64hex` / `fetched_at` + **rehydration
  closure**（7B.1.4B.2 补齐）：`series: FrozenMacroSeriesRef`（六字段身份）/
  `snapshot: FrozenMacroSnapshotDetail`（行级语义字段）/ `observations` /
  `artifact_links` / `raw_artifacts`。closure 字段**不**进入
  `compute_source_snapshot_fingerprint`（保持 domain macro fingerprint 与 bundle 字节
  identity 分离）；`raw_artifacts` 与 `artifact_links` 必须按 `artifact_id` 1:1 闭合。
- `FrozenStructuredArtifactRef`：`artifact_type`（enum：`financial_metric_observation`
  / `relative_valuation_observation` / `relative_valuation_comparison`）/
  `artifact_id UUID` / `artifact_fingerprint 64hex`。
- `FrozenSourceSnapshot`：`snapshot_schema_version=2` + 四类 tuple（document /
  macro / structured / `source_providers`）；`frozen=True`；
  duplicate identity 构造时拒绝（doc 按 `content_sha256`、macro 按
  `snapshot_fingerprint`、structured 按 `(artifact_type, artifact_fingerprint)`、
  provider 按 `provider_key`）；
  UUID（`snapshot_id` / `artifact_id`）只是 provenance 指针，**不是** semantic
  identity，不参与去重；**不保存 raw bytes**。
- `FrozenSourceProviderRef`（7B.1.4A 新增）：`provider_key` / `display_name` /
  `enabled` / `capabilities tuple`（sorted）。只冻结 router 与 citation label
  运行期真正读取的 semantic 字段。

### 3.3 EvalCase（`contracts.py`）

`schema_version=2` / `case_id`（稳定 slug，拒绝 UUID-only）/ `case_version>=1` /
`company_id UUID` / `company FrozenCompanyIdentity` / `research_question`（strip
非空、bounded）/ `analysis_as_of datetime` / `tags` /
`source_snapshot_fingerprint 64hex` / `human_label_fingerprint 64hex?`。**不含**
WorkflowRun id / orchestration id / created_at / 当前 DB execution status。
`FrozenCompanyIdentity`（7B.1.4A 新增）：`security_code` / `official_name` /
`short_name?` / `exchange` / `board` / `aliases tuple`（dedup+sorted）——即
production `ResearchPlannerInputSnapshot` 的 company 子集，取代旧 `security_code`
标量，见 §3.16。

### 3.4 EvalDatasetManifest（`contracts.py`）

`EvalDatasetCaseRef`：`case_id` / `case_version` / `case_fingerprint 64hex`。
`EvalDatasetManifest`：`schema_version=1` / `dataset_id`（如 `a_share_eval_v1`）/
`dataset_version>=1` / `cases tuple` / `description?`；`(case_id, case_version)` 去重。
fingerprint 基于 dataset semantic identity + ordered canonical case refs，不含
created_at / local path。

### 3.5 Typed Human Label（`contracts.py`，discriminated，非 generic）

四个 typed label（各自带 `label_type` 判别字面量）：
- `FinancialFactLabel`（`financial_fact`）：`metric_code` / `period` / `scope?` /
  `unit` / `expected_value Decimal` / `absolute_tolerance Decimal>=0` /
  `relative_tolerance Decimal>=0`。
- `RiskTopicLabel`（`risk_topic`）：`risk_code` / `required bool` /
  `acceptable_aliases tuple[str]`。
- `ClaimSupportLabel`（`claim_support`）：`claim_label_id` /
  `expected_support_status`（enum：supported/unsupported/conflicted）/
  `related_source_fingerprints tuple[64hex]`。
- `MacroCausalLabel`（`macro_causal`）：`driver_code` / `company_exposure_expected bool` /
  `causal_claim_allowed bool`。

`HumanLabel`：`schema_version=1` / `case_id` / `case_version` / `label_version>=1` /
`financial_facts` / `risk_topics` / `claim_support_labels` / `macro_causal_labels` /
`annotation?`。**禁止**：free-text `annotation` 被 machine metric 当 ground truth
（fingerprint 排除 `annotation`）。

### 3.6 Model / Variant execution config（`contracts.py`）

`FrozenModelConfig`：`provider` / `model_id` / `thinking_enabled bool` /
`temperature Decimal` / `max_output_tokens?` / `structured_output bool`；**不含 API key**。
`EvalExecutionConfig`：`config_schema_version=1` / `variant_id` / `model` /
`variant_version` / `prompt_version` / `retrieval_version` / `pipeline_version` /
`retrieval_top_k?` / `component_versions tuple[EvalComponentVersion]`（可空）。
`EvalComponentVersion`：`component_name` / `component_version`（各自 strip 非空 +
bounded）；`component_name` 唯一，canonical 按 `component_name` 排序；
`compute_execution_config_fingerprint` **包含** `component_versions`。

### 3.7 Execution 与 Scoring 分离（`contracts.py`）

`EvalExecutionSpec`：`schema_version=1` / `case_fingerprint` /
`source_snapshot_fingerprint` / `execution_config_fingerprint` / `variant_id`；
**不含** human_label_fingerprint / metric_registry_version / judge。
`EvalScoringSpec`：`schema_version=1` / `execution_result_fingerprint` /
`human_label_fingerprint?` / `metric_registry_version` / `judge_config_fingerprint?`。
`human_label_fingerprint` 与 `judge_config_fingerprint` 均为可选（None canonical）：
deterministic scoring spec（只跑 citation_validity / citation_coverage）无需 label 或
judge；None 在 fingerprint 中规范为 `null`。

### 3.8 Normalized Eval Output（`contracts.py`）

`EvalCitation`：`citation_id` / `source_fingerprint` / `locator?` / `claim_ids`。
`EvalClaim`：`claim_id` / `statement` / `claim_type` / `citation_ids`。
`EvalVariantOutput`：`schema_version=1` / `variant_id` / `case_id` / `case_version` /
`final_text` / `claims` / `citations` / `report_artifact_ref?`。**不含** CoT /
reasoning_content / API key。三 variant 最终都产出此 structure；baseline 不得用后置
强 LLM 把纯文本解析成它（公平性边界）。

### 3.9 Metrics（`metrics.py`）

- `MetricDimension`：`content_quality` / `reliability` / `efficiency`。
- `MetricKind`：`deterministic` / `human_labeled` / `semantic_judge` / `runtime`。
- `MetricStatus`：`computed` / `not_applicable` / `unavailable` / `error`。
- `MetricSpec`：`name` / `dimension` / `kind` / `metric_version>=1` / `higher_is_better`。
- `METRIC_SPECS`：完整 mapping，`keys == set(MetricName)`（测试断言）。

`MetricName`（22 项，冻结 v1 surface）：

| dimension | names |
|---|---|
| content_quality | financial_accuracy, citation_validity, citation_coverage, claim_support_rate, unsupported_claim_ratio, risk_topic_recall, macro_causal_error_rate, conflict_preservation, overclaim_rate |
| reliability | completion_rate, node_failure_rate, retry_count, recovery_success_rate, duplicate_write_rate, human_resume_success_rate, research_backflow_success_rate |
| efficiency | latency_ms, llm_call_count, input_tokens, output_tokens, total_tokens, estimated_cost |

`MetricValue`：`metric_name` / `metric_version` / `status` / `value Decimal?` /
`numerator Decimal?` / `denominator Decimal?` / `sample_count>=0` / `reason_code?`。
不变量：`status=computed → value 非 None`；`status≠computed → value None`；
`denominator=0` 非法（「无 eligible 样本」用 `not_applicable`，不自动生成 0 分）。

### 3.10 Fingerprints（`fingerprints.py`）

统一复用项目 idiom：`json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` → SHA-256 hex。排除 created_at / runtime UUID / DB execution
id / API key / wall-clock latency。8 个函数：
`compute_source_snapshot_fingerprint` / `compute_eval_case_fingerprint` /
`compute_dataset_fingerprint` / `compute_human_label_fingerprint` /
`compute_execution_config_fingerprint` / `compute_execution_spec_fingerprint` /
`compute_variant_output_fingerprint` / `compute_scoring_spec_fingerprint`。

关键排除：snapshot fp 用**内容 hash + semantic metadata**（不含 UUID）；case fp
**排除** `human_label_fingerprint`；label fp **排除** `annotation`；unordered
collection canonical sort。

### 3.11 PG→Frozen Snapshot Materializer（7B.1.1B）

`app/eval/materialization/` 把 live 源物化成 frozen snapshot 的输入：

- **依赖边界 = 仅 PostgreSQL + `RawArtifactStore` + `BundleWriter` + 领域 verifier**
  （**0 Chroma**）。document 从 `RawArtifactStore` 读原始字节；macro 从 PG
  `MacroDatasetSnapshot` / `MacroSeries` / `MacroObservation` /
  `MacroSnapshotArtifactLink` 读；structured 从 PG 对应 artifact 表读。不做任何
  向量检索。
- **no-lookahead**：materializer 只取 `fetched_at <= case.analysis_as_of` 的数据，
  不读取未来数据。
- **macro payload 的 semantic fingerprint 由 `MacroPersistenceService.
  verify_snapshot_integrity` 重算**（结构不变量 + fingerprint 重算防篡改）；eval
  模块**不复制**该算法，而是复用 domain helper
  （`app/macro/snapshot_rebuild.rebuild_macro_snapshot_fingerprint`）。
- **7B.1.4A 补齐**：materializer 额外物化 `FrozenCompanyIdentity`（从 `CompanyModel`
  + `CompanyAliasModel`，交叉核对 `security_code`）+ `FrozenSourceProviderRef`
  列表（`list_providers(enabled_only=False)` 全量投影）。document 投影新增
  title / source_url / acquired_at / authority_tier_snapshot /
  critical_claim_eligible_snapshot 字段。见 §3.16。

### 3.12 Deterministic Metrics Foundation（7B.1.2A）

`app/eval/scoring/` 实现**跨三路 variant 真正公平**的确定性指标（不依赖 label /
judge / DB / LLM / network）：

- `EvalScoringContext`（frozen）：`execution_spec_fingerprint` + `variant_output` +
  `source_snapshot`。**不含** `HumanLabel`（human_labeled 属另一来源，绝不进入
  deterministic 计量）。
- `verify_variant_output_structure()`：结构性 fail-fast 校验（unique ids / 双向
  closure / source membership），违反抛 `EvalOutputStructureError`。
- `DeterministicMetricCalculator` Protocol + registry
  （`calculate_available_deterministic_metrics()` / `get_deterministic_calculator()`）。
- **已实现 v1**：
  - `citation_validity`：分母 = 全部 citation；valid = `source_fingerprint` 命中
    snapshot 且 `claim_ids` 全指向真实 claim 且 `citation_id` 唯一；0 citation →
    `not_applicable`。
  - `citation_coverage`：分母 = 全部 claim；covered = claim 拥有 ≥1 条 valid real
    citation；0 claim → `not_applicable`。
- **未实现**（registry 暴露为 unavailable，不复制公式）：
  `claim_support_rate` / `unsupported_claim_ratio` / `conflict_preservation` /
  `financial_accuracy` 等；这些留待 judge 依赖或 human-label 阶段。

### 3.13 LLM Usage Instrumentation Foundation（7B.1.2B）

统一 LLM 调用收口，捕获每次 structured-output 调用的 usage 记录，回填全部
**10 个**生产 adapter（evidence/extractor、analysis/{financial,macro,synthesis,
valuation,claims}、audit、revision、draft_section、research_planner），为效率维度
（token/call-count）提供数据源。**0 真实 DeepSeek / 0 network**（验证全用 fake）。

- **`app/llm/instrumentation.py`**（通用层，不依赖 eval）：
  - `LlmCallUsageRecord` frozen dataclass：`component_name` / `provider` / `model_id`
    / `outcome`（success|parsing_error|invocation_error）/ `duration_ms`（≥0）/
    `usage_status`（reported|unavailable）/ 三个 token 字段 + `input_token_details` /
    `output_token_details`。**不含** raw response / prompt / AIMessage.content /
    reasoning_content / tool args / API key；usage reported → 三 token 完整非负；
    usage None → **不**自动填 0。
  - `LlmUsageObserver` Protocol + `NullLlmUsageObserver`（no-op）。
  - `invoke_structured_with_usage(model, schema, input, *, component_name, provider,
    model_id, usage_observer)`：`with_structured_output(schema, include_raw=True)`，
    读 `result["raw"]/["parsed"]/["parsing_error"]`；**先记 usage 再抛 parsing_error**；
    ainvoke 异常先记 attempt+duration+invocation_error 再 re-raise。
- **`app/eval/usage/`**：
  - `EvalLlmUsageCollector`：绑定 `execution_spec_fingerprint` / `variant_id` /
    `case_id`；实例级 list（**无模块全局**，适配 LangGraph 并行 worker）。
  - `aggregate_llm_usage(records)` → `{llm_call_count, input_tokens, output_tokens,
    total_tokens}` 四个 `MetricValue`。`llm_call_count` 统计**全部** attempt（含
    parsing_error / invocation_error）；token 指标仅当**所有** record
    `usage_status=reported` 且完整才 computed，否则 unavailable（reason_code=
    `incomplete_llm_usage`）；0 调用 → call_count=0 computed，token computed=0。
- **不**：estimated_cost / 价格 hardcode / 把 per-call duration 累加为 latency_ms /
  web fetch 价格进 runtime。

回填语义：各 adapter 返回类型 / model identity / thinking disabled / temperature /
config / prompt / schema / tool 权限**全部不变**；adapter 构造新增可选
`usage_observer` 参数（默认 None）。

### 3.14 Execution Runtime Semantics（7B.1.2C）

`app/eval/execution/` 冻结 `EvalExecutionSpec → Trial → Attempt` 三层执行身份 +
`VariantRunner` + `execute_variant_attempt` harness。纯 Python；0 DB / 0 LLM /
0 network（不含真实 variant runner）。

- `EvalTrialSpec`（frozen dataclass）：`execution_spec_fingerprint 64hex` /
  `trial_no>=1` / `schema_version=1`。trial fingerprint =
  `schema_version + execution_spec_fingerprint + trial_no` 的 canonical SHA-256
  （`compute_trial_fingerprint`，位于 execution/contracts.py）；同一 spec 下
  trial1 ≠ trial2（trial_no 不同）。**不含** `random_seed`（当前 `VariantRunner.run()`
  拿不到 TrialSpec、生产 model config 也未真正应用 seed，把 seed 放进 semantic
  fingerprint 等于给不影响真实执行的字段记账；待 provider 真正支持 deterministic
  seed 时，seed 才进入 `EvalExecutionConfig` / `FrozenModelConfig` 并由 real runner
  实际应用）。**无** attempt_no / UUID / started_at / latency。
- `EvalExecutionAttempt`（frozen dataclass）：`trial_fingerprint 64hex` /
  `attempt_no>=1` / `execution_id UUID`。attempt identity =
  `(trial_fingerprint, attempt_no)`；`execution_id` 是 runtime UUID（去重 /
  provenance），**不进** semantic identity。
- `ExecutionAttemptStatus`：`success` / `failed`。
- `EvalExecutionAttemptResult`（frozen dataclass）：`execution_id` /
  `trial_fingerprint` / `attempt_no` / `variant_id` / `case_id` / `case_version` /
  `status` / `wall_latency_ms>=0` / `variant_output?` / `variant_output_fingerprint?` /
  `usage_records tuple[LlmCallUsageRecord]` / `error_code?`。success → output + fp
  齐备、error_code=None；failed → output/fp=None、error_code 必填。**不含**
  exception message / traceback / prompt / raw response / reasoning。
- `VariantRunner` Protocol（`runner.py`）：`variant_id: EvalVariantId` +
  `async run(execution_case, execution_spec, *, usage_observer) -> EvalVariantOutput`；
  只收 execution 侧输入，**不含** HumanLabel / EvalScoringSpec（label leakage
  boundary）；`usage_observer` 由 harness 注入并线程到内部全部 LLM adapter。
  Fake/Noop runner 仅供测试，**不加** `EvalVariantId.NOOP`。
- `execute_variant_attempt(...)`（`harness.py`）：前置校验
  `runner.variant_id == spec.variant_id`（不一致抛 `EvalVariantError`）；创建
  `EvalLlmUsageCollector` 注入 runner（0 record = 0 LLM call）；单调时钟
  `time.perf_counter_ns()` 计 `wall_latency_ms`（**不** datetime、**不**把 per-call
  LLM duration 求和映射为 latency）；输出 variant/case identity 校验 + hard identity
  （duplicate id）校验 + `compute_variant_output_fingerprint`；任何 runner 异常 /
  校验失败收敛为 failed result，error_code = 异常稳定 `.code` 或
  `"eval_variant_execution_error"`（不保存 exception message）。
- `LoadedEvalExecutionCase` 现在额外携带 `case_id` / `case_version`（execution 侧
  output identity 校验所需，仍不含任何 label 信息）。

**DB schema 约束（spec R）**：持久化层（7B.1.3）的 schema 必须镜像
`ExecutionSpec 1:N Trial 1:N Attempt`——attempt 的 `execution_id` 是行 UUID（非
semantic identity），`(trial_fingerprint, attempt_no)` 才是去重身份。执行语义先于
持久化冻结，7B.1.3 只做镜像迁移，**不得**在 DB 层重新发明身份。

### 3.15 Evaluation Execution Persistence（7B.1.3A）

`app/eval/persistence/` + `app/db/models/eval_execution.py` + alembic 0045 持久化
`ExecutionSpec 1:N Trial 1:N Attempt 1:N LLM Call Usage` 四层。**只**持久化执行侧
四层（spec U：**不**持久化 MetricValue / ScoringSpec / HumanLabel / Judge），**不**
创建 `eval_runs`。0 LLM / 0 network。

- **4 张表**（`eval_execution_specs` / `eval_trials` / `eval_execution_attempts` /
  `eval_llm_call_usages`），RESTRICT 外键；`execution_spec_fingerprint` /
  `trial_fingerprint` UNIQUE，`(execution_spec_id, trial_no)` /
  `(trial_id, attempt_no)` / `(execution_id, call_index)` UNIQUE。
- **denormalized fingerprint 列**（case / source / config / spec / trial /
  variant output）供查询与快速一致性校验；JSONB 保存完整 frozen contract payload
  （`model_dump(mode="json")` → 重校验用 `model_validate`，**绝不** `model_construct`）。
- **CHECK 强制**：success/failed 与 output fp/payload、error_code 互斥
  （`ck_eval_exec_attempts_status_fields`）；usage reported 三 token 完整非负且
  `total = input + output`、unavailable 三 token 全 NULL
  （`ck_eval_llm_call_usages_token_fields`）。可空 JSONB 列 `none_as_null=True`
  （None → SQL NULL，**不**落 JSON null，否则 `IS NULL` CHECK 会漏判）。
- **create-or-get**（`create_or_get_execution_spec` / `create_or_get_trial`）：
  ON CONFLICT DO NOTHING（UNIQUE 是并发唯一性来源，无 Python 进程锁）；replay 时
  完整重校验 + 重算 fingerprint，一致 = replay（返回同 id），不一致 =
  `EvalPersistenceIntegrityError`。
- **`persist_attempt_result(result)`**：单事务写 attempt + N usage，失败全回滚；
  同 execution_id / (trial_id, attempt_no) 重放 → 完整校验，静默覆盖禁止（并发同
  attempt → 1 attempt + N usage，usage 去重靠 UNIQUE + ON CONFLICT）。
- **verifiers**（`verify_execution_spec_integrity` / `verify_trial_integrity` /
  `verify_attempt_integrity`）：加载 → `model_validate` → 重算 fingerprint → 校验 →
  返回 verified read model；usage 按 call_index 排序、连续（0..N-1）逐条重建
  `LlmCallUsageRecord`。
- **错误**：`EvalPersistenceError`（`eval_persistence_error`）/
  `EvalPersistenceIntegrityError`（`eval_persistence_integrity_error`）；消息**不**
  包含 prompt / output 文本 / token 明细 payload / API key / raw JSON。
- **downgrade guard**（0045→0044）：四张表任一存在行 → 拒绝回滚（RuntimeError，
  `alembic_version` 保持 0045）；四张表全空才允许回滚。

### 3.16 Frozen Runtime Replayability（7B.1.4A）

**目标**：审计 Frozen Evaluation Bundle 能否在**完全隔离环境（无 live PG / 无
Chroma / 0 network）**下重建三路 variant（single_rag / multi_stage_no_audit /
insightforge_full）的全部运行期输入。审计沿
`create_research_orchestration_dependencies` 装配链，逐一追踪 Planner / Initial
Fulfillment / Source Preparation / Document Parsing / Chunking / Retrieval /
Evidence Extraction / Stage4 / Stage5 / Backflow 各节点从 live PG 读取的生产字段，
核对「该字段是否已被 frozen bundle 覆盖」。

**审计结论：3 处 closure gap，已在本 stage 补齐**（`SNAPSHOT_SCHEMA_VERSION`、
`EVAL_CASE_SCHEMA_VERSION` 均 1→2）：

1. **company identity**：Planner 从 `CompanyModel`（security_code / official_name /
   short_name / exchange / board）+ `CompanyAliasModel.alias` 构建 planner 输入；
   旧 `EvalCase.security_code` 标量不足。新增 `FrozenCompanyIdentity` 取代之，
   完整冻结 planner LLM prompt 的 `CompanyIdentitySnapshot` 派生所需原始字段。
2. **document source provenance**：Evidence Extraction（`resolve_document`）与
   Preparation（no-lookahead / period 过滤）读取 `SourceRecord` 的 title /
   source_url / published_at / acquired_at / authority_tier_snapshot /
   critical_claim_eligible_snapshot / document_type / reporting_period_end；旧
   `FrozenDocumentSourceRef` 缺 title / source_url / acquired_at /
   authority_tier_snapshot / critical_claim_eligible_snapshot。补齐。
3. **provider registry**：Router（`list_providers(enabled_only=True)`）与 citation
   label（`SourceProvider.display_name`）读取 provider registry；旧 snapshot 完全
   缺失。新增 `FrozenSourceProviderRef`（provider_key / display_name / enabled /
   capabilities）。

**contract 测试（指纹敏感）**：`test_eval_fingerprints.py` 新增
`test_snapshot_fingerprint_sensitive_to_document_provenance` /
`test_snapshot_fingerprint_sensitive_to_provider_registry` /
`test_case_fingerprint_sensitive_to_company_identity` —— 任一冻结字段变化 → 对应
fingerprint 变化（防 frozen 字段漂移未被察觉）。

**FrozenRuntimeClosureAudit**（域 / 生产字段 / 读者 / 已冻结? / 必需? / 处置）：

| 域 | 生产字段 | 运行期读者 | 已冻结? | 必需? | 处置 |
|---|---|---|---|---|---|
| Planner | Company.security_code / official_name / short_name / exchange / board | `_build_input_snapshot` → `CompanyIdentitySnapshot` | ✅ | ✅ | 7B.1.4A 新增 `FrozenCompanyIdentity` |
| Planner | CompanyAlias.alias（去重排序） | 同上 | ✅ | ✅ | 7B.1.4A 新增 `aliases` |
| Planner | Company.listing_status / listing_date / delisting_date / identity_source_* / source_updated_at / identity_key | 无（仅 master-data / identity source） | ❌ | ❌ | 不冻结 |
| Planner | Task.questions[0] → research_question；Task.research_end_date → analysis_as_of | `create_plan` | ✅ | ✅ | 已冻结（`EvalCase.research_question` / `analysis_as_of`） |
| Router | SourceProvider.provider_key / enabled / capabilities | `router._build_entries` `list_providers(enabled_only=True)` | ✅ | ✅ | 7B.1.4A 新增 `FrozenSourceProviderRef` |
| Router | SourceProvider.authority_tier | 仅 provider 排序，route 快照丢弃 | ❌ | ❌ | 不冻结 |
| Router | SourceProvider.provider_type / homepage_url / allowed_domains / acquisition_methods / exchange_scope / requires_api_key | 仅 ingestion（路由/校验） | ❌ | ❌ | 不冻结 |
| Evidence provenance (doc) | SourceRecord.title / source_url / acquired_at / authority_tier_snapshot / critical_claim_eligible_snapshot | `provenance_service.resolve_document` + preparation | ✅ | ✅ | 7B.1.4A 补齐 `FrozenDocumentSourceRef` |
| Evidence provenance (doc) | SourceRecord.published_at / document_type / provider_key / reporting_period_end | 同上 | ✅ | ✅ | 已冻结 |
| Evidence provenance (doc) | SourceRecord.acquisition_method / external_document_id / provider_capabilities_snapshot / status / created_at | 无（ingestion-only） | ❌ | ❌ | 不冻结 |
| Evidence provenance (doc) | SourceProvider.display_name（citation label） | `resolve_document` → `provider_label` | ✅ | ✅ | 7B.1.4A 新增 `FrozenSourceProviderRef.display_name` |
| Evidence provenance (doc) | RawArtifact.media_type / 原始字节 | `resolve_document` / parsing+chunking | ✅ | ✅ | 已冻结（`media_type` + `content_sha256` + blob） |
| Macro | MacroDatasetSnapshot / MacroSeries / MacroObservation 全字段 | `resolve_macro` + macro analysis | ✅ | ✅ | 已冻结（macro payload deep closure，`build_macro_payload`） |
| Structured | FinancialMetricObservation / ValuationMetricObservation / RelativeValuationComparison 全字段 | financial / valuation need + Stage4 | ✅ | ✅ | 已冻结（structured payload deep closure） |
| Derived | EvidenceCard / Claim / DocumentChunk / ParsedSource / ChunkSet / Chroma index | 各 variant 自提取 / 检索 | ❌ | ❌ | **不冻结**（derived artifact，各 variant 从同一 frozen source 重建） |

**明确不冻结（by design）**：`acquisition_method` / `external_document_id`（ingestion
provenance，运行期不读）、`provider_capabilities_snapshot`（SourceRecord 上 ingestion
时 provider 能力快照，运行期读的是 `critical_claim_eligible_snapshot` /
`authority_tier_snapshot`）、provider `authority_tier`（router 排序后丢弃）、
`provider.critical_claim_eligible`（运行期只读 SourceRecord 快照值）、Company
master-data 时间戳与 identity-source 字段、Chroma 索引与 BGE embedding（derived /
pipeline config，不在 bundle 内）。

**authority_tier 语义审计（7B.1.4B.1 补证，Case B）**：`SourceProvider.authority_tier`
是否影响可观测 routing？`SourceProviderRepository.list_providers` 确实按
`authority_tier ASC, provider_key ASC` 排序，但 `router._build_entries` 在读到行后
**立即**用 `sorted({row.provider_key for row in rows})` 把 provider 折叠成**去重排序的
集合**（`SourceRouteEntry.provider_keys` 再经 field validator `sorted({...})` 二次
去重排序）——`ORDER BY authority_tier` 产生的**行序在折叠点被丢弃**，不会进入
`provider_keys` 的候选顺序、`route_payload`、`route_fingerprint`（`compute_route_fingerprint`
是纯函数，只依赖 normalized payload + plan fingerprint + router 身份）或后续 provider
selection（selection 只按 `provider_keys` 集合 + `SourceProvider.display_name` 等
frozen 字段，不读 authority_tier）。定向生产测试
`test_authority_tier_does_not_leak_into_route`（真实 `list_providers` + 真实
`_build_entries`，**不 mock 排序代码**）证明：仅改 authority_tier 时 `list_providers`
行序反转，但 `provider_keys` 与 `route_fingerprint` **完全不变**。→ **不冻结
authority_tier**，`SNAPSHOT_SCHEMA_VERSION` 保持 2，`REPLAY_PROVIDER_AUTHORITY_TIER`
replay_v1 脚手架保留（provider 行的 authority_tier 仍按 policy 写中性值，但它不参与
路由语义）。

### 3.17 Isolated Runtime Rehydration Foundation（7B.1.4B.1 + 7B.1.4B.2）

`app/eval/replay/` 把 Frozen Evaluation Bundle **复现**到一个**隔离运行时**上，
证明 7B.1.4A 的 frozen bundle 确实足以在「无 live PG / 0 Chroma / 0 network」下重建
三路 variant 的运行期输入。这是 7B.1.4A 审计（§3.16）的**落地证明**，不是新的产物。
7B.1.4B.1 落地 document 复现；7B.1.4B.2 在同一 rehydrator 内补齐 macro 复现。

- **隔离边界（结构强制）**：`EvaluationReplayRehydrator` 构造函数**只**接收
  `target_sessionmaker`（隔离 PG）+ `target_raw_artifact_store`（隔离 store root）+
  `bundle_loader`（frozen bundle）。它**没有** source/live sessionmaker、**不读**
  `DEFAULT_PROVIDERS` registry、**不调用** `SourceIngestionService`。测试
  `test_rehydrator_structurally_isolated` 断言实例字段集恰为
  `{"_sessionmaker", "_raw_store", "_loader", "_macro_service"}`；`_macro_service`
  是 `MacroPersistenceService(target_sessionmaker, raw_store)` 的**派生 wrapper**，
  只包装同一对注入依赖（用于 macro closure 的 fingerprint 一致性校验），不引入任何
  source/live 引用。
- **精确 ID replay**：frozen 的 `company_id` / `source_record_id` / `raw_artifact_id`
  作为隔离库的 DB PK **原样**落库（`RawArtifactRepository.insert` 新增 `session.add +
  flush`，与 `create` 的 ON CONFLICT 路径不同——后者不写 `artifact_id`）。
- **语义字段 vs 持久化脚手架**：frozen bundle 只携带运行期读取的语义字段
  （Company 的 security_code/official_name/short_name/exchange/board/aliases、
  Provider 的 provider_key/display_name/enabled/capabilities、Document 的 provenance）。
  其余 schema-only 字段（NOT NULL / FK / CHECK 约束但运行期不读）由
  `EVAL_REHYDRATION_POLICY_VERSION = "replay_v1"` 确定性补全（见 `replay/contracts.py`），
  **不散落**在 rehydrator、**不读** DEFAULT_PROVIDER registry。`identity_key` 由
  frozen 字段派生（`f"{exchange}:{security_code}"`）；`provider_capabilities_snapshot`
  由 frozen provider 的 `capabilities` 派生（bundle 自洽）。
- **不 seed derived artifact**：rehydration **不**写 ParsedSource / ParsedBlock /
  ChunkSet / DocumentChunk / VectorIndex / EvidenceCard。caller 在 rehydrate 后用
  `SourceParsingService.parse_source` + `ChunkingService.chunk_parsed_source` 走真实
  pipeline 重建（integration test 端到端证明）。
- **两阶段流程**：阶段一（DB session 之外）读 blob → SHA 校验 →
  content-addressed 落盘（`media_type` dispatch：PDF→`put_pdf_stream`、JSON→
  `put_json_bytes`、HTML→`put_html_bytes`）；macro raw artifacts 与 document 共用同一
  content-addressed 布局（`blobs/sha256/<first2>/<fullsha>`，SHA 相同复用同一 blob，
  不 base64 进 macro JSON）。阶段二单 DB 事务按 providers → company → aliases →
  raw_artifacts → source_records → macro（RawArtifact 先行，再 series → snapshot →
  observation → link）顺序 **create-or-verify** + commit，最后在同一事务内调用
  `verify_snapshot_integrity` 证明 domain fingerprint 一致。任一 blob SHA 不匹配、
  snapshot fingerprint 与 case 引用不一致、document provider_key 不在 source_providers、
  `media_type` 不支持、macro closure 缺失、或重算 fingerprint 与 frozen 不一致 →
  fail-fast（阶段一问题**不打开 target session**）。
- **create-or-verify immutable replay（spec A–F）**：rehydrator **不**用
  `SourceProviderRepository.upsert` 覆盖已有行、也**不**无条件 `create`。每个实体先按
  frozen PK / provider_key `load`：不存在 → 插入；已存在 → 逐 semantic + `replay_v1`
  脚手架字段比对，完全一致 → replay（返回同一 `RehydratedCase`），任何不一致 →
  `EvalReplayIntegrityError`（不覆盖、不静默改写）。语义字段——Provider =
  display_name / enabled / capabilities；Company = security_code / official_name /
  short_name / exchange / board / identity_key；RawArtifact = artifact_id /
  content_sha256 / byte_size / media_type / storage_key（storage_key 不覆盖）；
  SourceRecord = provenance + 脚手架；Alias = exact alias → replay、同 normalized
  identity 但 alias 语义不同 → reject（不产生重复）。单 DB 事务：任一语义冲突 → 整体
  回滚（无 partial rows）；content-addressed 已落盘字节可残留（不做文件删除回滚）。
- **错误分类（不复用 EvalMaterializationError）**：`EvalReplayError`
  （`eval_replay_error`，落盘/落库失败）+ `EvalReplayIntegrityError`
  （`eval_replay_integrity_error`，SHA 不匹配 / 引用不自洽 / 违反 schema 约束，
  映射 `IntegrityError`）。消息**不含** raw bytes / payload / DB URL / label / prompt /
  API key。
- **验证**：11 个 integration（真实 `alembic upgrade head` → 0045 的隔离临时库
  `insightforge_eval_replay_<random>`：document happy path + schema violation 负例 +
  幂等重放/行数不变/alias 不重复 + provider/company/raw/source 四类 mismatch 拒绝 +
  语义冲突回滚无 partial rows + macro 完整闭包（series/snapshot/5 observation/3 link/
  3 raw + `verify_snapshot_integrity` 通过）+ macro 幂等重放（行数不变）+ macro
  mismatch 拒绝）+ 6 个 unit（blob tamper / snapshot fingerprint tamper / 跨引用破坏 /
  unsupported media_type / 结构隔离 / 错误消息不泄露 payload）。0 LLM / 0 Chroma /
  0 network。

**宏观 / 财务 / 估值 rehydration 审计（spec R/S）**：

- **R1（macro payload 是否含 raw bytes？）→ 已由 7B.1.4B.2 补齐**。此前
  `build_macro_payload` 只投影**结构化 dict**（不含 raw bytes），macro isolated
  replay 存在 **closure gap**（重放出的 `MacroSnapshotArtifactLink` 引用 RawArtifact
  行但 store 无字节）。7B.1.4B.2 把 closure 扩展进 bundle：`FrozenMacroSnapshotRef`
  携带 `series` / `snapshot` / `observations` / `artifact_links` / `raw_artifacts`
  （`FrozenMacroRawArtifactRef`：artifact_id / content_sha256 / media_type / byte_size
  / role），raw bytes 以 content-addressed blob 存入 bundle（`blobs/sha256/<first2>/
  <fullsha>`，与 document 共用布局，SHA 相同复用）。materializer 从真实
  `MacroSnapshotArtifactLink` → `RawArtifact` 读 `RawArtifactStore` 字节并校验
  `SHA(bytes) == content_sha256`；rehydrator 先落 RawArtifact（含真实字节）→ 再
  series → snapshot → observation → link（`MacroSeries.provider_key` FK →
  `source_providers`，故 `world_bank` provider 先重建），**保留 frozen UUID**、**不
  重新从 WorldBank 抓取**，最后在同一事务内 `verify_snapshot_integrity` 重算 domain
  fingerprint == frozen `snapshot_fingerprint`（不复制 fingerprint 算法，走
  `rebuild_macro_snapshot_fingerprint`）。同一 bundle 二次重放精确等价、不重复/覆盖；
  任一 observation 值被篡改 → `EvalReplayIntegrityError`（不覆盖）。
- **R2（财务/估值 fingerprint 是否绑定 source_evidence_card_id？）→ 是，故结构化
  replay 本轮不实现（structured future boundary）**。`compute_metric_fingerprint`
  （`financial/contracts.py`）与 `compute_valuation_observation_fingerprint`
  （`valuation/contracts.py`）的 fingerprint payload **均包含** `source_evidence_card_id`。
  `source_evidence_card_id` 是**运行期派生的 identity**：FinancialMetricObservation /
  ValuationMetricObservation / RelativeValuationComparison 的 provenance 回到
  EvidenceCard → quote → Source，而 EvidenceCard 是 **derived artifact**（§3.16 明确
  「不冻结」，各 variant 自提取）。→ 财务/估值 observation 的 exact-ID 结构化 replay
  要求先重建 EvidenceCard 证据链，故**本轮不实现**（不新增
  `FrozenFinancialMetricObservation` 等契约），待 runner 重建证据链后再行结构化 replay。

## 4. 验收标准（7B.1.0）

1. `app/eval/` 六模块；**无 DB / LLM / network / Chroma / 新依赖**。
2. `EvalVariantId` 恰 3 项，顺序稳定，`noop/test/mock` 不在其中。
3. `METRIC_SPECS` keys == set(MetricName)，恰 22 项，dimension/higher_is_better 与
   §3.9 一致。
4. 契约 `frozen=True`；64hex 非法拒绝；snapshot duplicate identity 拒绝；dataset
   duplicate (case_id, version) 拒绝；case_id 拒绝 UUID-only。
5. HumanLabel typed（4 类判别）；annotation 不进 ground-truth（fingerprint 排除）。
6. MetricValue 状态不变量（computed 必 value / 非 computed 必无 value / denom≠0）。
7. execution spec 不含 label；label 变 → scoring fp 变、execution fp 不变。
8. 8 个 fingerprint 函数 deterministic / canonical / 语义敏感；UUID-only 变化不改
   snapshot semantic fp。
9. `pytest tests/eval` / `ruff check` / `ruff format --check` / `pip check` 全绿。

## 5. 风险审计

| # | 风险 | 影响 | 缓解（归属 slice） |
|---|---|---|---|
| R1 | **token/cost/latency 无法测量**：10 adapter 各自 `ainvoke` 且丢 `usage_metadata`，无统一收口 | 效率维度缺失 | 7B.1.2 引入统一 LLM 调用包装 + 捕获记录并回填 10 adapter（✅ 7B.1.2B 已落地）；7B.1.0 只冻结 `MetricValue` 契约 |
| R2 | **snapshot 漂移**：Stage4/5 读 live PG/Chroma，非 snapshot | frozen snapshot 与「实际读取」不一致 | snapshot 是字节寻址 manifest；runner 启动前校验读取内容 sha256 匹配 snapshot，不匹配 fail-fast（7B.1.1/1.4） |
| R3 | **baseline 偷跑**：给 single_rag / multi_stage_no_audit 喂 Full 才生成的最终 EvidenceCards | 三路比较失去意义 | snapshot 是 raw source，不是 EvidenceCard/Claim；各 variant 自提取（§1.3） |
| R4 | **judge 自评** | quality 分数虚高 | judge 模型独立固定，`FrozenModelConfig` 逐 variant 冻结（§1.5） |
| R5 | **指标来源混淆** | 不可比 | `MetricKind` 三分，比较不跨来源（§1.4） |
| R6 | **范围蔓延到 7C** | 拖垮交付 | 7B.1.0 只做纯契约；显式 OUT 清单 |

## 6. Slice 路线（后续）

- **7B.1.1A ✅ 完成**：Frozen Evaluation Bundle（offline / 无 DB）。把 Dataset Manifest /
  EvalCase / FrozenSourceSnapshot / HumanLabel / source payload 组织成可复制、可校验、
  可重放的目录；atomic 写 + replay + content-address blob；loader 由 identity 派生 path
  （防 traversal）+ label leakage boundary；`verify_bundle_integrity` 12 步 referential
  integrity；synthetic test bundle + 17 tests。
- **7B.1.1B ✅ FINAL**：PG snapshot materializer（把 live PG 物化成 frozen bundle 的输入；
  依赖 = PG + RawArtifactStore + BundleWriter + domain verifier，**0 Chroma**；
  no-lookahead；macro fingerprint 由 domain helper 重算）。
- **7B.1.2A ✅ FINAL**：Cross-Variant Deterministic Metrics Foundation（`app/eval/scoring/`；
  实现 citation_validity / citation_coverage v1 + 结构校验 + registry；只做真正公平、
  不依赖 label/judge/DB/LLM 的指标）。
- **7B.1.2B ✅ FINAL**：LLM Usage Instrumentation（`app/llm/instrumentation.py` 统一
  包装 + `app/llm/components.py` + `app/eval/usage/` collector/aggregation；回填 10
  adapter，采集 llm_call_count / input_tokens / output_tokens / total_tokens）。
- **7B.1.2C ✅ FINAL**：Execution Runtime Semantics（`app/eval/execution/`；
  `EvalTrialSpec` / `EvalExecutionAttempt` / `EvalExecutionAttemptResult` 三层身份 +
  `VariantRunner` Protocol + `execute_variant_attempt` harness；0 DB / 0 LLM /
  0 network；14 tests）。
- **7B.1.3A ✅ FINAL**：Evaluation Execution Persistence（`app/eval/persistence/` +
  `app/db/models/eval_execution.py` + alembic 0045；持久化 ExecutionSpec → Trial →
  Attempt → LLM Call Usage 四层，**不**持久化 MetricValue / ScoringSpec / HumanLabel
  / Judge；create-or-get replay + persist_attempt_result + verify_*_integrity +
  downgrade guard；18 + 2 integration tests，见 §3.15）。
- **7B.1.4A ✅ FINAL**：Frozen Runtime Replayability Gate（审计三路 variant 在无
  live PG 隔离环境下的输入闭包；补齐 company identity / document provenance /
  provider registry 三处 closure gap；schema version 1→2 + 3 个指纹敏感 contract
  测试，见 §3.16）。
- **7B.1.4B.1 ✅ FINAL**：Isolated Runtime Rehydration Foundation
  （`app/eval/replay/`；`EvaluationReplayRehydrator` 把 frozen bundle 复现到隔离
  PG + store，精确 ID replay + replay_v1 脚手架 + 不 seed derived artifact；
  8 integration + 6 unit tests；宏观/财务/估值 rehydration 审计 deferred，见 §3.17）。
- **7B.1.4B.2 ✅ FINAL**：Macro Isolated Rehydration（`FrozenMacroSnapshotRef` deep
  closure：`FrozenMacroSeriesRef` / `FrozenMacroSnapshotDetail` /
  `FrozenMacroObservationRef` / `FrozenMacroArtifactLinkRef` / `FrozenMacroRawArtifactRef`
  + content-addressed raw bytes；materializer 物化真实 `MacroSnapshotArtifactLink` →
  `RawArtifact` 字节；rehydrator 先 RawArtifact 再 series/snapshot/observation/link，
  保留 frozen UUID、不重抓 WorldBank，同事务 `verify_snapshot_integrity` 重算 domain
  fingerprint == frozen；3 macro integration tests；财务/估值结构化 replay 仍 deferred
  （source_evidence_card_id 是运行期派生 identity，见 §3.17 R2））。
- **7B.1.3B**：MetricValue / ScoringSpec / HumanLabel / Judge 持久化（本轮**未**开始）。
- **7B.1.4**：真实/dev runner（dev/test Noop runner 用独立 identity，**不加**
  `EvalVariantId.NOOP`）+ 三路 real variant runner。
- **7B.1.5**：offline CLI `python -m app.cli.eval run --variant ... --dataset ...`。
