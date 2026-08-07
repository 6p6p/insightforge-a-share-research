# ADR-0012：Macro 快照持久化数据模型与 RawArtifact JSON 泛化（阶段 2C.2A）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **2C.1 状态：implementation completed / automated tests completed / live external acceptance pending**。
   - 2C.1 的 Provider 契约、WorldBankProvider 与全部自动化测试已完成并冻结；live external acceptance 因本机对 `worldbank.org` 的域名级出口阻断保持 pending。
   - 网络阻断不是代码失败：接受 World Bank 官方的公开 API 形态已被 MockTransport 测试充分覆盖，本阶段不把"本机连不上外网"当作 2C.1 的代码缺陷。
2. **允许离线推进 2C.2A**。
   - 2C.2A 只建数据模型、migration、Repository 与原始归档路径，不依赖真实宏观数据；在 live acceptance 跑通前推进数据层不影响证据链正确性。
   - 在真实验收跑通前：不开放生产宏观采集、不把 Macro Snapshot 视为 Evidence、不进入 Claim/Report，也不把 2C.1/2C.2A 标为完整生产可用。
3. **Macro 数据不复用 SourceRecord**。
   - 当前 SourceRecord 是 company-bound、PDF-only 的公司披露来源记录；宏观数据是"国家/指标/年份"粒度的结构化观测，不绑定单一公司，不是 PDF 字节流。
   - 强行复用会迫使宏观数据伪造 company_id、扭曲 document_type、把 JSON 塞进 PDF 归档，破坏 2B 阶段冻结的语义。
4. **不把 Macro JSON 包装成 SourceRecord**。
   - Macro 原始 JSON 响应直接归档为 RawArtifact（media_type=`application/json`），不创建 SourceRecord、不写 provider_key/document_type/source_url 等公司披露字段。
   - SourceRecord 的语义、Schema 与既有 ingestion 流程（上传/URL 导入仍只接受 PDF）保持原样，本阶段不修改 2B 表结构与阶段 2C.1 的 Provider 契约。
5. **RawArtifact 从 PDF-only 泛化为 PDF+JSON**。
   - `RawArtifactMediaType` 增加 `application/json`，DB CHECK 与 migration 0009 同步放宽；`content_sha256` 对全部媒体类型保持全局唯一（内容寻址不变）。
   - 全部 PDF 行为不变：Source ingestion 仍只接受 PDF、SourceRecord 仍只引用 PDF、PDF storage_key（`sha256/ab/cd/<hash>.pdf`）与既有归档不迁移、既有 PDF 测试全部保留。
   - JSON 只用于 Macro 原始响应归档，是"不可变、内容寻址、业务来源登记的原始字节"这一 RawArtifact 抽象的自然扩展，不引入第二个存储抽象。
6. **每次 HTTP 响应单独归档，不重新构造**。
   - 一条 Macro 获取可能包含：1 份 indicator metadata JSON、1 份 country metadata JSON、1–18 份 observations page JSON；每份原始响应分别作为独立 RawArtifact 归档，并由 `macro_snapshot_artifacts` 按 role/page 关联到 Snapshot。
   - 不把多份响应合并/重新序列化成一个"伪装的原始 JSON"：归档保留 Provider 原始字节（不重新序列化、不格式化、不改键序），SHA-256 基于原始字节计算，保证审计与重放可追溯。
7. **MacroSeries 与 MacroDatasetSnapshot 分工**。
   - `macro_series` 保存稳定身份：provider_key / source_id / external_indicator_id / geography_type / geography_code / frequency 六元组 UNIQUE，创建后不可变；同一身份并发写入由 PostgreSQL ON CONFLICT 保证只保留一行。
   - `macro_dataset_snapshots` 保存一次"查询-获取"产生的不可变快照：查询请求、来源元数据、Provider 策略快照与分页状态，`snapshot_fingerprint` 全局唯一。
   - 身份与快照分离使"同一指标多次获取"可版本化对比，而不需要复制身份数据。
