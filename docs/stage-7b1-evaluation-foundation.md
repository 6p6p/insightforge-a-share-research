# Stage 7B.1 — Evaluation Foundation

> 状态：**7B.1.0 契约 = FINAL**；**7B.1.1A Frozen Evaluation Bundle = FINAL**；
> **7B.1.1B PG→Frozen Snapshot Materializer = FINAL**；**7B.1.2A Deterministic Metrics
> Foundation = FINAL**。
> 实现按 slice 逐块交付，每块小而完整。
> 范围：三路系统评估（single_rag / multi_stage_no_audit / insightforge_full）的
> 数据集契约、frozen snapshot、typed human label、variant 契约、确定性指标、
> 持久化、offline CLI、fingerprint。
> 明确不在 7B.1 内：Web Eval UI、真实大规模模型跑分、Fault Injection（= 7C）。

## 0. 审计结论（2026-08-13）

仓库**尚无** eval / telemetry / token-usage 基础：

- 无 `eval/`、`evaluation/`、`metrics/`、`telemetry/` 包。
- 无 token usage / cost 捕获；LLM 调用分散在 9 个独立 `DeepSeek*Model` adapter
  （evidence/extractor、analysis/{financial,macro,synthesis,valuation,claims}、
  audit、revision、draft_section），各自 `with_structured_output(...).ainvoke()`
  并**丢弃** `usage_metadata`。**无统一调用收口**（→ 效率维度依赖 7B.1.2 捕获层）。
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
  materialization/# 7B.1.1B：PG/Chroma 物化为 frozen snapshot（依赖详见 §3.11）
    service.py    #   document / macro / structured 三路 payload 投影
    projections.py#   payload bytes + sha256 + semantic fingerprint
  scoring/        # 7B.1.2A：cross-variant deterministic metrics
    context.py    #   EvalScoringContext（output + snapshot + exec fp，无 label）
    deterministic.py # citation_validity / citation_coverage v1 + 结构校验
    registry.py   #   deterministic calculator 注册表 + 可用集合
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
  `published_at?` / `reporting_period_start?` / `reporting_period_end?`。
- `FrozenMacroSnapshotRef`：`snapshot_id UUID` / `series_id UUID` /
  `snapshot_fingerprint 64hex` / `fetched_at`。
- `FrozenStructuredArtifactRef`：`artifact_type`（enum：`financial_metric_observation`
  / `relative_valuation_observation` / `relative_valuation_comparison`）/
  `artifact_id UUID` / `artifact_fingerprint 64hex`。
- `FrozenSourceSnapshot`：`snapshot_schema_version=1` + 三类 tuple；`frozen=True`；
  duplicate identity 构造时拒绝（doc 按 `content_sha256`、macro 按
  `snapshot_fingerprint`、structured 按 `(artifact_type, artifact_fingerprint)`）；
  UUID（`snapshot_id` / `artifact_id`）只是 provenance 指针，**不是** semantic
  identity，不参与去重；**不保存 raw bytes**。

### 3.3 EvalCase（`contracts.py`）

`schema_version=1` / `case_id`（稳定 slug，拒绝 UUID-only）/ `case_version>=1` /
`company_id UUID` / `security_code` / `research_question`（strip 非空、bounded）/
`analysis_as_of datetime` / `tags` / `source_snapshot_fingerprint 64hex` /
`human_label_fingerprint 64hex?`。**不含** WorkflowRun id / orchestration id /
created_at / 当前 DB execution status。

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
`human_label_fingerprint` / `metric_registry_version` / `judge_config_fingerprint?`。

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
  `financial_accuracy` 等；这些留待 7B.1.2B（judge 依赖）或 human-label 阶段。

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
| R1 | **token/cost/latency 无法测量**：9 adapter 各自 `ainvoke` 且丢 `usage_metadata`，无统一收口 | 效率维度缺失 | 7B.1.2 引入统一 LLM 调用包装 + 捕获记录并回填 9 adapter；7B.1.0 只冻结 `MetricValue` 契约 |
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
- **7B.1.2B（下一步，未开始）**：token/cost/latency 捕获层（统一 LLM 调用包装，回填 9
  adapter）+ 其余 deterministic 指标（conflict_preservation / 证据链 closure 类）。
- **7B.1.3**：eval 持久化（alembic migration + models + repository，镜像 `ReportCheckResult`）。
- **7B.1.4**：variant runner 契约（`VariantRunner` Protocol）+ dev/test Noop runner。
- **7B.1.5**：offline CLI `python -m app.cli.eval run --variant ... --dataset ...`。
