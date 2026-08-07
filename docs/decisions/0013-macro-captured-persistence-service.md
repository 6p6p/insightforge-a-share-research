# ADR-0013：Macro 原始响应捕获、Snapshot Fingerprint 与事务化持久化 Service（阶段 2C.2B）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **2C.2B 状态：implementation / automated tests completed；live external acceptance 沿用 2C.1 pending**。
   - MacroPersistenceService、原始响应捕获、Snapshot Fingerprint v1 与事务化持久化已实现并冻结；全部自动化测试（单元 + MockTransport E2E 集成）通过。
   - 本阶段不执行真实 World Bank 请求（本机对 `worldbank.org` 域名级出口阻断与 2C.1 相同）；真实验收沿用 2C.1 的 pending 状态，跑通前不开放生产宏观采集。
2. **Migration 0010 固化 fingerprint 版本字段**。
   - `macro_dataset_snapshots` 新增 `fingerprint_version`（SmallInteger，CHECK `= 1`）与 `normalization_version`（String(64)，CHECK `length(trim(...)) > 0`，当前 `world_bank_v1`），两者有 server_default，历史行可回填。
   - `fingerprint_version` 定义"从领域结果 + 归档 descriptor 构造 canonical SHA-256"的算法版本；`normalization_version` 定义"同一原始字节如何解析成 MacroIndicator / MacroGeography / MacroObservation"的规范版本。
   - 未来解析规则改变时必须升级 `normalization_version`，否则同一 raw bytes + 新 parser → 不同结构化数据 → 却命中旧 fingerprint，破坏重放审计。
3. **MacroRawJsonResponse 契约冻结原始响应**。
   - 每条原始响应以 `role`（indicator_metadata / country_metadata / observations_page）、`page`、`response_status`（2xx）、`final_hostname`（裸主机名）、`content_type`（必须 `application/json`）、`fetched_at`（时区感知）、`raw_bytes`（非空且 ≤ `MACRO_MAX_JSON_RESPONSE_BYTES` 5 MiB）捕获，构造时 8 项校验。
   - 元数据角色的 `page` 必须为 None；observations_page 的 `page` 必须为正整数。`CapturedMacroFetch.responses` 必须是 tuple（不可变，防调用方改动捕获集）。
4. **fetch_with_capture 响应顺序固定：indicator → country → observations pages**。
   - 一次获取先取 indicator metadata、再 country metadata、再按 page 升序取 observations 分页；每份响应捕获 Provider 原始字节（不重新序列化、不格式化、不改键序）。
   - 顺序固定使"同一次获取"的 artifact 序列可稳定重建，配合 fingerprint 排序规则与持久化顺序可审计。
5. **validate_captured_macro_fetch 11 项完整性校验**。
   - 持久化前校验：indicator_metadata 恰一条、country_metadata 恰一条、observations_page 的 page 集合完整等于 `1..pages`（缺页/重复/page=0/跳页均拒绝）、responses 总数 = 2 + pages、每条 content-type 基础类型为 `application/json`、final_hostname 必须 `api.worldbank.org`、result.provider_key == `world_bank`、source_id == `"2"`（WDI）、pages ≤ 18 观测分页上限、元数据 page 为 None。
   - 校验失败抛 `MacroCaptureInvalid`，发生在任何文件/DB 写入之前，保证非法捕获不落任何数据。
6. **文件 I/O 先于 DB transaction（严格写入顺序 A-K）**。
   - `persist_captured_fetch` 严格 A-K：A. validate → B. 每条原始响应先 `put_json_bytes` 内容寻址落盘 → C. 依据归档 artifact 的 content SHA-256 计算 fingerprint → D. 开启短 DB transaction → E. series get_or_create → F. raw artifact rows get_or_create → G. create_or_get_by_fingerprint → H. replay 检查 → I/J/K. 仅赢家写 links + observations → L. flush + commit。
   - 文件 I/O 失败不写任何 DB；DB 失败不回滚已落盘文件。文件与 DB 的一致性通过 replay 完整性检查在后续获取时发现。
7. **orphan 文件保留不删**。
   - `put_json_bytes` 相同内容复用（`newly_created=False`），重复获取不产生重复文件；事务失败遗留的孤儿文件本阶段保留，等待后续独立 GC 任务，避免"删除可能仍被其他快照引用文件"的竞态。
