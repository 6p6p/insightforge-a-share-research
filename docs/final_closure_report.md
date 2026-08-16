# InsightForge V1.1 最终产品收口（Autonomous Closure）验收报告

- 分支：`v1.1-product-completion`（v1.0.0 tag 未触碰；无 push）
- 提交：`8fd7afe` → `3ab3479` → `f9e4b51` → `3febe0c` → `dee4b69` → `5cd6206` → `5c4e097` → `de1c003` → `95e9a92`（9 个收口提交）
- 日期：2026-08-15 ~ 2026-08-16

---

## 一、执行摘要

V1.1 产品收口完成。全部 P0/P1 生产供给链缺口已补齐并经**真实生产路径**验证：

- **公司主数据**（5543 家 / 11098 别名）、**官网域名 registry**（5470 家）在 fresh volume 上由启动 bootstrap 自动导入，且 self-heal（repair）路径在数据被清空后实测恢复；
- **来源注册表** 14 个 provider（含 eastmoney / issuer_official / user_supplied）；
- **URL 自动解析来源**、**上传 PDF 无需伪造 URL**、**报告/公告/IR 自动发现**（East Money，反爬握手）、**宏观自动获取**（World Bank）、**财务数值人工转录**（Tier-4 证据卡）全部打通；
- **前端**移除全部 legacy/Stage UI，need-code 产品化，估值开关隐藏，财务数据录入 tab 上线；
- **场景 A**：宁德时代真实研究任务从创建到 6 节报告 + 审计 + backflow 全自动推进（自动获取 4 份真实年报/季报 → 21 张证据卡 → claims → 草稿 → 报告 → 审计 16 项问题 → 补充研究回路）；
- **场景 B**：财务转录 → 确定性计算 → 财务 claims 全链验证；
- **场景 C**：资料不足 → 真实 PDF 无 URL 上传 → parse/chunk/index → resume 验证；
- 最终门禁全绿：后端单元 2363 + 集成 1087、ruff、pip check、alembic（0049 head 无漂移）、前端 93 tests / typecheck / build、Docker fresh-volume 启动 + 真实模型 smoke。

## 二、验收结论

| 门禁项 | 结果 |
|---|---|
| 后端单元（非集成） | ✅ 2363 passed |
| 后端集成（真实 PG + Chroma，临时/共享库） | ✅ 1087 passed |
| ruff check / format | ✅ 全量通过 |
| pip check | ✅ 无损坏依赖 |
| alembic current / heads / check | ✅ 0049 (head)，无模型漂移 |
| 前端 npm test / typecheck / build | ✅ 93 / 93 + 通过 + 构建成功 |
| Docker fresh-volume 启动 | ✅ 迁移 head → registry 14 → master 5543 → issuer domains 5470 → /ready 全绿 |
| 真实模型（DeepSeek + BGE）smoke | ✅ 场景 A/B/C 实测（见 §7） |

**结论：V1.1 产品完成（PRODUCT COMPLETION）**。核心产品路径（任务 → 计划 → 自动/人工资料供给 → 证据 → 分析 → 报告 → 审计）在真实堆栈上端到端可用；剩余的「新闻/事件资料」缺口是**设计内的人工兜底边界**（新闻需原创发布者验证链，见 §9）。

## 三、本轮交付范围

1. **Schema 0049**：`source_records.source_url` nullable（URL CHECK NULL 容忍）、`acquisition_method` 增 `automatic_discovery`/`user_supplied`、`evidence_cards` origin 增 `user_supplied`（一致性/空 locator CHECK 分支）、新表 `issuer_domains` + `issuer_domain_snapshots`（provenance，downgrade guard）。
2. **用户转录证据链**：`UserSuppliedEvidenceService`（Tier-4 证据卡，确定性 JSON 收据 artifact、幂等 replay、extractor=user_transcription v1/low）；`FinancialMetricService` 接受 user_supplied origin；`POST /tasks/{id}/financial-observations`。
3. **自动发现**：`AnnouncementDiscoveryService`（East Money 公告 API：列表/内容/PDF 下载 + **反爬 cookie 握手** + %PDF 校验 + allowlist 不变；扫描窗口按报告发布期推导；公告标题排除法律意见等扫描件；IR 关键词）；`ingest_discovered(bytes)`；DocumentNeedExecutor 集成（含 not-indexable 替代来源获取）。
4. **宏观自动获取**：`MacroAutoFetchService`（确定性 topic→World Bank indicator 白名单，有界 5 年窗口）。
5. **Planner 尊重用户模块**：`plan_scope.apply_selected_modules` 纯函数过滤（events 门控：未选事件剔除 event/news/公告/IR 需求；valuation 由开关门控——开关已隐藏 → 默认关闭）。
6. **URL 自动解析**：`POST /source-providers/resolve`（issuer_domains 优先 → provider allowlist）；上传 `source_url` 可选。
7. **Issuer 官网**：`issuer_domains_v1.json`（5470 家，SZSE 官方名录 + EM F10 降级）+ bootstrap + `issuer_official` provider（域名动态校验，catl.com 实测解析成功）。
8. **前端产品化**：删除 WorkPlanEditor/StartResearchPanel/手动研究方案/估值开关；上传/导入自动识别来源 + 413 友好提示 + URL 可选；need-code 中文标签；财务数据录入 tab；术语重扫。
9. **nginx 413**：`client_max_body_size 100m`。
10. **生产修复**（场景实测驱动）：BGE 离线模型（镜像 + HF_CACHE_HOST 挂载 + HF_HUB_OFFLINE）、extractor v2/v3（相关性标准、空白容差 quote、空 items 合法）、writer v4（数字逐字核查清单 + validation 违规有界重试 + grounding corpus 含报告期日期）、claim critical 降级、financial analyst v2（数字自检清单）+ numeric guard 年份/期间引用对齐、soft extraction、source_preparation structlog 修复。