8. **指标名称、单位、地区名等可变属性存 Snapshot 而非 Series**。
   - `indicator_name` / `indicator_unit` / `geography_name` / `region_name` / `income_level_name` / `source_name` / `source_note` / `source_organization` / `topics_snapshot` 等随 Provider 元数据可能变化的字段放在 Snapshot。
   - Series 只保存稳定身份，避免把"某次获取时的元数据"误当作指标固有属性固化；每次 Snapshot 反映该次获取的真实元数据。
9. **Observation 绑定 Snapshot 而非 Series**。
   - `macro_observations.snapshot_id` FK `macro_dataset_snapshots`（ON DELETE CASCADE），观测值是某次获取的结果快照，随快照版本变化。
   - 快照删除级联删除观测，保证"没有快照就没有观测"，版本边界清晰；不提供 update，观测写入后不可变。
10. **冻结每条 API 响应**。
    - `macro_snapshot_artifacts` 记录每次 HTTP 响应的 `response_status`（BETWEEN 200 AND 299）、`final_hostname`（非空）、`content_type`、`fetched_at`，原始正文经 RawArtifact 归档。
    - 任何响应级别的不变量（role/page 组合、响应状态、hostname、与 Snapshot 的绑定）在 2C.2B Service 写入前先在 schema 层冻结，避免把不一致的响应固化进表。
11. **观测值使用 PostgreSQL NUMERIC，不用 DOUBLE PRECISION / REAL / FLOAT**。
    - 人口等宏观数值可超 2^53，float 中间转换会丢精度；NUMERIC 保证十进制全程精确。
    - 2C.2A 只冻结列类型为 NUMERIC（测试验证 Decimal 大数往返不失真）；`decimal_scale` 单独存原始小数位数（is_missing=false 时必须非负，is_missing=true 时必须为 NULL）。
12. **normalized_period_start 的意义**。
    - `period` 是 Provider 年份标签（`^[0-9]{4}$`）；`normalized_period_start` 固定为该年 1 月 1 日，**只用于排序/索引/统一时间轴，不表示 Provider 真实统计周期起始日**，`period_semantics` 固定 `provider_year_label`。
    - DB CHECK 强制 `normalized_period_start` 的年月日与 `period` 一致，杜绝"标签 2020 却存 2020-06-01"这类不一致。
13. **Snapshot fingerprint 的职责**。
    - `snapshot_fingerprint` 是 64 位小写十六进制、`CHAR(64)` 全局唯一，唯一标识一次获取，用于去重与"同一次获取的多个 artifact/observation 分组"。
    - 2C.2A 只冻结字段格式与数据库唯一性；**正式生成算法在 2C.2B 冻结**，本阶段不在 Repository 内猜测算法（`MacroSnapshotRepository.create` 不做哈希推导）。
14. **Provider 策略快照**。
    - Snapshot 固化 `acquisition_method`（当前固定 `official_api`）、`authority_tier_snapshot`（1–4）、`critical_claim_eligible_snapshot`、`provider_capabilities_snapshot`（JSON array）、`source_id_snapshot` 等获取时点的 Provider/来源策略。
    - 快照与 Provider 当前配置解耦：即使 Provider 配置变化，历史快照反映获取当时的策略（与 2B SourceRecord 的 capabilities 快照同语义）。
15. **Artifact Link role/page 语义**。
    - `macro_snapshot_artifacts.role` ∈ {`indicator_metadata`, `country_metadata`, `observations_page`}。
    - role=`observations_page` 必须带 `page >= 1`；元数据角色（indicator/country metadata）的 `page` 必须为 NULL。
    - 两个 UNIQUE 均启用 `NULLS NOT DISTINCT`：`UNIQUE(snapshot_id, role, page)` 保证同 snapshot 同 role 只能一条（元数据角色 page 恒 NULL，否则 PostgreSQL 默认把 NULL 视为互不相同导致约束失效）；`UNIQUE(snapshot_id, artifact_id, role, page)` 防止同一 artifact 在同一位置重复关联。