8. **Snapshot Fingerprint v1：canonical JSON + SHA-256**。
   - `build_macro_snapshot_fingerprint` 构造 canonical JSON（`ensure_ascii=True, sort_keys=True, separators=(",",":"), allow_nan=False`）并返回 64 位小写 SHA-256；golden vector 固定（`15c9607b…`），任何序列化/排序/版本规则变化都会使 golden 测试失败。
   - Decimal 用 `str(value)` 确定性字符串并保留 `decimal_scale`（1.0 与 1.00 规范上可区分）；null 保持 JSON null。
9. **fingerprint 排除可变采集时点、输入顺序无关**。
   - `fetched_at` / `request_count` / `snapshot_id` / `series_id` / `storage_key` 不参与指纹——重复获取同一数据集必须 replay 到同一 fingerprint；`fetched_at` 变化不应产生新快照。
   - observations 按 `(normalized_period_start, period)` 稳定排序、topics 按 `(topic_id, name)` 稳定排序、raw_responses 按 role → page 稳定排序，结果与输入顺序无关；值/指标/artifact sha 任一变化都会改变指纹。
10. **fingerprint 基于归档后的 artifact 内容 SHA-256**。
    - 指纹输入是"归档后 RawArtifact 的 content_sha256"而非原始响应内存引用，保证指纹与磁盘归档强一致、可由持久化的 artifact rows 重算（E2E 测试验证重算一致）。
    - `content_type` 在 fingerprint 中使用规范化基础类型 `application/json`；DB Artifact Link 可保存实际响应 header 供审计。
11. **网络 I/O 不持有 AsyncSession**。
    - `fetch_and_persist` 先完成全部网络获取（`fetch_with_capture`），再进入 `persist_captured_fetch` 的短 DB transaction；DB transaction 期间无任何网络调用。
    - `MacroPersistenceService` 自身无网络：只接收已捕获结果或已构造的 Provider，由调用方决定何时做网络。
12. **并发幂等：ON CONFLICT + 仅赢家写子记录**。
    - `create_or_get_by_fingerprint` 使用 `ON CONFLICT DO NOTHING + RETURNING`：并发相同 fingerprint 只有一个事务 `created=True`，只有赢家写 Artifact Links 与 Observations，输家回查既有 Snapshot。
    - series 用 `ON CONFLICT DO UPDATE`（no-op）会等待冲突事务提交，从而在"新 series 首次并发"场景天然串行化，避免 DO NOTHING + 回查看到未提交行的竞态。
13. **repository 不 commit、不推导业务**。
    - 所有 Macro Repository 只做数据访问（scoped 到单 AsyncSession、不 commit、不做业务判断）；Service 控制 commit，网络 I/O 与业务校验都不进 Repository。
    - ORM 显式主键：`MacroSeriesModel` / `MacroDatasetSnapshotModel` 由 Service 显式传 `uuid4()`，Core insert 全列赋值不依赖 Python 侧 default 触发。
14. **replay 完整性检查，不自动修复**。
    - fingerprint 已存在时，`_verify_replay` 检查：series_id 匹配、fingerprint 匹配、`fingerprint_version == 1`、`normalization_version == "world_bank_v1"`、Artifact Link 数 == responses 数、Observations 数 == observations 数；任一不一致抛 `MacroSnapshotIntegrityError`。
    - 不一致（如观测被篡改、版本升级后旧数据不满足新规范）不自动修复——修复策略属于数据治理决策，本阶段只暴露错误。
15. **观测值全程 Decimal，禁止 float**。
    - 观测值经 NUMERIC 列保存，人口等大数（≥ 2^53）Decimal 精确往返（E2E 用 1400000000+n 验证）；`decimal_scale` 保留原始小数位数；is_missing 语义与 `value=None ⇔ is_missing=True` 保持 2C.1 契约。
16. **错误分类稳定 4 类，消息不含敏感信息**。
    - `MacroCaptureInvalid` / `MacroArtifactConflict` / `MacroSnapshotIntegrityError` / `MacroPersistenceFailed`，稳定 code（`macro_capture_invalid` / `macro_artifact_conflict` / `macro_snapshot_integrity_error` / `macro_persistence_failed`）。
    - 错误消息不得含 raw JSON body、storage 绝对路径、DB URL、完整 URL、allowed_domains 全集；传输/持久化失败包装为稳定错误，不泄漏底层细节。