## 四、生产供给链验证

| 供给链 | 来源 | 验证 |
|---|---|---|
| 公司主数据 | SSE 官方 API + SZSE 官方 xlsx + BSE（Sina 成员 + EM F10） | fresh volume 自动导入 5543/11098；`repair:true` 自愈实测（两次：测试清理后重启恢复） |
| 官网域名 | SZSE 名录「公司网址」列 + EM F10 ORG_WEB | 5470 家；`https://www.catl.com` → issuer_official（issuer_domain 匹配） |
| 来源注册表 | 14 provider 内置定义 | seed 幂等（14+1 保留未知） |
| 定期报告 | East Money 公告 API（Tier-3 后备） | 宁德时代 2023/2024 年报、2025 半年报、2025 Q3 季报自动获取（真实 PDF，反爬握手） |
| 公告/IR 材料 | 同上（标题过滤 + 排除扫描件） | 董事选举/股东会决议公告、《投资者关系管理制度》、投资者关系活动记录表 |
| 宏观数据 | World Bank API（topic 白名单） | fetch_and_persist 幂等（单元/集成覆盖） |
| 财务数值 | 用户转录（官方报告引文，Tier-4） | 2022-2024 营收/归母净利/营业成本转录 → 确定性计算（yoy/margin） |

## 五、前端产品化

- 正常用户流程只保留「创建任务 → 自动开始研究 → 资料不足补充 → 继续研究 → 报告」；
- legacy/Stage UI（手动研究方案、WorkPlanEditor、StartResearchPanel、估值开关）已从流程移除（文件删除 + 测试更新）；
- 上传/导入：URL 可选（本地 PDF 不伪造链接）；URL 自动识别来源（resolve 回填 provider + 提示）；413 → 「文件过大：单个 PDF 不能超过 100MB」；
- need code 中文产品化（缺失需求：年度报告、财务数据…，原始代码折叠在技术详情）；
- 财务数据 tab：从官方报告转录表单（指标/口径/期间/单位/引文/陈述/来源，422 错误透出）；
- 术语回归全绿（无用户可见 Stage/work-plan 残留）。

## 六、安全与完整性边界（未放松）

- **LLM 数字边界**：Financial Claim statement 禁数字字面量（numeric-literal guard 保留；仅 4 位年份/上一年度等期间引用按语义放行——百分比/金额/中文数字仍全部拒绝）；
- **quote 逐字性**：空白容差只允许空白差异（非空白字符骨架唯一匹配），卡内 quote 仍为原文逐字切片；
- **allowlist/SSRF**：自动发现/下载全程 provider allowlist 校验（eastmoney.com / dfcfw.com），反爬握手不绕过任何域名检查；
- **news_article 不可经上传注入**（仅原创发布者验证链）；
- **critical claim 需 eligible 证据**：importance 上限策略（无 eligible 证据 → 降级 normal，不提升）；
- **报告数字 grounding**：writer 数字必须逐字来自引用证据（+ 报告期上下文年份），违规有界重试后仍拒绝（0 写）；
- 审计/检查链完整（16 项 issue 实测捕获并路由 backflow）。

## 七、场景验证（真实堆栈 + 真实模型）

### 场景 A — 文档研究（宁德时代，公司概况/业务/风险）
1. 创建任务（问题：业务结构、增长驱动与主要风险）→ 自动计划（真实 DeepSeek planner）；
2. 自动发现并获取 4 份真实报告（2023/2024 年报、2025 半年报、2025 Q3 季报）——**无需用户下载**；
3. 自动 parse → chunk → BGE index → LLM 证据抽取（extractor v3，21 张证据卡）；
4. claims（importance 上限）→ synthesis → 6 节报告草稿（业务结构/收入增长/原材料风险/竞争风险/减值风险/风险与缺口）→ 报告生成；
5. 检查 + 审计：`audit_status=fail, 16 issues（wording_overclaim 等）` → `recommended_route=research` → backflow 补充研究；
6. backflow 因结构化数据刷新需求（财务数字）进入 `waiting_human`（D2 设计内拒绝自动 resume）。
**结论：自动资料供给 → 证据 → 分析 → 报告 → 审计 → 质量回路全链真实运行。**（含事件模块的任务会在新闻需求处人工兜底——设计边界，见 §9。）