16. **本阶段没有 PersistenceService**。
    - 2C.2A 只交付数据模型、migration 0009、三个 Repository 与原始 JSON 归档能力；`MacroPersistenceService`、Macro API、Snapshot 真实写入、fingerprint 算法都属 2C.2B。
    - Repository 只做数据访问（不 commit、不 update、不做业务判断），为 2C.2B 的 Service 提供稳定接口。
17. **不用数据库触发器验证 Artifact media type**。
    - "Artifact Link 只能引用 application/json RawArtifact"无法仅靠通用 CHECK 表达，也不适合用 DB 触发器：触发器会把 2C.2B 的 Service 职责硬编码进 schema，增加维护面且无写入路径需要保护（本阶段无 Service 写入）。
    - 该不变量由 2C.2B 的 PersistenceService 保证，并在本 ADR 记录为 Service 边界；`macro_snapshot_artifacts.content_type` 只记录去除参数后的基础类型。
18. **2C.2B 的边界**。
    - 2C.2B 将实现 MacroPersistenceService：应用 Provider 策略快照、冻结 snapshot_fingerprint 正式算法、校验 Artifact Content-Type 基础类型为 `application/json`、真实写入 Snapshot/Observation/Artifact Link、解析归档并重建观测。
    - 2C.2B 未开始前，任何 Service 写入路径都不存在；数据库仅承载本阶段冻结的 schema 与 Repository 契约。
19. **真实 World Bank 数据门槛仍存在**。
    - 2C.1 的 live external acceptance 仍受网络阻断保持 pending；**本阶段没有任何已经持久化的真实 World Bank 数据**（未执行真实请求、未捕获真实响应、未写生产宏观数据）。
    - 生产宏观采集在真实验收跑通前不开放；2C.2A 的验收只基于 schema/迁移/单元与集成测试。
20. **Macro 数据仍不是 Evidence**。
    - 本阶段不创建 Evidence、Claim、Report 或 DocumentChunk，不把 Macro 数据接入证据链，不接入 LangGraph/LLM/Agent/RAG/Chroma 编排。
    - 只有 2C.2（2C.2A + 2C.2B）完成原始响应归档、来源快照与持久化，且 2C.1 真实验收跑通后，宏观数据才能进入 Evidence/Claim 管线。

## 后果

- 建立宏观数据持久化的数据模型与 JSON 原始归档路径，与公司 PDF SourceRecord 完全解耦；为 2C.2B 提供稳定的表结构与 Repository 契约。
- RawArtifact 泛化为 PDF+JSON：新增 29 项集成测试（`tests/integration/test_macro_persistence_schema.py`）验证四表存在、RawArtifact media type CHECK、各模型 FK/UNIQUE/CHECK、Artifact Link role/page、NULLS NOT DISTINCT 唯一性、NUMERIC 大数精度、value/is_missing 与 normalized date/year、decimal_scale、ON DELETE CASCADE/RESTRICT、并发 get_or_create、Snapshot/Observation/Artifact Link 稳定排序、数据精确清理、原始归档只写 tmp_path（不访问真实网络、不连 Chroma、不写真实 `.data/raw`）。
- 单元测试新增 61 项（JSON RawArtifactStore 21 + 模型约束 22 + Repository 契约 21）；既有全部 PDF/Company/Task/Workflow/Disclosure Probe/World Bank MockTransport 测试与网络 Guard 保持通过；全部集成测试（61 既有 + 29 新增 = 90）通过。
- 迁移 0009 已应用（`alembic current` = 0009 head），四表与 RawArtifact media_type CHECK 在真实 PostgreSQL 上验证；downgrade 在存在 JSON RawArtifact 时明确拒绝。
- 遗留边界：2C.1 真实验收仍 pending；无任何持久化的真实 World Bank 数据；MacroPersistenceService、Macro API、fingerprint 正式算法、FRED、NBS、月度/季度频率均属后续阶段，未开始。