17. **不创建 RetrievalAttempt 表（记录设计决策）**。
    - 考虑过"为每次获取建立 RetrievalAttempt 记录（时间/URL/状态）"，否决：一次获取的原始响应已逐条归档为内容寻址 RawArtifact，`macro_snapshot_artifacts` 已冻结 role/page/response_status/final_hostname/content_type/fetched_at，Snapshot 已固化查询请求、分页状态与 Provider 策略快照——"一次获取"已被 Snapshot + artifact links + observations 完整刻画，RetrievalAttempt 只会重复归档元数据、引入与 snapshot 相同的 1:N artifact 关联，对证据可追溯性无增量。
    - 重放审计需求已被 replay 完整性检查覆盖（重算 fingerprint + 对比 link/观测数）；若未来需要按时间检索"何时尝试获取某数据集"，可基于既有表查询，不必新增表。
18. **Macro 数据仍不是 Evidence，不接 LangGraph**。
    - 本阶段不创建 Evidence、Claim、Report 或 DocumentChunk，不创建 Macro API，不接入 LangGraph/LLM/Agent/RAG/Chroma 编排。
    - 只有 2C.2（2C.2A + 2C.2B）完成原始响应归档、来源快照与事务化持久化，且 2C.1 真实验收跑通后，宏观数据才能进入 Evidence/Claim 管线。
19. **真实 World Bank 数据门槛仍存在**。
    - 本阶段没有持久化任何真实 World Bank 数据：全部测试使用 httpx.MockTransport，原始归档只写测试临时目录，不连 Chroma；conftest autouse guard 阻止任何非回环真实网络。
    - 生产宏观采集在真实验收跑通前不开放；2C.2B 的验收基于 schema/migration/单元/集成测试与 MockTransport E2E。
20. **2C.2B 已完成边界与遗留**。
    - 已完成：原始响应捕获（capture.py）、完整性校验（capture_validation.py）、内容寻址 JSON 归档（LocalRawArtifactStore.put_json_bytes）、fingerprint v1（fingerprint.py）、事务化 MacroPersistenceService（persist_captured_fetch / fetch_and_persist）、并发幂等、replay 完整性检查、4 类稳定错误、MockTransport E2E 集成测试。
    - 遗留：Macro API / 检索入口、FRED / NBS Provider、月度/季度频率、多国家查询、真实 World Bank 数据验收、孤儿文件 GC 均属后续阶段，未开始。

## 后果

- 建立"原始响应捕获 → 内容寻址归档 → fingerprint → 事务化持久化 → 并发幂等 → replay 完整性检查"的完整 2C.2B 链路；Service 只通过 Repository 写库，不持网络、不写 Evidence。
- 新增测试 36 项：test_capture.py 19 项（MacroRawJsonResponse 8 项校验 + validate_captured_macro_fetch 完整性 + 错误分类）+ test_fingerprint.py 9 项（golden vector / 顺序无关 / 排除规则 / 敏感性）+ test_macro_persistence_service.py 8 项 E2E（全链路持久化、fetch_and_persist、replay 幂等、跨次字节稳定 replay、并发单快照、篡改观测的 IntegrityError、校验失败零落库、DB 异常包装 MacroPersistenceFailed）。
- Migration 0010 已应用（`alembic current` = 0010 head），`fingerprint_version` / `normalization_version` 与 CHECK 在真实 PostgreSQL 上验证；downgrade 删除两列与 CHECK。
- 修正"五元组"表述为"六字段稳定身份"：`macro_series` 的稳定身份是 provider_key / source_id / external_indicator_id / geography_type / geography_code / frequency 六字段 UNIQUE（README、模型注释、测试注释同步修正）。
- 全部验证：Macro 单元测试 236 项 + 8 项 E2E 集成测试通过，ruff lint 通过；既有 PDF/Company/Task/Workflow/Disclosure Probe/World Bank MockTransport 测试保持通过。
- 遗留边界：2C.1 真实验收仍 pending；无任何持久化的真实 World Bank 数据；Macro API、FRED、NBS、月度/季度频率、孤儿文件 GC 均属后续阶段，未开始。
