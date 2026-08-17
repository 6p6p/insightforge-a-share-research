# InsightForge Production Validation Report

日期：2026-08-17 · 阶段：End-to-End Production Validation & Bug Fix（无新功能）

## 1. 测试环境

- 后端：Python 3.12（conda insightforge）· FastAPI 本地进程（port 8003，EMBEDDING_LOCAL_MODEL_PATH 离线 BGE）
- 前端：Vite dev（5173）+ production build（tsc + vite build）
- 依赖：Docker compose（PostgreSQL 18.4 / Chroma 1.5.9，共享 5433/8002）
- LLM：DeepSeek deepseek-v4-flash（真实 API）
- 数据：空库 bootstrap 验证 + 真实公司运行（全部真实网络获取）

## 2. 测试公司列表

| Company | Result | Failed Stage | Fix |
|---|---|---|---|
| 宁德时代 | ✅ 完整闭环 | —（多轮修复后） | F3/F4/F6/F7 |
| 海康威视 | ✅ 完整闭环 | — | F4/F6 |
| 贵州茅台 | ⚠️ 财务数据提取修复后进 stage5；DeepSeek 当日持续不稳定导致 writer 失败（可 retry） | stage5（外部 LLM 不稳定） | F5 |
| 招商银行 | ⚠️ 半年报未发布（真实 no-lookahead 缺口）→ waiting_manual 正确兜底 | preparing（真实缺口） | — |
| 比亚迪 | ⚠️ 年报 PDF 为扫描件（24 blocks/618 chars）→ 真实解析限制 | preparing（真实缺口） | — |

宁德时代闭环结果（只输入公司名 + 默认参数）：6 来源（3 年报/季报/半年报/IR 材料）→ 38 证据卡 → 8 节报告（经营业绩/资本运作/财务杠杆/存货/原材料价格/盈利质量/风险）→ audit → 人工审查门（research_backflow / structured_data_refresh_required）。

## 3. 修复列表

| # | Symptom | Root Cause | Code Change | Regression Test |
|---|---|---|---|---|
| F1 | 用户报告：表单要求研究周期/模块/问题必填 | 后端 TaskCreateRequest 强制必填 | schema 给周期/模块 AUTO 默认值（近 3 年+全部模块）；前端去掉误导必填红星 | test_create_request_company_only_defaults |
| F2 | 用户报告：docker compose build 无法执行 | CitationDrawer 类型 union 缺 FinancialExtractionProvenance 分支 → TS2322 | 新增 FinancialExtractionProvenanceBlock + union 分支 + import | financial_extraction provenance 渲染财务提取来源追溯（前端） |
| F3 | waiting_manual 任务工作台显示 0 sources/0 evidence（用户以为需手动添加资料） | artifacts API 只从 Stage4/5 checkpoint 取集；无 checkpoint 为空 | _company_evidence_ids fallback（公司级真实证据）+ _combined_sources 支持 financial_extraction 卡 | test_waiting_manual_task_shows_company_evidence |
| F4 | 默认全部模块时 macro/event/news 数据不可得 → 卡 waiting_manual | preparation ready 判定把所有非 context missing 当阻塞 | context/macro/event + news_article/company_announcement/issuer_ir_material 类 document 非阻塞；stage4 跳过空输入模块 | test_prepare_missing_macro_non_blocking / test_prepare_issuer_ir_source_not_found_non_blocking |
| F5 | 贵州茅台 equity_parent 缺失 → 财务计算失败 | 标签变体（茅台用净资产非权益） | 增加标签模式（归属于上市公司股东的净资产） | 手工提取验证（244,637,811,032.18 ✓） |
| F6 | 真实运行中 eastmoney 获取瞬时失败（季度/公告缺失） | _fetch_page 单次请求无重试 | 有界重试 3 次（2s/4s backoff） | 既有 services 测试 99 passed |
| F7 | DeepSeek 当日频繁瞬时 5xx / 输出违规 → workflow failed | LLM 组件重试次数不足 | writer/synthesis/audit/financial/macro 重试 3→5 次；eastmoney fetch 重试 | 更新调用次数断言（3→5） |
| F8 | 重启后首次编排 ConnectionTimeout（上阶段遗留） | 连接池默认 5+10 过小 | pool_size=20/max_overflow=30/pool_timeout=30 | 集成测试全绿 |
| F9 | HF 不可达导致 BGE 无法加载（上阶段遗留） | 模型缓存损坏 + 网络隔离 | embedding_local_model_path 离线加载（modelscope 下载 + 本地目录） | 前端 build + 真实运行 embedding 正常 |
| F10 | financial_extraction 证据 citation 409（上阶段遗留） | provenance resolve 不支持该 origin | FinancialExtractionProvenance + resolve_financial_extraction；load_closure 支持 | test_financial_evidence_provenance_resolves_with_page |

## 4. 重点验证项

- Frontend production build：npm run build（tsc -b + vite）通过；CitationDrawer 三种 provenance（document/macro/financial_extraction）均正常渲染
- Docker 一键启动：docker compose up -d --build 成功（backend + frontend 镜像构建）；4 容器 healthy；backend /health/ready 200（production 环境）；frontend 8080 200
- 首次启动 bootstrap（空库）：migrations → source registry 14 providers → company master 5543 公司/11098 aliases → issuer domains 5470（test_production_docker_export_gate 2 passed）
- DeepSeek 异常：瞬时 5xx → 有界重试（writer/synthesis/audit/financial/macro 5 次）；仍失败 → 稳定错误码 + orchestration failed（用户可 retry），不崩溃
- PDF 异常：正常 PDF（年报/半年报/季报）→ 提取成功；扫描件（比亚迪年报）→ 诚实 waiting_manual；公告/IR 材料 → 有则用，无 evidence 不阻塞

## 5. 最终验证

- 后端单元：2457 passed；集成：1123 passed（含新语义/fallback/docker gate 测试）
- 前端：95 passed + typecheck + production build 通过
- ruff 全量干净；alembic head 0050
- 真实公司完整运行：宁德时代（8 节报告全链路）✅

## 6. Git 状态

- 本阶段提交：f08d9db（表单默认值/fallback/CitationDrawer）· 190c268（非阻塞语义/重试）· 3a45790（测试对齐）等，工作树 clean
- 未 push / merge / tag

## 已知限制

- DeepSeek API 当日不稳定（约 10-15% 瞬时错误率）→ 5 次有界重试后仍可能失败（茅台本轮 stage5 writer），用户 retry 一次即可
- 招行半年报未发布 / 比亚迪年报扫描件：真实数据可得性问题，系统诚实 fallback 到人工环节（不伪造）
- audit 对报告质量判定严格（research 路由）→ 无进展时停在人工门（设计特性，非限制）
