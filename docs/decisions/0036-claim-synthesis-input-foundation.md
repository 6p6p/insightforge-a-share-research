# ADR-0036：Claim Synthesis Input & Provenance Foundation（阶段 4D.1A）

- 状态：已接受
- 日期：2026-08-10
- 决策人：InsightForge 项目

## 背景

4C 已经把各 domain 的 Claim 分析产物登记为可追溯、可回放的 Claim（generic
business / event / risk v1、financial v2/v3、macro v4/v5/v6、valuation v7）。
4D 的目标是**综合**这些已登记的 Claim（Claim Synthesis / Conflict / Evidence
Gap）。4D.1A 先把综合的**输入集边界**做扎实：**调用方显式选出的 2..50 条跨
domain Claim + company + research_question + analysis_as_of** 登记为一个不可变
`SynthesisRun`，并校验每条输入 Claim 的**完整性 / 隔离 / no-lookahead**，使
未来 LangGraph 合成节点只能消费"已验证、同公司、同问题、不掺未来信息"的 Claim
集合。**本阶段不是合成判断，也不创建 Report / DraftSection / Report**。

**角色边界（综合输入 = 显式选择，不做语义筛选）**：
- 调用方 / 未来 LangGraph state 提供 `claim_ids`（2..50，显式选择）；
- caller 只提供 company_id / research_question / analysis_as_of / claim_ids；
  claim 的 domain / kind / confidence / importance / fingerprint / evidence /
  source / company / question 一致性**一律从真实 Claims 与 domain provenance
  确定性校验派生**，caller 不得提供；
- **不调用 LLM、不接 LangGraph 合成节点、不做语义判断**——只证明"这些已验证的
  Claim 在什么 question / cutoff 下进入综合"。

## 决策

1. **阶段边界（spec G）**：4D.1A / 4D.1B **不创建 ReportOutline /
   DraftSection / Report / Audit**；`claim_synthesis_runs` /
   `claim_synthesis_input_links` 允许存在，Stage-5 表**不得存在**。

2. **Migration 0028（spec H）**：`claim_synthesis_runs`（synthesis_id PK；
   company_id FK companies RESTRICT；research_question +
   research_question_sha256；analysis_as_of；synthesis_schema_version >= 1；
   synthesis_fingerprint CHAR(64) **UNIQUE**；created_at；CHECK sha256 /
   fingerprint 64 位小写 hex、question trim 非空；INDEX company_id /
   analysis_as_of）+ `claim_synthesis_input_links`（PK(synthesis_id, claim_id)；
   synthesis_id FK runs **CASCADE**、claim_id FK claims **RESTRICT**——Claim
   存在期间 link 不静默消失，保证 provenance 可重放；INDEX claim_id）。
   **不复制** Evidence / Calculation / Transmission / Comparison ID 到 synthesis
   表——Claim → 各 domain 子表 → Evidence → Source 的 provenance 已在既有
   schema 中，本表只引用 claims.claim_id。**downgrade guard（spec U）**：
   任一表存在行 → 拒绝回滚（不静默丢弃已登记的 synthesis 输入集），
   alembic_version 保持 0028；全部为空才允许回到 0027。

3. **Schema version（spec I）**：`CLAIM_SYNTHESIS_SCHEMA_VERSION = 1`；
   synthesis 无 analyst version（本阶段无 Analyst，只有输入登记）。

4. **`app/synthesis/` 包（spec J）**：contracts.py（draft / verified claim /
   summary / fingerprint）、errors.py（稳定错误分类）、integrity.py
   （ClaimIntegrityGateway）、service.py（create_or_get_synthesis 两步提交）；
   持久化复用 `app/repositories/claim_synthesis_run_repository.py` /
   `claim_synthesis_input_link_repository.py` 与
   `app/db/models/claim_synthesis_run.py` / `claim_synthesis_input_link.py`。

5. **Claim 选择显式（spec K）**：`SynthesisInputDraft.claim_ids` 2..50，去重 +
   canonical 排序（`str(uuid)` 升序，与提交顺序无关）。**不做语义筛选**——
   选择权在调用方 / 未来 LangGraph state。

6. **Research-question 隔离（spec L）**：每个输入 Claim 的
   `research_question_sha256` 必须 == draft question 的
   `compute_research_question_sha256`，否则 `SynthesisResearchQuestionMismatch`。
   综合只能基于同一 research question 下产生的 Claim，跨 question 混入拒绝。