### 场景 B — 财务研究（宁德时代，财务+业务）
1. 计划含年度/季度报告需求（自动获取解决）+ 财务需求（yoy、毛利率、净利率）；
2. 财务需求缺底层 Observation → `waiting_manual`（structured refresh 语义）；
3. 用户经 `POST /tasks/{id}/financial-observations` 转录 9 条真实数字（2022-2024 营收/归母净利/营业成本，引文含精确数字 token）→ 201；
4. resume（K1）→ 财务需求解析 → 确定性计算（yoy/absolute_change/margin）→ 财务 claims（numeric-literal guard 实测拦截模型输出并最终对齐）；
5. 全链推进至 synthesis（temporal evidence gate 对无发布日期的转录卡提出后续要求，见 §9）。
**结论：真实结构化财务数据供给链（人工转录 → Tier-4 证据 → 确定性计算 → claims）验证通过；LLM 数字边界按设计工作。**

### 场景 C — 人工兜底（比亚迪，业务+风险）
1. 任务 → `waiting_manual`（资料不足，真实 fallback 状态）；
2. 下载真实公告 PDF（EM 反爬握手，115KB，%PDF 校验）→ **本地上传（无 source_url）→ 200，acquisition_method=user_upload**；
3. 自动 prepare：parse ✓ → chunk ✓ → index ✓；
4. resume（K1）→ 200，研究继续推进。
**结论：无 URL 上传 + 自动资料处理 + 继续研究闭环验证通过。**

## 八、最终门禁明细

- `ruff check app tests` ✅；`ruff format --check app tests` ✅；`pip check` ✅；
- `pytest -m "not integration"` → **2363 passed**；
- `pytest -m integration tests/integration` → **1087 passed**（真实 PG 5433 + Chroma 8002；含 fresh acceptance、closure 供给链、全部既有回归）；
- `alembic current`=0049（head）；`alembic check` → No new upgrade operations detected；
- 前端：`npm test` 93/93；`npm run typecheck` ✅；`npm run build` ✅；
- Docker fresh volume：迁移 head → bootstrap（registry 14 / master 5543+11098 / issuer domains 5470）→ `/api/v1/health/ready` 200（database/chroma/checkpoint/raw/export 全绿）→ 真实模型场景 A/B/C。

## 九、遗留问题与已知限制

1. **新闻/事件资料**：`news_article` 需求无法自动获取（需原创发布者验证链，生产未启用 GDELT 类发现）。选「事件」模块的任务会在新闻需求处进入 `waiting_manual`（设计内人工兜底；前端已把事件驱动的 news/公告/IR 需求按模块门控，非事件任务不受影响）。
2. **场景 B 尾部**：转录卡 `source_published_at` 为空 → synthesis temporal gate 对「期间跨度证据」提出要求（`SynthesisTemporalEvidenceInsufficient`）。转录 API 暂不接受 published_at；建议后续在表单增加「报告发布日期」字段。
3. **Stage4 重试**：财务/综合分析的 LLM 输出合规性随调用波动，偶发需一次人工 retry（orchestration 的 retry action；hard 边界按设计不自动改写）。
4. **Stage5 writer**：validation 违规有界重试一次（correction_hint）；仍违规（如数字/绑定反复违反）→ 拒绝 0 写，需重试。
5. **基础设施网络**：huggingface.co / download.pytorch.org 在部分时段不可达——BGE 已离线固化（镜像 + 宿主缓存挂载 + HF_HUB_OFFLINE），Docker 构建的 torch 层在缓存失效时依赖网络（偶发需重试构建）。
6. **共享 DB 风险（开发环境）**：集成测试与 Docker 栈共用 PG 5433；测试的 `DELETE FROM companies` 会清空主数据（已由启动 self-heal 自动恢复）；最终验收以 fresh volume 为准。
7. **季度报告期间推导**：Q2 季报（半年度）与 Q4（年度）由标题关键词区分；「三季度报告/一季度报告」无「第」字也已覆盖（回退 09-30）。

## 十、后续建议

1. 转录 API 增加 `published_at`（解决 §9.2 temporal gate）；
2. 新闻供给链（原创发布者验证 + 受控新闻源）作为下一阶段候选，补齐事件模块的自动路径；
3. Dockerfile 重排（torch 层前置到 COPY app 之前）以稳定增量构建；
4. 为 Stage4/Stage5 增加「合规性波动自动重试一次」的产品级重试（当前为人工 retry）；
5. 将 `issuer_domains_v1.json` 与 `company_master_v1.json` 纳入定期刷新脚本（build 脚本已就绪）。

---

**最终状态**：工作树干净，9 个收口提交，无 push，v1.0.0 tag 未触碰。V1.1 产品完成。