7. **Company 隔离（spec M）**：每个输入 Claim 的 `company_id` 必须 ==
   draft.company_id，否则 `SynthesisCompanyMismatch`。跨公司混入破坏
   company-level 综合边界。

8. **ClaimIntegrityGateway（spec N）**：按 Claim 的**真实 analysis_domain +
   claim_schema_version** dispatch 到 generic（business / event / risk v1）/
   Financial（v2/v3）/ Macro（v4/v5/v6）/ Valuation（v7）完整性校验，返回
   `VerifiedSynthesisClaim`。**为何不调用各 domain service 的 private
   `_verify_replay`**：replay 校验需要重建 semantic draft，而 automatic vs
   additional Evidence 在 claim_evidence_links 中不可区分。gateway 改为：从
   persisted links + domain 子表重建 fingerprint 输入 → 调用各 domain 的**公开**
   `compute_*_fingerprint` → 与 claim.claim_fingerprint 对比。这是唯一能处理
   含 automatic Evidence 的历史 Claim 的方案。**禁止复制** domain formula /
   transmission policy / comparison policy / fingerprint 逻辑本身（只调用公开
   函数）。macro 重建 additional_context = context − transmission_ids（来自
   macro_transmission_evidence_links），与 MacroClaimService `_derive` 一致。
   Claim 缺失 / domain 子表缺失 / fingerprint 不一致 / 引用的 Evidence /
   Calculation / Comparison 缺失 → `SynthesisClaimIntegrityError`，**不自动
   repair**；legacy macro v1/v2 链 analysis_as_of 为 NULL（无法重算 fingerprint
   且无 temporal 语义）→ `SynthesisUnsupportedClaimSchema`，不猜测 / 不跳过。

9. **Temporal no-lookahead（spec O）**：每条输入 Claim 的全部 Evidence
   availability（复用 `resolve_availability`：document → source.published_at
   否则 acquired_at；macro → snapshot.fetched_at；**绝不用
   reporting_period_end**）必须 <= synthesis analysis_as_of；future →
   `SynthesisFutureEvidence`；无法解析（provenance 缺失）→
   `SynthesisTemporalEvidenceInsufficient`（不伪造缺失日期）。domain
   analysis_as_of（macro chain / valuation profile）也必须 <= cutoff（域分析
   截止晚于综合截止 = 分析基于综合之后的信息）。当前 schema 用 RESTRICT FK
   保证 provenance 行不可悬空，因此 Insufficient 是防御性分支（单元层直接验证
   resolve_availability 的 None 映射）。

10. **No semantic mutation（spec P）**：Claims / EvidenceCards / 各 domain 子表
    **永不改写**。综合输入只接受已验证的 Claim；修改观点 = 新 Claim = 新
    fingerprint = 新 run，旧 run 保留。**无 update API**。

11. **Fingerprint（spec Q）**：`compute_synthesis_fingerprint` = canonical JSON
    （sort_keys + 固定 separators + UTF-8）+ SHA-256，含
    synthesis_schema_version / company_id / research_question /
    research_question_sha256 / analysis_as_of / claims（按 claim_id canonical
    排序，每项 claim_id / claim_fingerprint / analysis_domain / claim_kind /
    claim_schema_version）。**不含 synthesis_id / created_at**。同一完全相同
    input → 同一 fp → replay 同一 run；question / cutoff / claim set /
    fingerprint 任一变化 → 新 fp → 新 run；input 提交顺序不影响指纹。

12. **Persistence（spec R，两步提交镜像 ClaimService / ValuationAnalysisService）**：
    短 DB session 加载 + gateway + 隔离 + temporal 校验（0 写）→ **关闭 DB
    session**（期间不持有 connection / transaction）→ 纯函数派生
    （research_question_sha256 + fingerprint + summary）→ 短 DB transaction：
    `ClaimSynthesisRunRepository.create_or_get`（PG `ON CONFLICT(
    synthesis_fingerprint) DO NOTHING`，**无进程锁**）→ 首次 created=True 时
    bulk insert 全部 input links 原子 commit；fingerprint 命中 → replay。任一
    校验失败 → 0 写；`SQLAlchemyError → rollback + SynthesisPersistenceFailed`。
    **并发相同 fingerprint → 最终 1 run + 1 套 links**。

13. **Replay integrity（spec S，不 repair）**：fingerprint 命中时**重新加载**
    run / links / claims / domain provenance / evidence，逐项核实 run 字段
    （company / question / sha256 / cutoff / schema_version / fingerprint）+
    exact claim set == draft.claim_ids + 重新执行 gateway / 隔离 / temporal /
    fingerprint。任一损坏 → `SynthesisIntegrityError`，**不自动 repair**（输入
    任一变化 = 新 run，旧 run 保留）。`SynthesisClaimIntegrityError` 与
    `SynthesisIntegrityError` 是**兄弟子类**（非父子）——corrupted claim replay
    抛 ClaimIntegrityError，不被 IntegrityError catch 吞掉。

14. **`SynthesisInputSummary`（spec T）**：纯函数确定性结构摘要（claim_count /
    domain / kind / confidence / importance 计数，key 缺失补 0，全确定性）。
    本阶段**不决定** core / conflict / evidence gap——只提供结构化输入画像供
    LangGraph 消费。

15. **错误分类**（`errors.py`，8 个稳定 code）：SynthesisError 基类 +
    SynthesisDraftError（draft 构造）/ ResearchQuestionMismatch / CompanyMismatch /
    UnsupportedClaimSchema / ClaimIntegrityError / FutureEvidence /
    TemporalEvidenceInsufficient / IntegrityError / PersistenceFailed。错误消息
    不包含 evidence 正文 / 完整 raw content / DB URL / absolute path / UUID
    集合明细。

16. **测试（spec V）**：
    - **25 项单元**（`tests/synthesis/test_contracts.py`，零 DB）：draft 构造
      校验（trim / 非空 / 2..50 边界 / 去重 + canonical 排序 / UUID / date）、
      fingerprint 确定性 + 敏感性（order-independent / question / cutoff /
      claim set / claim fingerprint / domain / kind / 稳定性，**纯函数不自行
      去重**）、summary 计数、resolve_availability 的 None 映射；
    - **26 项集成**（`tests/integration/test_claim_synthesis_service.py`，真实
      PG + 真实服务链 seed，零 Chroma / LLM）：gateway dispatch（business /
      event / risk / financial / macro / valuation）+ missing /
      unsupported / corrupted fingerprint / corrupted evidence link、isolation
      （company / research_question mismatch）、temporal（future document /
      macro snapshot / macro analysis_as_of / valuation analysis_as_of）、
      persistence / replay（first create / replay / input order / claim-set
      change / cutoff change / concurrent / corrupted link / corrupted claim /
      no mutation）、cross-domain E2E（1 business + 1 financial + 1 macro + 1
      valuation Claim → 1 SynthesisRun → 4 条精确 input links → replay 同一
      run）、boundary（无 Stage-5 report 表；Service 只持有 sessionmaker）；
    - **3 项 migration 0028 downgrade guard**（`tests/integration/
      test_migration_0028_downgrade_guard.py`，isolated 临时 PG：空库
      upgrade 0028 → downgrade 0027 成功两表被删 / runs 有行拒绝且数据保留 /
      links 有行拒绝）。
    全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表。全量 **1694 非集成
    + 639 集成通过**。

## Runtime acceptance（2026-08-10）

- **Windows runtime**：在 HEAD（含本 ADR 对应代码）上运行
  `python -m app.cli.run_backend`，live / ready 各 **5×200**；ready 五项 checks
  （configuration / database / chroma / checkpoint / raw_storage）全部 ok。
  停止 host backend。
- **Docker rebuild**：`docker compose up -d --build backend` 重建当前代码，
  容器 healthy，live / ready 各 5×200、五项 checks ok；从 Docker runtime
  **实际读取** `alembic_version=0028` 与两张 synthesis 表
  （claim_synthesis_runs / claim_synthesis_input_links），表存在且列结构与
  migration 0028 一致。

## 边界

- **不创建 Report / DraftSection / Report / ReviewIssue / Audit**；不接
  LangGraph 合成节点（Service 层供未来 LangGraph 顶层编排调用）；不开放 HTTP
  API；不调用 LLM / 不查 Chroma / 0 Retrieval。
- **不做语义筛选 / 冲突检测 / 证据缺口分析**（4D.1B 及以后）。
- 不修改 generic / Financial / Macro / Valuation 既有 schema；不批量 update
  历史 rows；**不开始 4D.1B**。
- 不提交 `.env` / API key / 完整 prompt / raw provider response。
