# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统。

> 当前进度：阶段 2A 基础（公司标准身份 + Source Registry）、阶段 2B.1（原始文件归档 + 来源登记）、阶段 2B.2A（官方披露发现契约 + 可行性探测）、阶段 2C.1（Macro Provider 契约 + World Bank Indicators Provider，实现与自动化测试已完成、真实验收待网络环境）、阶段 2C.2A（宏观持久化数据模型 + RawArtifact JSON 泛化）、阶段 2C.2B（原始响应捕获 + Snapshot Fingerprint + 事务化持久化）、阶段 2D.1（News Discovery 基础 + GDELT DOC 2.0 Discovery Provider，实现、自动化测试与 Docker 重建验收已完成、真实验收待环境，见下）、阶段 2D.2A（原始新闻来源核验 + Safe HTML 归档，实现、自动化测试、Docker 重建验收与受控 HTML 传输验收均已完成）、阶段 2E.1（确定性 HTML 解析 → ParsedSource / ParsedBlock 快照，实现与自动化测试已完成）、阶段 2E.2（确定性 PDF 解析 + 页面定位 → pdf_layout v2，实现与自动化测试已完成）、阶段 3A（确定性文档分块 → ChunkSet / DocumentChunk，block_window v1）、阶段 3B.1（BGE Embedding + Chroma 向量索引基座，real BGE 验收通过）、阶段 3B.2（Filtered Vector Retrieval，语义检索 read path）、阶段 3C.1（EvidenceCard Provenance）、阶段 3C.2（Structured Evidence Extractor）、阶段 3C.3A（Generic Evidence Origin + Macro Evidence，EvidenceCard 双 origin：document_chunk / macro_observation）与 3C.2.1（生产 LLM runtime：DeepSeek adapter，真实 DeepSeek V4 Flash smoke 已通过）**已实现**（各阶段自动化测试通过，live acceptance 详见对应章节），阶段 1B/1C 提供 LangGraph 模拟工作流基础。核心证据链中的 **Claim（阶段 4A：Claim Provenance + Persistence Foundation，`claims` / `claim_evidence_links` 确定性登记与回放，不调用 LLM、不接 Analyst Agent）已实现**；**阶段 4B.1（Structured Claim Analysis Foundation + Business / Event / Risk Claim Analysis）已实现**：EvidenceCard[] + research question + analysis domain → DeepSeek 结构化分析 → 确定性 E-ref resolution → `ClaimService.create_claim_batch` 原子登记 Claim（只支持 business / event / risk；financial / macro / valuation → `ClaimAnalysisDomainNotReady`；真实 DeepSeek V4 Flash smoke 通过，LLM 仅用于受控 smoke，不进入自动化测试）；**阶段 4B.2A（Financial Metric Observation Foundation）已实现**：把来源于真实财务 Evidence 的**原始财务数值**确定性登记为 `FinancialMetricObservation`（migration 0020；source_value_text 必须是 Evidence quote 的 exact substring、`Decimal` 零 float 解析、单位归一化到 CNY、fingerprint/replay 幂等、provenance 可回溯到 RawArtifact；**0 LLM / 0 Chroma / 0 Claim / 0 Report 表**）；**阶段 4B.2B（Deterministic Financial Calculation）已实现**：把已登记 Observation 通过**冻结公式**计算为派生财务事实（absolute_change / YoY / QoQ / margin / ratio，migration 0021；全程 `Decimal`、除法 quantize 到 `CALCULATION_SCALE=12` `ROUND_HALF_EVEN`、同期间期 period 规则、fingerprint/replay 幂等、并发 `ON CONFLICT`、0 partial write；**0 LLM / 0 Chroma / 0 Claim / 0 Report 表**）；**阶段 4B.2C.1（Financial Claim Provenance）已实现**：把引用已登记 `FinancialCalculation` 的 Financial Claim 确定性登记为 Claim + 自动展开的 Evidence 链接 + Calculation 链接（migration 0022；Claim → ClaimFinancialCalculationLink → FinancialCalculation → Observation → EvidenceCard → Source；v2 fingerprint、Calculation integrity replay、relation conflict、critical policy；**0 LLM / 0 Chroma query / 0 Retrieval / 0 LangGraph / 0 Report 表**）；**阶段 4B.2C.2（Structured Financial Analysis）已实现**：把 4B.2C.1 的 provenance 基础接上 LLM，`Financial Calculation[] + research question → DeepSeek 结构化分析 → FinancialClaimCandidate[] → 确定性 alias/ref resolution → v3 Financial Claim`（numeric-literal guard、analyst 身份、atomic batch 持久化；真实 DeepSeek 受控 smoke 通过，LLM 不进入自动化测试）——**4B.2 = FINAL**；**阶段 4C.1A（Macro Transmission Provenance）已实现且 = FINAL（v2 Closeout）**：把 Macro Evidence + Company Exposure Evidence 通过 Macro Transmission Chain 登记为 Macro Claim（migration 0023 + 0024；origin 按角色校验（v2 允许 external event document 作 macro_driver）、information availability no-lookahead（published_at/acquired_at/fetched_at）、time-alignment / overclaim policy v2、replay 版本感知强一致；transmission_fingerprint 非 global identity，同语义 + 不同 statement/analyst → 新 Claim + 新链；**0 LLM / 0 Chroma / 0 Retrieval / 0 LangGraph / 0 Report 表**）；**阶段 4C.1B（Structured Macro Context Analyst）已实现**：把 4C.1A 的传导 provenance 接上 LLM，`Macro Evidence + Company Evidence + research question → DeepSeek 结构化 Macro Context 分析 → MacroClaimCandidate[] → 确定性 M/E alias + numeric-literal guard + ref resolution → v6 Macro Claim`（migration 0025 Gate 0：`macro_transmission_chains.analysis_as_of` 查询列，**不 backfill / 不反推历史 cutoff**；analyst 身份、atomic batch 持久化；真实 DeepSeek V4 Flash 受控 smoke 通过，LLM 不进入自动化测试）——**4C.1A = FINAL / 4C.1B = completed**。Report → Audit、真实 Agent、业务研报生成、4C.2 Valuation 与前端**尚未实现**（4B.2 = FINAL，4C.1A = FINAL，4C.1B = completed）：不自动抓取公告、不同步公司目录、不做扫描件/OCR PDF 识别；Evidence Extractor 的 LLM 调用已提供生产 adapter（DeepSeek，`deepseek-v4-flash`，显式关闭 thinking）但尚未接入自动提取流程。当前 FastAPI 应用提供健康检查、研究任务、模拟工作流、来源登记与原始文件归档接口，并具备 PostgreSQL 与 Chroma 的持久化基础设施。

## 目录职责

```
backend/            Python 后端（FastAPI）工程，依赖由 backend/pyproject.toml 管理
backend/alembic/    Alembic 迁移（raw_artifacts / source_records 等业务表）
.data/              （gitignore）本地原始文件归档根目录，默认项目根 .data/raw，由 RAW_STORAGE_ROOT 指定
frontend/           （预留）React + TypeScript 前端入口，当前为空
docs/decisions/     ADR（架构决策记录）
docker/             Dockerfile 等镜像构建文件
scripts/            （预留）开发/运维脚本
compose.yaml        PostgreSQL + Chroma + backend 三服务编排
environment.yml     Conda 基础环境定义（仅解释器与 pip）
```

## Docker 依赖服务（PostgreSQL + Chroma）

```bash
docker compose up -d postgres chroma
```

- PostgreSQL：宿主机 `${POSTGRES_HOST_PORT:-5433}` → 容器 5432
- Chroma：宿主机 `${CHROMA_HOST_PORT:-8002}` → 容器 8000

普通停止（**不删除 volume**）：

```bash
docker compose stop
```

> ⚠️ 以下命令会**删除 named volume 与全部数据**，仅在确认要清库时使用，不作为默认命令：
>
> ```bash
> docker compose down -v
> ```

## 本地运行 backend + Docker 依赖

1. 创建并激活 Conda 环境：

```bash
conda env create -f environment.yml
conda activate insightforge
```

2. 安装 backend 开发依赖：

```bash
conda run -n insightforge python -m pip install -e "./backend[dev]"
```

3. 复制本地配置（如果不存在 `.env`）：

```bash
cp .env.example .env   # Windows cmd 下使用：copy .env.example .env
```

4. 启动 Docker 依赖，执行迁移：

```bash
docker compose up -d postgres chroma
conda run -n insightforge alembic -c backend/alembic.ini upgrade head
```

5. 启动 FastAPI（Windows host 官方入口）：

```bash
cd backend
conda run -n insightforge python -m app.cli.run_backend
```

> **为什么不用 `python -m uvicorn app.main:app`？** Windows 上需要**两个条件同时
> 成立**才能让 psycopg async 的 `/api/v1/health/ready`（database / checkpoint）
> 通过：1）在 `uvicorn.run` **之前**显式调用 `configure_asyncio_runtime()`
> （否则 uvicorn 先创建默认 ProactorEventLoop，`app.main` 模块级的配置已来不及
> 生效）；2）`uvicorn.run(..., loop="none")`——uvicorn 0.52 的
> `asyncio_loop_factory` 在 Windows 上直接 new `ProactorEventLoop`
> （`loop="auto"` 默认值），会绕过事件循环 policy，只有 `loop="none"` 才让
> uvicorn 退回 `asyncio.run()` 用已配置的 policy 创建 loop。`app.cli.run_backend`
> 已同时满足这两点。Docker 入口保持 `compose.yaml` 的 uvicorn 命令（Linux 容器
> 无此问题）。

## 健康检查语义

- `GET /api/v1/health/live` → 进程可响应即 `200 {"status":"ok"}`，不检查任何外部依赖。
- `GET /api/v1/health/ready` → 检查 `configuration`、`database`、`chroma`、`checkpoint`、`raw_storage` 五项：
  - 全部 `ok` → `200 {"status":"ok"}`
  - 任一 `error` → `503 {"status":"not_ready"}`，对应检查项为 `error`

- http://127.0.0.1:8001/api/v1/health/live
- http://127.0.0.1:8001/api/v1/health/ready
- API 文档：http://127.0.0.1:8001/docs

## 研究任务 API

当前已支持研究任务的创建与查询；**创建任务不会自动开始研究**，任务先落库为 `pending`（LangGraph 编排、Agent 与资料采集尚未实现）。

创建任务：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/tasks \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: task-20260806-001" \
  -d '{
    "company_query": "600519",
    "research_start_date": "2023-01-01",
    "research_end_date": "2025-12-31",
    "modules": ["company_profile", "business", "financial", "risk"],
    "questions": ["公司收入增长主要由哪些因素驱动？"],
    "include_relative_valuation": false,
    "require_plan_approval": true
  }'
```

- 新建成功：`201`。
- 相同 `Idempotency-Key` + 相同请求内容重放：`200`，响应头 `Idempotent-Replayed: true`。
- 相同 `Idempotency-Key` + 不同请求内容：`409`（idempotency_conflict）。

查询任务：

```bash
curl http://127.0.0.1:8001/api/v1/tasks/替换为task_id
curl "http://127.0.0.1:8001/api/v1/tasks?status=pending&limit=20&offset=0"
```

以上示例仅用于演示接口，不构成任何投资建议。

## LangGraph 模拟工作流（阶段 1B/1C 基础）

已建立 LangGraph 确定性模拟工作流、PostgreSQL Checkpoint 与后台执行基础。**它不是实际公司研究**——不采集资料、不生成研报、不接入 LLM，仅验证工作流编排、状态持久化与事件推送。当前为**单进程开发实现**，不是分布式任务队列。

初始化 LangGraph Checkpoint 表（vendor 表，由 langgraph-checkpoint-postgres 管理，不属于 Alembic）：

```bash
conda run -n insightforge python -m app.cli.setup_checkpointer
```

对已有研究任务执行一次模拟工作流（CLI）：

```bash
conda run -n insightforge python -m app.cli.simulate_workflow --task-id 替换为task_id
```

通过 HTTP 创建并后台启动模拟 WorkflowRun（返回 202 与 pending run）：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/tasks/替换为task_id/runs
```

查询 WorkflowRun：

```bash
curl http://127.0.0.1:8001/api/v1/workflow-runs/替换为run_id
```

订阅事件流（SSE）：

```bash
curl -N http://127.0.0.1:8001/api/v1/workflow-runs/替换为run_id/events
```

断线重连增量回放（可选 `Last-Event-ID` 请求头，值为上次收到的 `event_id`；注意 `event_id` 单调但可能有间隙，只用作游标）：

```bash
curl -N -H "Last-Event-ID: 3" http://127.0.0.1:8001/api/v1/workflow-runs/替换为run_id/events
```

### 人工审批、取消与重试

当任务设置了 `require_plan_approval=true`（数据库默认 true）时，工作流在计划生成后进入 `waiting_human` 等待人工确认（`waiting_human` 不是终态）。

审批计划：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/workflow-runs/替换为run_id/actions \
  -H "Content-Type: application/json" -d '{"action_type":"approve_plan"}'
```

取消（允许 pending/running/waiting_human）：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/workflow-runs/替换为run_id/actions \
  -H "Content-Type: application/json" -d '{"action_type":"cancel"}'
```

重试（仅 failed/cancelled，创建新的 run/thread）：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/workflow-runs/替换为run_id/actions \
  -H "Content-Type: application/json" -d '{"action_type":"retry"}'
```

backend 重启协调（当前单实例方案）：遗留的 `pending/running` run 在启动时被标记为 `failed`（error_code=worker_restarted）；`waiting_human` run 可用同一 run_id/thread_id 继续审批恢复。

模拟工作流**不会改变 ResearchTask 状态**（保持 pending），也不会产生资料、证据或报告记录。

## 公司身份与来源策略（阶段 2A 基础）

当前新增公司标准身份（CompanyIdentity）与 Source Registry 基础。**尚未自动同步 A 股公司目录、未抓取公告、未实现宏观数据 API、未实现大模型联网搜索**。

登记默认来源 Provider（幂等，可重复执行）：

```bash
conda run -n insightforge python -m app.cli.seed_source_registry
```

解析公司查询（精确匹配；多交易所代码返回 ambiguous）：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/companies/resolve \
  -H "Content-Type: application/json" -d '{"query":"SSE:600519"}'
```

查询来源 Provider：

```bash
curl "http://127.0.0.1:8001/api/v1/source-providers"
curl "http://127.0.0.1:8001/api/v1/source-providers?capability=regulation"
```

来源权威等级（authority_tier）与获取方式（acquisition_method）分离；大模型联网搜索只作为发现方式，不作为事实来源。

## 来源登记与原始文件归档（阶段 2B.1）

新增原始文件归档与来源登记。**本阶段不解析 PDF 正文**：只做不可变字节归档与来源登记，不创建 DocumentChunk、Evidence、Claim 或 Report；**不主动执行外网请求**（不抓取公告、不同步公司目录），URL 导入仅在用户显式调用接口时发生。

- RawArtifact：不可变字节归档，SHA-256 内容寻址唯一（存储布局 `sha256/ab/cd/<hash>.pdf`，原子写入、不覆盖）。
- SourceRecord：一次来源登记，引用 artifact_id；同一原始文件可被多个来源记录共享。
- 判定 replay：相同 `(provider_key, source_url, artifact_id)` 返回已存在记录（HTTP 200 + `Source-Replayed: true`）；不同 URL 同内容则新建来源记录共享同一 artifact。
- URL 导入使用受限 fetcher：仅接受来源注册表校验过的域名，重定向重新校验、流式 + 大小双上限，不访问外网测试（httpx MockTransport）。

环境变量（`RAW_STORAGE_ROOT` 指向的目录需可写，volume 见 compose.yaml）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RAW_STORAGE_ROOT` | 项目根 `.data/raw` | 原始文件归档根目录 |
| `SOURCE_MAX_FILE_SIZE_BYTES` | `104857600` | URL 导入/上传单文件字节上限 |

上传 PDF（multipart）：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/source-records/upload \
  -F "company_id=替换为company_id" \
  -F "provider_key=sse" \
  -F "document_type=annual_report" \
  -F "title=2025 年度报告" \
  -F "source_url=https://www.sse.com.cn/example.pdf" \
  -F "file=@./example.pdf"
```

- 新建成功：`201`；replay（同内容同 URL 已存在）：`200` + `Source-Replayed: true`。

按安全 URL 导入 PDF（JSON）：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/source-records/import-url \
  -H "Content-Type: application/json" -d '{
    "company_id": "替换为company_id",
    "provider_key": "sse",
    "document_type": "annual_report",
    "title": "2025 年度报告",
    "source_url": "https://www.sse.com.cn/example.pdf"
  }'
```

查询与下载：

```bash
curl http://127.0.0.1:8001/api/v1/source-records/替换为source_id
curl "http://127.0.0.1:8001/api/v1/companies/替换为company_id/source-records?document_type=annual_report&limit=20&offset=0"
curl -o source.pdf http://127.0.0.1:8001/api/v1/source-records/替换为source_id/content
```

## 官方披露发现契约与探测 CLI（阶段 2B.2A）

阶段 2B.2A 建立了官方公告"发现"契约与受控可行性探测工具。**探测 CLI 是保守的开发期诊断工具**：只访问 Source Registry 已登记且启用的 Provider（当前仅 sse、cninfo），受控请求数（单 Provider ≤6、整次 ≤12），仅访问官方 allowlist 内的 https URL，不使用 Cookie/Auth、不执行 JS、不逆向内部接口、不调用内部数据服务接口（如 webapi.cninfo.com.cn）。探测结果只反映探测当时的公开通路形态，**不宣称已实现自动公告采集，也不宣称 SSE / CNINFO 不可用**——只说明"尚未确认合规自动通路"。

```bash
conda run -n insightforge python -m app.cli.probe_disclosure_sources \
  --providers sse,cninfo \
  --security-code 600519 \
  --start-date 2026-01-01 \
  --end-date 2026-08-07
```

- 输出 JSON 到 stdout，不写数据库、不下载/保留响应正文；`request` 字段如实记录探测目标（security_code / 日期窗口）。
- 候选识别基于页面真实链接（security_code + 非空标题 + 日期文本 + urljoin 后 allowlist 校验）；日期/公司筛选未送入合规查询入口时 `search_request_applied=false`，不伪造候选。
- **PDF 探测使用流式 GET** 只读取前 8192 字节文件头验证（2xx + Content-Type `application/pdf` + Content-Length ≤ 10 MiB + `%PDF-` 签名），不下载正文；旧 `client.get()` 会在返回前读取完整正文的缺陷已修复。
- 接入形态决策采用严格不变量（7 条优先级）：`public_server_rendered_html` 必须 `search_request_applied=true`；`direct_pdf_verified=false` 不得判为 `public_direct_pdf_only`；页面可达但本次未确认按公司/日期自动发现时保守判为 `discovery_not_confirmed`（不表述为"不可用"或"需要 JS/内部接口"）；仅出现"登录/注册"不得判为需要认证。
- 自动发现仅限 `documented_api` 与 `public_server_rendered_html` 两种接入形态；未确认合规通路的形态不实现生产 Adapter。
- **收口 Probe（2026-08-07）**：SSE 与 CNINFO 首页均可达（HTTP 200），但 `search_request_applied=false`、`direct_pdf_verified=false`、`matching_candidate_count=0`，两者均判为 `discovery_not_confirmed`；总请求 2，`selected_candidate_provider=null`，未满足 2B.2B 启动门槛（暂缓），继续依赖用户上传 + URL 导入 + 后续网络搜索发现兜底。
- 探测边界、决策规则与人工验收门槛见 [docs/decisions/0010-official-disclosure-discovery-policy.md](docs/decisions/0010-official-disclosure-discovery-policy.md)。

## 宏观数据 Provider（阶段 2C.1）

阶段 2C.1 建立宏观数据领域契约与第一个 Provider（World Bank Indicators API V2）。**状态拆分：implementation：completed / automated tests：completed / live external acceptance：pending**——网络阻断不是代码失败，允许离线推进 2C.2A；真实验收跑通前不开放生产宏观采集、不把 Macro Snapshot 视为 Evidence、不进入 Claim/Report。**当前 2C.1 只是"获取"层**：不写数据库、不创建表、结果不是 Evidence/Claim/Report，也不进入 LangGraph 编排；只有阶段 2C.2（2C.2A + 2C.2B）完成持久化、Provider 快照与原始 JSON 归档后，宏观数据才能进入证据链。**尚未实现 FRED 与国内官方宏观数据（NBS 等）**。

- 只支持 `annual` 频率与单一 `country` 地理类型（`country_code` 拒绝 `ALL`）；年份闭区间最多 60 年。World Bank 允许 WLD/LCN/HIC 等聚合代码出现在 country 路径，解析国家元数据时若 `region.value` 规范化后等于 `Aggregates` 或缺失/无法确定，保守拒绝为 `geography_not_country`。
- 固定官方 Indicators API V2（`api.worldbank.org/v2`）+ 数据源 `source=2`（World Development Indicators）；客户端固定接口模板，不接受任意 endpoint/query 参数；`MacroFetchResult.source_id` 固定 `"2"`。
- 数值使用 Decimal 全程（`parse_float=Decimal` + 显式拒绝 NaN/Infinity 字面量），不做单位换算或同比/环比；缺失年份保留为 `is_missing=true`，不补齐缺失年份；观测 `period` 为 Provider 年份标签，`normalized_period_start` 固定该年 1 月 1 日（仅用于排序/索引，不表示真实统计周期起始日）。
- 请求预算 `per_page=1000`、2 次元数据请求 + 观测分页 ≤ 20、观测分页上限 18；受控客户端仅 https、URL 通过 Source Registry allowlist（子域匹配）、无 Cookie/Auth/Header、手动重定向（同 allowlist ≤3 次、跨域拒绝）、单响应上限 5 MiB。
- 国家身份一致性：country metadata 校验返回国家与请求一致——两字母（ISO2）请求对 `iso2Code`、三字母（ISO3）请求对 country id（即 iso3），不匹配报 `malformed_response`，不按名称猜测国家映射；`MacroFetchResult` 是跨对象一致性边界，构造时强制 Query/Indicator/Geography/Observation/Provider/Source 六者一致（不要求 `query.country_code` 直接等于 `observation.geography_code`，如 ISO2 请求 CN→observation 地理代码 CHN 合法）。
- 开发期 CLI `fetch_world_bank_macro`（需 Source Registry 已登记 `world_bank` Provider）：

```bash
conda run -n insightforge python -m app.cli.fetch_world_bank_macro \
  --country CHN \
  --indicator SP.POP.TOTL \
  --start-year 2020 \
  --end-year 2024
```

  - 输出 JSON 报告到 stdout（日志走 stderr），Decimal 输出为字符串，**不写数据库、不保存响应正文、不写本地文件**；
  - 退出码：0 成功 / 2 输入错误 / 3 Provider 配置错误 / 4 API/网络/响应错误。

> **受控真实验收说明（2026-08-07，2C.1.1/2C.1.2 收口）**：本机网络对 `worldbank.org` 存在域名级出口阻断（DNS 被劫持到合成地址、TLS 握手被丢弃），真实请求在本环境返回 `{"error":"request_failed","message":"World Bank API request failed"}`（exit 4，稳定非空错误、不泄漏底层细节），CLI 未跑通成功路径。上述命令与断言不变量已记录在 [docs/decisions/0011-macro-provider-and-world-bank.md](docs/decisions/0011-macro-provider-and-world-bank.md)，可在具备 World Bank 出网的环境补跑；跑通前 2C.1 的 **live external acceptance 保持 pending**（不影响离线推进 2C.2A，见下）。

## 宏观数据持久化数据模型（阶段 2C.2A）

阶段 2C.2A 建立了宏观数据持久化的数据模型与原始 JSON 归档路径，为 2C.2B 提供表结构与 Repository 契约（2C.2B 捕获/指纹/Service 见下节）。**2C.2A 本身不实现 MacroPersistenceService、不创建 Macro API、不把 Macro 数据接入 Evidence/Claim，不接入 LangGraph/LLM/Agent/RAG/Chroma；没有已持久化的真实 World Bank 数据**。

- RawArtifact 媒体类型从 PDF-only 泛化为 PDF+JSON：既有 PDF 行为、storage_key（`sha256/ab/cd/<hash>.pdf`）与全部 PDF 测试保持不变；`application/json` 仅用于 Macro 原始响应归档，不包装成 SourceRecord（SourceRecord 仍保持 company-bound、PDF-only 语义）。
- `LocalRawArtifactStore` 新增 JSON 原始字节归档（`sha256/ab/cd/<hash>.json`）：非空、上限 `MACRO_MAX_JSON_RESPONSE_BYTES`（默认 5 MiB）、UTF-8（允许 BOM）、合法 JSON、`parse_float=Decimal`、拒绝 NaN/Infinity 字面量、保存原始字节（不重新序列化/不格式化/不改键序）、SHA-256 基于原始字节、随机临时文件 + flush/fsync + 原子移动、相同内容复用。
- 四张 Macro 业务表（migration 0009）：`macro_series`（provider_key/source_id/external_indicator_id/geography_type/geography_code/frequency 六字段稳定身份，UNIQUE）、`macro_dataset_snapshots`（一次获取快照，`snapshot_fingerprint` 唯一）、`macro_snapshot_artifacts`（原始响应与快照的关联，role/page 语义）、`macro_observations`（NUMERIC 值 + 缺失语义）；对应 Repository 使用 PostgreSQL ON CONFLICT 并发去重、稳定排序、不 commit。
- 决策记录：[docs/decisions/0012-macro-snapshot-persistence-model.md](docs/decisions/0012-macro-snapshot-persistence-model.md)。

环境变量（新增）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MACRO_MAX_JSON_RESPONSE_BYTES` | `5242880` | Macro 原始 JSON 响应单文件字节上限（1 KiB—20 MiB） |

## 宏观数据捕获、指纹与持久化 Service（阶段 2C.2B）

阶段 2C.2B 已实现"原始响应捕获 → 内容寻址归档 → Snapshot Fingerprint → 事务化持久化 → 并发幂等 → replay 完整性检查"的完整链路。**状态：2C.2B completed（2026-08-07，含 2C.2B.1 最终验收：`macro_series` UNIQUE 六字段核实、migration 0009 downgrade JSON 防护验证、hostname validation、JSON-only Artifact 冲突防线、事务原子性故障注入、replay 完整性；完整工程验收通过）。本阶段不创建 Macro API、不把 Macro 数据接入 Evidence/Claim、不接入 LangGraph/LLM/Agent/RAG/Chroma；没有持久化的真实 World Bank 数据；不开始新功能**。

- `MacroRawJsonResponse` 冻结原始响应（role/page/2xx 状态/裸 hostname/`application/json`/非空且 ≤ 5 MiB/时区感知，构造时 8 项校验）；`fetch_with_capture` 响应顺序固定 indicator → country → observations pages。
- `validate_captured_macro_fetch` 11 项完整性校验（元数据各恰一条、分页完整 1..pages、总数 = 2+pages、hostname/content-type/source_id/provider_key），失败在文件/DB 写入前拦截。
- 原始响应先内容寻址归档（`LocalRawArtifactStore.put_json_bytes`，文件 I/O 先于 DB transaction）；孤儿文件保留等待后续 GC。
- Snapshot Fingerprint v1：canonical JSON + SHA-256（golden vector 固定），排除 `fetched_at`/`request_count`（重复获取可 replay）、输入顺序无关、基于归档 artifact 的 content SHA-256。
- `MacroPersistenceService`（`persist_captured_fetch` / `fetch_and_persist`）严格写入顺序 A-K：网络 I/O 不持有 AsyncSession；并发幂等（`ON CONFLICT DO NOTHING`，仅赢家写 Links/Observations）；replay 完整性检查失败抛 `MacroSnapshotIntegrityError`，不自动修复。
- migration 0010 新增 `fingerprint_version` / `normalization_version` + CHECK（已应用，`alembic current` = 0010 head）；**不创建 RetrievalAttempt 表**（设计决策见 ADR-0013）。
- 决策记录：[docs/decisions/0013-macro-captured-persistence-service.md](docs/decisions/0013-macro-captured-persistence-service.md)。

## 新闻发现（阶段 2D.1）

阶段 2D.1 建立**发现（Discovery）与事实来源（Source）分离**的 News Discovery 基础，并实现第一个 discovery-only 新闻候选 Provider（GDELT DOC 2.0）。**状态（四维，2026-08-07）：implementation = completed / automated_tests = completed / docker_rebuild_acceptance = completed（`docker compose build backend` 成功重建含当前工作区代码的新镜像；`up -d` 后容器 healthy，`/api/v1/health/live` 200、`/api/v1/health/ready` 200 且五项 checks——configuration/database/chroma/checkpoint/raw_storage——全部 ok）/ live_external_acceptance = pending（本机对 `api.gdeltproject.org` 的单次受控真实请求发生 ConnectTimeout；跑通前不开放生产新闻发现、不把 Discovery Candidate 视为 Evidence，见下）**。

- 通用 News Discovery 契约：`NewsDiscoveryQuery`（company_id/query_text/start_at/end_at/max_results，8 条校验规则）与 `NewsDiscoveryCandidate`（rank/title/discovered_url/normalized_url/domain/seen_at/engine，10 条规则；URL 确定性 normalization：默认端口删除、fragment 删除、IDNA hostname、不删 utm）；`NewsDiscoveryProvider` Protocol + `NewsDiscoveryResult` + `NewsRawDiscoveryResponse`。
- `GdeltNewsDiscoveryProvider`（`NewsDiscoveryEngine.GDELT_DOC`）：固定 endpoint `https://api.gdeltproject.org/api/v2/doc/doc`，仅 `mode=artlist&format=json&sort=datedesc&maxrecords=1..100&startdatetime&enddatetime`（UTC `YYYYMMDDHHMMSS`）；安全 HTTP 22 条规则（仅 https、固定 hostname、`trust_env=false`、无 Cookie/Auth/API Key、手动重定向 ≤3 次且 hostname 不变、不自动重试、429/5xx 稳定错误、5 MiB 流式上限、Content-Type 必须 `application/json`、JSON 显式拒绝 NaN/Infinity、日志脱敏——只记 provider_key/hostname/status/duration_ms/error_type）。
- 宽容 Parser：缺 url/title、非法 URL、日期无法解析的单条**跳过**（不可信候选，不让整个查询失败）；顶层非 object / articles 非 list / 容器结构不符 → `GdeltMalformedResponse`（**malformed response ≠ invalid_json**：JSON 解析层失败才是 `GdeltInvalidJson`，结构不合法是 `GdeltMalformedResponse`）；domain 由 normalized URL hostname 派生、不盲信 Provider 字段；同一 normalized_url 去重；rank 从 1 重排。
- **Discovery Run / Discovery Candidate 持久化**（migration 0011，已应用）：`news_discovery_runs`（engine/query/时间窗/max_results/raw_artifact 引用 + 冗余响应元数据/query_fingerprint UNIQUE）与 `news_discovery_candidates`（rank/title/discovered_url/normalized_url/url_sha256/domain/seen_at/verification_status=unverified，`(run, rank)` 与 `(run, normalized_url)` 唯一）。GDELT 原始 JSON 搜索响应归档为 RawArtifact（内容寻址，SHA-256）。
- `NewsDiscoveryPersistenceService.discover_and_persist` A-H：网络 I/O 不持有 AsyncSession → 原始响应先落盘 → 短 DB transaction → raw artifact get_or_create → query fingerprint → run create-or-get（`ON CONFLICT DO NOTHING`，并发幂等）→ replay 完整性检查（候选数不符抛 `NewsDiscoveryIntegrityError`）→ 仅赢家写 Candidates → commit。
- **RawArtifact 内容寻址强一致（artifact conflict 边界）**：get_or_create 后校验既有一行的 `media_type == application/json`、`content_sha256`、`byte_size`、`storage_key` 四项与本次落盘描述完全一致；任一不一致（如同一 SHA-256 已被 PDF 占用）抛 `NewsDiscoveryArtifactConflict`，**不创建任何 Run/Candidate**（raw 内容寻址文件可存在，不要求删除）。
- query fingerprint v1：engine + company_id + query_text + UTC 时间窗 + max_results + raw response sha256 的 canonical JSON SHA-256（golden vector 固定）；重复完全相同的发现响应 replay 到同一 run。
- 测试：**78 项 News non-integration 测试**（Contracts 35 / GDELT Client 16 / Parser 18 / Fingerprint 9）+ **11 项 MockTransport E2E 集成测试**（含 artifact conflict 3 项、SourceRecord count 不变化 1 项），全部通过；pytest Network Guard（conftest autouse）拦截非回环真实网络——该 guard 仅由自动化测试证明，真实 Probe 不经过它。
- **GDELT 不进 Source Registry**：`source_providers` seed 禁止 `gdelt`/`gdelt_doc`/`openai`/`chatgpt`/`search_engine`；GDELT 不是 Tier 3/4 SourceProvider、不创建 SourceRecord/Evidence/Claim。
- **重要现实限制**：GDELT 不是中文全文搜索的可靠替代，已实现的是**第一种 discovery-only 新闻候选 Provider**——它只产生待核验的候选 URL 线索，不代表系统现在可以完整搜索 A 股新闻。本阶段不下载新闻正文、不解析 HTML、不把 Candidate 当 Source、不用 LLM、不接 LangGraph；2D.2A / 2D.2A.1 已完成 Candidate → 原创发布者核验与 HTML 归档（见下节），2D.3（Model Web Search fallback + Discovery Router）尚未开始（2D.2B 的正文解析职责已移除，由 2E——2E.1 HTML / 2E.2 PDF 承接，Evidence 管线属 Stage 3）。
- **Probe 表述修正（2026-08-07）**：对 `api.gdeltproject.org` 的单次受控真实请求发生 **ConnectTimeout**，因此 live external acceptance 保持 pending。该真实 Probe **不经过 pytest Network Guard**；guard 由自动化测试单独证明。Probe 错误路径日志只验证了 **failure-path log redaction**（provider_key/hostname/error_type 已记录、query_text/URL 未记录），**不能宣称成功响应路径已经真实验证**。
- 决策记录：[docs/decisions/0014-news-discovery-and-gdelt-provider.md](docs/decisions/0014-news-discovery-and-gdelt-provider.md)。

## 新闻原始来源核验（阶段 2D.2A）

阶段 2D.2A 建立确定性链路 `NewsDiscoveryCandidate → Original Publisher → Safe HTML fetch → RawArtifact(text/html) → SourceRecord → NewsSourceVerification`。**状态（四维，2026-08-07）：implementation = completed / automated_tests = completed / docker_rebuild_acceptance = completed（`docker compose build backend` 成功重建含当前工作区代码的新镜像；`up -d` 后容器 healthy，`/api/v1/health/live` 200、`/api/v1/health/ready` 200 且五项 checks——configuration/database/chroma/checkpoint/raw_storage——全部 ok）/ live_html_transport_acceptance = completed（对 `www.xinhuanet.com` 的单次受控真实 HTTPS 传输请求成功，见下）；real_article_e2e_acceptance = not performed / not required for 2D.2A code acceptance（真实文章端到端由自动化测试覆盖：真实 PostgreSQL + MockTransport + FakeHostResolver，见测试段）**。

- **verified 语义（不变量 D）**：Candidate URL 属于 Source Registry 登记的原创媒体、公开 HTML 被安全获取、raw HTML 已不可变归档、Candidate → SourceRecord 溯源已建立。它**不代表**内容真实 / 已交叉验证 / 支持关键声明 / 是 Evidence——新闻真实性判定属于后续 Evidence 管线。
- **不变量 A-H**：A Discovery Provider ≠ Source Provider（GDELT 永不进入 SourceRecord）；B Candidate URL 不直接请求，必经 Resolver → Registry → SafeHtmlFetcher；C 只有 enabled + news_article + public_html 的 Provider 有资格成为 original publisher；E Media tier 3、critical_claim_eligible=false；F seen_at 永不是 published_at（本阶段一律 NULL）；G SourceRecord.provider_key 必须是 xinhuanet / cnstock / cs_com_cn；H source_url 用 final URL，discovery URL 保留在 Candidate + Verification 溯源。
- `OriginalPublisherResolver`（纯函数、零网络）：把 normalized URL 解析为 Source Registry 登记的 original publisher；仅 https、无 userinfo、无非默认端口、hostname 非 IP 字面量、`is_url_allowed`"等于或真子域"语义（非 substring）；无匹配 → `NewsPublisherUnsupported`，多匹配 → `NewsPublisherAmbiguous`（不自动挑选）。
- Candidate 完整性：从 `candidate.normalized_url` 重算 hostname 必须等于 `candidate.domain`，否则 `NewsOriginalSourceIntegrityError`；company_id 一律来自 `NewsDiscoveryRun.company_id`，绝不来自 Candidate 或外部参数。
- DNS/SSRF 纵深防御：`HostResolver` Protocol + `SystemHostResolver`（IPv4+IPv6，`asyncio.to_thread` 包裹 `socket.getaddrinfo`）；拒绝 loopback/private/link-local/multicast/reserved/unspecified/shared 地址与 IP 字面量 host；预检仅声明为纵深防御，不宣称传输层 DNS pinning。
- `SafeHtmlFetcher`：`trust_env=False`；无 Cookie/Authorization/API Key/浏览器 Headers/JS/自动重试；仅 https、hostname 必须属于同一 publisher 的 allowed_domains；请求前 DNS 预检；手动重定向 ≤5 次且每跳完整重校验、跨 publisher 拒绝；仅接受 2xx、Content-Type 基础媒体类型必须 `text/html`（不接受 xhtml+xml）；5 MiB 上限（Content-Length 提前拒绝 + 流式超限拒绝）；保留原始字节（不强制 UTF-8、不重编码）；返回 `FetchedHtmlPage`（requested_url/final_url/final_hostname/status_code/content_type/redirect_count/fetched_at tz-aware UTC/raw_bytes）。
- HTML RawArtifact 归档：`LocalRawArtifactStore.put_html_bytes` → media_type `text/html`、storage key `sha256/ab/cd/<sha256>.html`、内容寻址原子写不可变、replay 不覆盖；先 `get_or_create` 再四项强一致校验（media_type/content_sha256/byte_size/storage_key），任一不一致抛 `NewsOriginalArtifactConflict`，不创建任何 SourceRecord/Verification。
- `NewsOriginalSourceService.verify_candidate`：同一 DB 事务内 RawArtifact → SourceRecord → Verification → candidate verified → commit；replay 短 session 检查既有 Verification、完整性通过则 replayed=true、零网络；SourceRecord 去重键 `(provider_key, final_url, artifact_id)`，多个 Candidate 可共享同一 SourceRecord 但各一条 Verification（UNIQUE per candidate）；并发用 `ON CONFLICT DO NOTHING + RETURNING`，无进程锁。SourceRecord：document_type=news_article、acquisition_method=public_html、published_at=NULL、reporting_period_end=NULL、external_document_id=NULL、authority_tier_snapshot=publisher.authority_tier、provider_capabilities_snapshot=sorted(...)、title 截断到 500。
- 稳定错误（news/errors.py，8 类）：`NewsCandidateNotFound` / `NewsPublisherUnsupported` / `NewsPublisherAmbiguous` / `NewsOriginalFetchFailed` / `NewsOriginalContentRejected` / `NewsOriginalArtifactConflict` / `NewsOriginalSourceIntegrityError` / `NewsOriginalPersistenceFailed`；消息禁止正文/HTML/DB URL/绝对存储路径/Cookie/Auth/query_text，日志只记 provider_key/hostname/status/duration/error_type。
- 边界（§二十-二十一）：`news_article` 只能经 `NewsOriginalSourceService` 产生——upload/import-url 在 service 层拒绝（`NewsArticleIngestionNotAllowed`），回归测试证明 PDF 上传/导入无法绕过；`GET /source-records/{id}/content` 对 HTML 形态返回 415 `SourceContentUnsupportedMediaType`（防存储型 XSS，不内联返回第三方 HTML）。
- migration 0012（已应用，`alembic current` = 0012 head）：新表 `news_source_verifications`（verification_id PK / candidate_id FK RESTRICT UNIQUE / source_id FK RESTRICT / publisher_provider_key FK RESTRICT / requested_url / final_url / final_hostname / http_status CHECK 200-299 / content_type / redirect_count CHECK 0-5 / title_origin IN ('discovery_candidate') / verified_at / created_at）+ 5 个 CHECK 约束演进（source_providers.provider_type +media、raw_artifacts.media_type +text/html、source_records.document_type +news_article、source_records.acquisition_method +public_html、candidates.verification_status +verified）；downgrade 0011 / 再 upgrade 往返验证通过。
- 测试：**45 项新单元测试**（OriginalPublisherResolver 17 / SafeHtmlFetcher 28——含 SSRF 纵深防御、重定向 4 类、大小上限、URL 安全规则、无浏览器特征、trust_env spy、失败日志不泄露正文）+ **9 项 MockTransport E2E 集成测试**（全链路 E2E / replay 无网络 / 并发只产生单 source+单 verification / 多 Candidate 共享 SourceRecord 各一条 Verification / Network Guard / 无 Evidence 结构不变量 / 错误路径）+ **3 项协议与服务边界回归**（news_article 上传与 URL 导入拒绝 ×2、HTML content 415 ×1）+ **4 项一致性收口测试**（SourceContentUnsupportedMediaType.http_status=415 契约冻结 ×1、真实链路 HTML 内容端点 415 + XSS 防护 + stream 生命周期 ×2、Candidate verification_status 冻结 unverified/verified（DB CHECK 拒绝 rejected/archived/evidence_ready）×1，后 3 项走真实 PostgreSQL + 真实 LocalRawArtifactStore + 真实 FastAPI）；全套 **785 非集成 + 126 集成测试通过**，ruff 零告警，`pip check` 通过。
- **受控真实 HTML 传输 Probe（live_html_transport_acceptance）**：对 `www.xinhuanet.com` **恰好 1 次请求**、不重试、不代理、不 DNS 覆盖；真实 `SystemHostResolver` + 真实 httpx transport；结果 status=200 / content-type=text/html / redirects=0 / 178,458 字节 / sha256=d97e535e…，证明真实 HTTPS 传输链（DNS 解析 / 域名归属 / 安全抓取 / 字节归档）可用。**该 Probe 只验证 HTML 传输，不是真实文章端到端验收（real_article_e2e_acceptance = not performed）**：真实文章全链路由自动化测试覆盖（真实 PostgreSQL + MockTransport + FakeHostResolver）。该 Probe **不经过 pytest Network Guard**（guard 由自动化测试单独证明）。
- **明确不做（§二十七 边界）**：不创建 EvidenceCard / Claim / DocumentChunk / Chroma；不用 LLM / LangChain / LangGraph / CrewAI；不做新闻正文解析、内容清洗、摘要、情感、聚类；不做批量历史新闻同步；不开一般 Web 爬虫；不抓取任意 GDELT Candidate；SourceRecord 不是 Evidence（Evidence 管线整体属于 Stage 3）；2D.2B（正文确定性解析与结构化抽取已移交 2E——2E.1 HTML / 2E.2 PDF）与 2D.3（Model Web Search fallback + Discovery Router）尚未开始，Evidence 管线属 Stage 3。Candidate verification_status 冻结为 `unverified` / `verified`，**不存在 rejected / archived / evidence_ready 设计**——验证失败不改 Candidate 状态（保持 unverified），未来失败历史由独立 Attempt 模型记录（如 NewsSourceVerificationAttempt，见 ADR-0015，本阶段不建表）。
- 决策记录：[docs/decisions/0015-original-news-source-verification.md](docs/decisions/0015-original-news-source-verification.md)。

## 确定性 HTML 解析（阶段 2E.1）

阶段 2E.1 把已归档的 text/html SourceRecord（如 2D.2A 登记的新闻原文）**确定性解析为可定位结构化文本快照**（ParsedSource + ParsedSourceBlock）。**状态（2026-08-07）：implementation = completed / automated_tests = completed / docker rebuild acceptance = completed / live acceptance = not required（本阶段纯本地确定性解析，无真实网络依赖）**。

- **顶层分工**：确定性解析（2E）只产出"已归档原文的可定位结构化文本"，是 Evidence 管线的**确定性前置**，不是 Evidence 本身。**Chunk / Embedding / Chroma / EvidenceCard 全部属于 Stage 3**（本阶段一张相关表都不建）。2D.2B 的"真实新闻正文进入 Evidence 管线"表述已移除——正文解析由 2E（2E.1 HTML → 2E.2 PDF）承接。
- **migration 0013（已应用，`alembic current` = 0013 head）**：`parsed_sources`（source_id/artifact_id FK RESTRICT、parse_fingerprint UNIQUE、sha256 regex、parser_version≥1、block_count≥0）+ `parsed_source_blocks`（parsed_source_id FK CASCADE、ordinal≥1、block_type IN 5 类、text 非空、text_sha256 regex、locator JSONB object、UNIQUE(parsed_source_id, ordinal)）。
- **`html_dom` parser（VERSION=2，lxml 5.4.0 唯一新增依赖）**：不联网（no_network）、不执行 JS、不修改 RawArtifact；**编码只从真实 meta 声明识别**（`<meta charset>` / `<meta http-equiv="Content-Type" content="...; charset=...">`，http-equiv 大小写不敏感），用 **stdlib `html.parser.HTMLParser` 确定性 attribute 扫描**（`<meta name="description" content="... charset=gbk">` 这类非声明文本绝不产生编码声明），body/script 文本中的 `charset=` 不影响判定，BOM → 声明 → UTF-8 默认（确定性解码为 str 再交 lxml，避免 latin-1 乱码，GBK 等声明受尊重，无声明非 UTF-8 宁失败不乱码）；删除 script/style/noscript/template/svg；内容根 article→main→body；DOM 顺序抽取 h1-h6/p/li/blockquote/table；whitespace normalize；空文本跳过；相邻相同 block 去重；title 优先 og:title→<title>→h1→None；**published_at 只接受明确 publication 元数据**（`article:published_time` meta，或 `[itemprop="datePublished"]` 的 meta content / time datetime；普通 `<time datetime>` 无 itemprop 忽略、updated/modified 不冒充），naive→None，绝不使用 seen_at/parsed_at/now 伪造。
- **Locator**：每块携带 `{"type":"html_dom","ordinal","tag","xpath","element_id"}`，绝对 xpath 在相同 DOM 下稳定，供后续 Evidence 原文核对。
- **parse_fingerprint**：确定性 SHA-256（source_id、raw artifact sha256、parser_name/version、extracted metadata、ordered blocks text+locator；sort_keys、固定 separators、UTF-8；排除 parsed_at/created_at/DB ID）。**RawArtifact 永久不可变、SourceRecord 固定引用其 artifact**：同一 source + 同 RawArtifact + 同 parser version → 同一指纹 → replay 原快照；原始内容变化必须由新 RawArtifact + 新 SourceRecord 表达（→ 各自独立快照）；同 source + 同 raw + parser version 变化 → 新指纹 → 新快照、旧快照保留（可追溯）。
- **`SourceParsingService.parse_source(source_id)`**：短 session 读 SourceRecord+RawArtifact → 关闭；仅 text/html；文件 I/O 不持 DB transaction；create-or-get ParsedSource → bulk insert Blocks → commit；并发只 1 快照；replay 校验完整性，损坏抛 `ParsedSourceIntegrityError` 不自动修复（存储文件与登记 SHA 不一致视为存储层损坏/篡改，非原文更新）；**不更新 SourceRecord.title/published_at**（SourceRecord 保持原始 provenance 不可变）。
- **安全边界**：HTML content API 对 text/html 保持 HTTP 415（存储型 XSS 防线）；**不新增 raw HTML endpoint**；解析只经 LocalRawArtifactStore 读归档字节、不启动浏览器；**不创建 DocumentChunk / EvidenceCard / Chroma / Embedding / Claim**。
- **测试**：**40 项 HTML parser 单元测试 + 25 项 contracts/fingerprint 单元测试 + 12 项解析 Service 集成测试**（真实 PostgreSQL + 临时 RawStore，零网络）全部通过，覆盖 first parse / replay / fingerprint 确定性 / **不同 RawArtifact → 独立快照（RawArtifact 不可变，旧记录零 UPDATE）** / parser version 变化（v1→v2）→新快照、旧快照保留（v1 不修改不删除）/ 完整性损坏（block sha 篡改、block_count 不一致、存储 SHA 与登记不一致）→ integrity error 不自动修复 / 并发单快照 / ordinal 稳定 / 非 HTML 拒绝 / Source 不存在 / SourceRecord 元数据不被回写 / **published_at 只认 publication 元数据（普通 time 忽略、updated 不冒充）** / **charset 只认真实 meta 声明（stdlib HTMLParser attribute 扫描；description content 里的 charset= 不产生声明；http-equiv 大小写不敏感；body/script 文本 charset= 不影响判定）**；ruff 零告警，`pip check` 通过。
- **决策记录**：[docs/decisions/0016-deterministic-html-parsing.md](docs/decisions/0016-deterministic-html-parsing.md)。
- **后续**：2E.2 已完成（确定性 PDF 解析，见下节）；2E.3 = Stage-2 source pipeline E2E acceptance；Stage 3 才开始 DocumentChunk / Embedding / Chroma / EvidenceCard。

## 确定性 PDF 解析（阶段 2E.2）

阶段 2E.2 把已归档的 application/pdf SourceRecord（如公告、研报 PDF）**确定性解析为可定位结构化文本快照**，复用 2E.1 的 ParsedDocument / ParsedBlock / parse_fingerprint / Repository / replay / integrity / concurrency 全链路，不建任何新表。**状态（2026-08-07）：implementation = completed / automated_tests = completed / docker rebuild acceptance = completed / live acceptance = not required（本阶段纯本地确定性解析，无真实网络依赖）**。

- **`pdf_layout` parser（VERSION=2；pdfplumber 0.11.10 + lxml 5.4.0 锁定到精确版本）**：只读 bytes（内部 `BytesIO`），**不联网、不修改 PDF、不写临时外部文件**；**仅支持 machine-generated PDF**，整个 PDF 无任何可提取文本 → `PdfTextUnavailable`（OCR 留未来），单页无文字不失败。
- **确定性提取**：每页 `page.dedupe_chars()`（仅去**同一坐标**的重复绘制字符）→ `extract_words(use_text_flow=False, keep_blank_chars=False, expand_ligatures=True, 固定 x/y tolerance)`（不用 experimental API）；固定排序 page_number ASC → top ASC → x0 ASC；固定 y tolerance（3.0）聚合 words 为行，每行一个 block（`block_type=paragraph`，**不推断 heading/语义**）；空文本跳过。**不做 text-level 去重（2E.2 收口，v1→v2）**：同页不同 bbox / 跨页的相同文本行全部保留，由 pdf_page locator 区分原文位置——重复文本 ≠ 重复原文内容。
- **Locator**：每块携带 `{"type":"pdf_page","page_number","line_index","bbox":[x0,top,x1,bottom],"page_width","page_height"}`，page_number/line_index 1-based（line_index 每页重置），bbox 用 pdfplumber top-left 语义、全 float `round(...,3)`、必须在 page bounds 内；同一 PDF + parser version → locator 完全稳定。
- **metadata**：`extracted_title` = PDF metadata `Title` normalize 后非空否则 None；`extracted_published_at` **恒为 None**（绝不使用 CreationDate/ModDate/SourceRecord.published_at）。
- **安全边界**：PDF magic 必须有效（`%PDF-`）；encrypted / password-protected → `PdfEncryptedError`；page_count ∈ 1..1000、提取字符总量 ≤ 5,000,000，超限 → `PdfResourceLimitError`；非加密但损坏 → `PdfParseError`。
- **`SourceParsingService` 泛化为 dispatcher**：`text/html → html_dom v2`、`application/pdf → pdf_layout v2`，其他 media type → `UnsupportedParseMediaType`；复用同一 ParsedSource/ParsedSourceBlock 持久化、replay、integrity、并发单快照逻辑；**不改 schema（Alembic 保持 0013 head）**；SourceRecord 元数据不被回写。
- **测试**：**28 项 PDF parser 单元测试 + 新增 PDF contracts/fingerprint 单元测试 + 6 项解析 Service 集成测试**（真实 PostgreSQL + 临时 RawStore，零网络；PDF bytes 由纯 stdlib 手写 fixture 确定性构造，不引入 PDF 生成运行时依赖），覆盖 first parse / replay / HTML vs PDF 独立快照 / page/line 1-based 与每页重置 / bbox bounds 与 float rounding / 重复字符 dedupe_chars 去重 / **同页不同 bbox 与跨页相同行全部保留（v2 收口）** / 中文提取 / 空页允许 / 整篇无文本 → PdfTextUnavailable / magic 与 malformed → PdfParseError / encrypted → PdfEncryptedError / 页数与字符超限 → PdfResourceLimitError / metadata Title normalize / published_at 恒 None / 确定性多遍一致 / SourceRecord 元数据不被回写 / 并发单快照 / 非受支持 media type 拒绝。
- **决策记录**：[docs/decisions/0017-deterministic-pdf-parsing.md](docs/decisions/0017-deterministic-pdf-parsing.md)。

## 确定性文档分块（阶段 3A）

阶段 3A 把 ParsedSource + ordered ParsedBlocks 快照**确定性切分为 ChunkSet + DocumentChunk**（`chunk_sets` + `document_chunks` 表，migration 0014）。**状态（2026-08-08）：implementation = completed / automated_tests = completed / docker rebuild acceptance = completed / live acceptance = not required（纯本地确定性分块，无真实网络依赖）**。

- **`block_window` chunker v1（字符窗口，不绑定 BGE tokenizer）**：`target_chars=400` / `max_chars=500` / `overlap=0`；严格按 block.ordinal、尽量合并完整 block、block 之间 `"\n"`、合并后 ≤ max；单 block > 500 按确定性句末标点（。！？!?；;）切分，无标点 hard split；**不删除重复文本、不跨 ParsedSource、chunk text 非空**。
- **locator_refs（可完整回溯）**：每 chunk 保存 `[{"block_ordinal","char_start","char_end","locator"}]`，char 索引相对原 block.text（Python `[start, end)`）；**Chunk → ParsedBlock locator → ParsedSource → SourceRecord → RawArtifact** 逐级可回溯；PDF（`pdf_page`）与 HTML（`html_dom`）同一 Chunk 模型。
- **fingerprint / replay / 并发 / 版本**：`chunk_set_fingerprint`（canonical JSON + SHA-256，排除 DB ID/created_at）驱动 replay 原 ChunkSet；并发相同 chunking 只 1 个 ChunkSet + 一套 chunks；chunker version 变化 → 新 ChunkSet、旧版本保留；已有 ChunkSet 损坏 → `ChunkSetIntegrityError`，**不自动修复**。
- **不修改上游**：ChunkingService 对 SourceRecord / ParsedSource 零写操作；不重新读 RawArtifact 解析。
- **边界**：**不创建 Chroma collection、不做 Embedding / Retrieval / EvidenceCard / LLM**（Stage 3B = BGE + Chroma，3C = EvidenceCard，见 [docs/stage-3-plan.md](docs/stage-3-plan.md)）。
- **测试**：**38 项单元测试 + 11 项集成测试**（真实 PostgreSQL + 临时 RawStore，零网络）：HTML/PDF 首建、逐 chunk 回溯到 SourceRecord + RawArtifact + 原 locator、多 block 多 chunk、replay、chunker version 变化新旧并存、并发单集、chunk text/chunk_count/locator_refs 篡改 → 不修复、ParsedSourceNotFound、SourceRecord/ParsedSource 零修改。
- **决策记录**：[docs/decisions/0018-deterministic-document-chunking.md](docs/decisions/0018-deterministic-document-chunking.md)。

## BGE Embedding + Chroma 向量索引基座（阶段 3B.1）

阶段 3B.1 把 3A 的 DocumentChunk **确定性向量化并写入 Chroma**，建立 PG manifest + Chroma derived index 的索引基座。**状态（2026-08-08）：implementation = completed / automated_tests = completed / live acceptance = not required；real_bge_acceptance = passed；latest_image_docker_acceptance = completed**（CPU-only build 成功产出新镜像 `9066b4c9150b`，2.24GB；Docker 使用 CPU-only BGE runtime：torch 2.13.0+cpu 从 PyTorch 官方 CPU index 预装，镜像内无 nvidia-* CUDA 运行时包；Stage 3B 全部达成）。

- **冻结 Embedding 契约**（`app/rag/embedding/contracts.py`）：**BAAI/bge-small-zh-v1.5**，dimension=512、normalize=true、max_input_tokens=512，query_instruction=`"为这个句子生成表示以用于检索相关文章："`（**仅 query 加**）；**immutable revision = `7999e1d3359715c523056ef9478215996d62a620`**（真实模型 smoke 解析的 commit hash，不依赖 moving "main"）；**禁止 silent truncation**（超限抛 `EmbeddingInputTooLong`）；向量必须满足 dimension / finite / L2 norm≈1 契约。
- **BGEProvider 惰性加载**（`app/rag/embedding/bge.py`）：模型首次调用时才 import 加载，不阻塞 app import / startup、不在启动时联网下载；sentence-transformers **精确 pin**（4.1.0 / transformers 4.57.6 / tokenizers 0.22.2 / torch 2.13.0+cpu，real smoke 记录）。
- **PG = Source of Truth，Chroma = 可重建 derived index**：Chroma 只存 `确定性 record id(str(chunk_id)) → embedding → primitive metadata`，不含正文与 locator_refs（locator 从 PG hydrate），允许 partial rows / 整体重建；**collection identity v2**：collection 名称由 embedding schema fingerprint 纯函数派生（`insightforge_chunks_v2_<fp[:12]>`，schema_version=2），同 schema 的公司 / ChunkSet 共享，model revision 变化 → 确定性新 collection + 新 manifest、旧 collection/manifest 保留；cosine、**不配置 embedding function**（application 自算 embedding 显式传入）；collection 冻结 metadata 不一致 → `VectorCollectionConflict`。
- **Migration 0015**：`chunk_vector_indexes` 表（vector_index_id PK、chunk_set_id FK RESTRICT、模型配置、expected/indexed count、`index_fingerprint` CHAR(64) UNIQUE、status building/ready/failed、last_error_code、ready_at；自然身份 UNIQUE(chunk_set_id, model_id, model_revision, schema_version)）；`chunk_sets` 补 UNIQUE(parsed_source_id, chunker_name, chunker_version)。
- **VectorIndexService.index_chunk_set(chunk_set_id)**：短 DB session 读后关闭 → Embedding/Chroma 网络操作不持 DB transaction → create-or-get manifest（自然身份 ON CONFLICT）→ 兼容 collection → 分批 upsert → 验证 expected chunk IDs + text_sha256；成功 `ready`、失败 `failed`+稳定错误码；**ready replay 先验证不重嵌入**，Chroma 缺失/错误 → `VectorIndexIntegrityError` 不自动修复（read path 尚未建立）；允许 Chroma partial；**并发 → PG manifest=1 + 每 chunk record=1**（无进程锁）。
- Chroma record metadata 仅 primitive：chunk_id/chunk_set_id/parsed_source_id/source_id/company_id/provider_key/document_type/chunk_ordinal/text_sha256/authority_tier/critical_claim_eligible（published_at / reporting_period_end 有值才存 epoch）。
- **明确不做**：RetrievalService / top-k / threshold / reranker / EvidenceCard / Claim / Report / LLM / LangGraph 集成（属于 3B.2 / 3C）；不新增 HTTP 端点；BGE 不作为 `/ready` 检查条件。
- **测试**：**单元测试**（embedding contracts / BGEProvider / index contracts fingerprint+collection / Chroma fakes）+ **集成测试**（真实 PostgreSQL + FakeChroma 零网络：happy path、where company_id 过滤、ready replay 不重嵌入、embedding 失败 → failed → retry ready、Chroma record 被删 → integrity error 不修复、并发单 manifest、ChunkSetNotFound、chunk 被删 → integrity error、collection 冲突 → failed）+ **0015 downgrade guard**（独立临时 PG 库 `insightforge_gate_*`：有 manifest 数据拒绝降级、无 manifest 允许降级）+ **真实 Chroma 集成测试**（独立测试 collection，结束删除：roundtrip + 冻结 metadata 往返 + where 过滤、replay 验证）；自动化测试**不下载真实模型**。
- **决策记录**：[docs/decisions/0019-bge-chroma-index-foundation.md](docs/decisions/0019-bge-chroma-index-foundation.md)。

## Filtered Vector Retrieval（阶段 3B.2）

阶段 3B.2 在 3B.1 向量索引之上建立 **RetrievalQuery → Chroma filtered query → PG hydrate → RetrievalHit** 的语义检索 read path。**状态（2026-08-08）：implementation = completed / automated_tests = completed / live acceptance = not required**。

- **RetrievalQuery**（`app/rag/retrieval/contracts.py`）：company_id 必填；query_text trim 后非空、≤1000 字符；top_k 默认 10（1..50）；可选 filters：source_ids / provider_keys / document_types / authority_tiers / critical_claim_eligible_only / published_from/to / reporting_period_from/to（时间 timezone-aware、from≤to）；**不支持任意用户自定义 Chroma where JSON**。
- **Eligible index selection（PG 侧，完整匹配）**：ready manifest + **embedding 配置完整匹配**（model_id / revision / dimension / normalize_embeddings / collection_name / collection_schema_version）+ **indexed == expected** + 当前 chunker（block_window v1）+ 当前 parser identity（html_dom v2 / pdf_layout v2）+ company_id + filters；为空 → `RetrievalIndexNotReady`；failed/building manifest、旧 chunker/parser、维度/归一化/collection 名不匹配、indexed<expected 一律排除。
- **Query embedding**：`embed_query(query_text)`（BGE query instruction 由 provider 加）；**禁止 silent truncation**（超长 → `EmbeddingInputTooLong` 传播）。
- **Chroma filtered query**：where 至少含 `chunk_set_id $in eligible`（company 隔离白名单），再组合 filters 成单个 `$and`；只取 ids/metadatas/distances（**不用 documents 作为正文来源**）。
- **Collection metadata 校验（query 前）**：`get_collection` 后校验实际 collection name == 查询 collection，且 `collection.metadata` 冻结键（schema_version / model_id / model_revision / dimension / normalized / distance_metric）与 `build_collection_metadata(current spec)` **完全一致**；任一不一致 → `RetrievalIndexIntegrityError`，**不继续 query、不自动修改 collection**。
- **PG hydrate + integrity（保持 Chroma ranking 顺序）**：按 chunk_id 批量 hydrate（DocumentChunk → ChunkSet → ParsedSource → SourceRecord provenance）；任何不一致（chunk 缺失 / metadata 或 text_sha256 不匹配 / chunk_set 不在 eligible / **重复 chunk_id / distance 非 finite** / ids-metadatas-distances 长度不一致）→ `RetrievalIndexIntegrityError`，**不 skip / 不自动重建**。
- **Ranking**：只使用 Chroma cosine distance（升序）；**无 threshold / reranker / MMR / BM25 / LLM judge**；`distance` 只作检索诊断，不叫 confidence/probability；top_k 不足时返回实际命中数。
- **RetrievalHit 是 read model（不落库）**：rank / chunk_id / chunk_set_id / parsed_source_id / source_id / company_id / text / distance / provider_key / document_type / source_title / source_url / published_at / reporting_period_end / authority_tier / critical_claim_eligible / chunk_ordinal / locator_refs；`locator_refs` 从 PG hydrate。
- **纯 read path**：检索不自动 index_chunk_set、不 repair/write、不创建 collection（`get_collection` 缺失 → `RetrievalIndexNotReady`）；**0 manifest、0 Chroma 写**。
- **测试**：**单元测试 37 项**（RetrievalQuery 校验 / where builder / RetrievalService：query instruction、token too long、no threshold、company isolation、collection 缺失、eligible 空、Chroma 不可用、hydrate integrity、**collection metadata 校验、重复 chunk_id、非 finite distance**）+ **集成测试**（真实 PostgreSQL + FakeChroma 零网络 20 项：company 隔离 / provider / document_type / source_ids / authority / critical-only / published range / reporting period range / ready-only / failed+building 排除 / 旧 chunker+parser 排除 / **维度、normalize、collection 名不匹配、ready 但 indexed<expected 排除** / 全链路 hydrate / ranking / 篡改 metadata→integrity / Chroma 不可用 / read path 0 manifest）+ **真实 Chroma 1 项**（独立 collection，结束删除）；自动化测试**不下载真实模型**。
- **决策记录**：[docs/decisions/0020-filtered-vector-retrieval.md](docs/decisions/0020-filtered-vector-retrieval.md)。

## EvidenceCard Provenance（阶段 3C.1）

阶段 3C.1 把**已确认与研究问题相关的 DocumentChunk 片段**确定性登记为可追溯 EvidenceCard（`evidence_cards` 表，migration 0016）。**状态（2026-08-09）：implementation = completed / automated_tests = completed / live acceptance = not required（不开放 Evidence HTTP 端点）**。证据边界：**RetrievalHit = 候选资料；EvidenceCard = 已确认、有明确原文片段和 provenance 的原子证据；Claim = Stage 4**。EvidenceCard 不含 supports/contradicts_claim，语义字段命名 `evidence_statement`（不用 `claim_text`）。

- **Migration 0016**：`evidence_cards`（evidence_card_id PK；5 个 FK RESTRICT：company_id/source_id/parsed_source_id/chunk_set_id/chunk_id；provider_key FK RESTRICT source_providers；16 个 CHECK：quote_start≥0、quote_end>quote_start、evidence_type IN 五类、extractor_confidence IN 三档、extractor_version≥1、evidence_schema_version≥1、全部 SHA `^[0-9a-f]{64}$`、locator_refs array、btrim 非空五字段；6 个索引：company_id/source_id/chunk_id/research_question_sha256/evidence_type/created_at；evidence_fingerprint CHAR(64) UNIQUE）。**0016 downgrade guard**：有卡拒绝降级、空表允许（isolated 集成测试）。
- **EvidenceCardDraft 只允许语义输入**（`app/evidence/contracts.py`）：research_question/evidence_statement/evidence_type/chunk_id/quote_start/quote_end/extractor_name/extractor_version/extractor_model_id?/extractor_confidence；调用方**不得提供** company_id/source_id/authority tier/provider/published time/locator_refs/quote_text/quote_sha256/evidence_fingerprint（由 Service 从 PG provenance + chunk 确定性推导）。
- **Exact quote**：`quote_text = chunk.text[quote_start:quote_end]`，程序切片，不信任 caller/LLM，strip 后非空、越界 → `EvidenceQuoteRangeError`；**绝不 normalize/改写/摘要/自动纠错**。
- **Locator projection**（`project_evidence_locator_refs`）：chunk text = 各 ref block slice 以 `"\n"` 连接；`sum(段长)+separators == len(chunk_text)` 破坏 → `EvidenceLocatorIntegrityError` 不修复；与 quote 求交后只留实际覆盖的 refs，char 范围缩窄到原 ParsedBlock，locator 原样保留（HTML xpath/element_id；PDF page_number/bbox）。
- **Provenance load**：`create_card(draft)` 从 chunk_id 真实加载 DocumentChunk→ChunkSet→ParsedSource→SourceRecord→Company，派生全部 provenance 快照；链损坏 → `EvidenceProvenanceIntegrityError`；**不读取 Chroma、不重新 Retrieval**。company_id 取自 SourceRecord.company_id（FK RESTRICT）。
- **Research question**：不新建表、不伪造 question UUID；trim 后保留原文本；`research_question_sha256` = SHA-256(trim 后 UTF-8)。
- **Confidence/reliability 分离**：`authority_tier_snapshot`（来源可靠性）≠ `extractor_confidence`（语义提取置信度）；`critical_claim_eligible_snapshot` 直接复制 SourceRecord，**不因 extractor_confidence=high 自动提升**。
- **Fingerprint / replay / 并发**：`evidence_fingerprint` = canonical JSON（sort_keys、紧分隔、UTF-8）+ SHA-256，含 schema_version + 5 ids + 语义 + quote + locator_refs + provenance 快照 + extractor 三件套，排除 evidence_id/created_at；相同 → replay 原卡；并发 → 1 卡（PG `ON CONFLICT(evidence_fingerprint)`，无进程锁）；语义/quote/extractor version 任一变化 → 新卡、旧卡保留。
- **Replay integrity**：replay 时重新加载真实 provenance 并核实 quote 切片、quote_sha256、locator projection、各级 IDs、provider、authority/critical 快照、published/reporting period、fingerprint；任一损坏 → `EvidenceCardIntegrityError`，**不自动 repair**。
- **Repository**（`app/repositories/evidence_card_repository.py`）：get_by_id / get_by_fingerprint / list_by_company / create_or_get（ON CONFLICT DO NOTHING RETURNING）；**无 update API**。
- **测试**：**57 项单元**（contracts/quote/locator）+ **19 项集成**（真实 PG + 真实 SourceParsingService/ChunkingService，零 Chroma/LLM/embedding：首建、replay、并发→1、statement/quote/extractor version 变化→新卡、provenance snapshots、high confidence 不提升 critical eligibility、损坏 replay→integrity 不修复、无 update API、E2E HTML DOM locator + 完整回溯、E2E HTML 跨 `"\n"` 双 locator、E2E PDF page/bbox 跨页 + 回溯 ParsedSourceBlock、provenance 链断裂→integrity）+ **2 项 migration 0016 downgrade guard**（isolated 临时 PG）。
- **边界**：不创建 Claim/Report/ReviewIssue；不调用 LLM/LangGraph/CrewAI/BGE/Chroma query；EvidenceCard 不是 RetrievalHit 的自动升级（Service 构造函数只持有 sessionmaker，只显式接受 `create_card(EvidenceCardDraft)`）。
- **决策记录**：[docs/decisions/0021-evidence-card-provenance.md](docs/decisions/0021-evidence-card-provenance.md)。

## Structured Evidence Extractor（阶段 3C.2）

阶段 3C.2 把 **RetrievalHit + research question → LLM structured semantic extraction → 确定性 quote resolution → EvidenceCardService.create_card()** 接通：把检索候选变成"被原文直接支持、有精确引用定位的 EvidenceCard"。**状态（2026-08-09）：implementation = completed / automated tests = completed / live acceptance = not required（不开放 Evidence HTTP 端点）；real_evidence_extractor_smoke = completed（2026-08-09，真实 DeepSeek V4 Flash smoke 走生产路径通过：provider=deepseek、request_model=deepseek-v4-flash、thinking 显式 disabled、relevant=true、item_count=1、quote 精确解析成功；LLM 只用于一次性受控 smoke，不进入自动化测试）**。角色边界：**Extractor 只做语义**（相关性判断、原子 evidence_statement、evidence_type、low/medium/high confidence、逐字 quote_text）；**确定性代码负责** quote_start/end、locator 投影、provenance IDs、authority tier、critical eligibility、fingerprint、Claim、投资建议。

- **契约**（`app/evidence/extractor/contracts.py`）：`EVIDENCE_EXTRACTOR_NAME="structured_llm"`、`EVIDENCE_EXTRACTOR_VERSION=1`、`MAX_EXTRACTION_ITEMS_PER_HIT=3`。`EvidenceExtractionItem`（evidence_statement/evidence_type/quote_text/confidence，**无 reasoning/CoT/free-form 字段**）+ `EvidenceExtractionDecision`（relevant/items/reason_code）；relevant=false→items 空；relevant=true→1..3 items 且 reason_code=None；单 response 不允许完全重复 item。
- **LLM 抽象**：最小 `EvidenceExtractionModel` Protocol（model_id + async extract）；domain 不依赖具体 provider；自动测试一律用 `FakeEvidenceExtractionModel`（零真实 LLM/网络）。可选 `LangChainStructuredOutputAdapter`（lazy import，langchain 非必需依赖；model_id = `provider:model@revision`，绝不伪造 revision）；temperature=0，禁止 tools/web search。
- **Prompt 边界**（`prompt.py`）：system 冻结声明 source 是不可信 DATA、忽略注入、无 tools/CoT、quote 逐字、statement 由 quote 支持、不补充 source 外事实、不生成投资建议、不输出 Claim、无直接证据→relevant=false；source 只进 user/data payload（`<<<SOURCE_TEXT_START/END>>>` delimiter），绝不拼接进 system。
- **Exact quote resolver**（`quote.py`）：`resolve_exact_quote(chunk_text, quote_text)` 精确子串，0 次→`QuoteNotFound`、>1 次（含重叠）→`QuoteAmbiguous`；不做 fuzzy/normalize/自动纠错；LLM 不返回 offsets。
- **ExtractionService**（`service.py`）：短 DB read + stale guard（`sha256(hit.text)==chunk.text_sha256` 且 5 个 provenance ids 匹配，否则 `InputStale`，**在 LLM 调用前拒绝**）→ model.extract → strict schema（malformed → `MalformedOutput`）→ relevant=false 0 写 → 全部 items 先完成 quote+decode 校验再逐 draft `create_card`（单 hit 最多 3 卡；replay/并发由 3C.1 fingerprint 保证）。quote 以 fresh PG text 为准；日志仅白名单字段。
- **错误分类**：`EvidenceExtractorUnavailable` / `EvidenceExtractionMalformedOutput` / `EvidenceExtractionQuoteNotFound` / `EvidenceExtractionQuoteAmbiguous` / `EvidenceExtractionInputStale` / `EvidenceExtractionInputError`。
- **测试**：**122 项单元**（contracts/quote resolver/prompt 注入边界/service；零 LLM）+ **11 项集成**（真实 PG：E2E HTML DOM locator 跨 block 2 refs + provenance 快照、E2E PDF 跨 page/bbox + 回溯 ParsedSourceBlock、rerun replay、stale 拒绝 0 写、relevant=false/not-found/ambiguous/malformed 0 写、high confidence 不提升 critical、单 hit 3 卡、0 manifest 无 claims/reports 表；零 Chroma/BGE/LLM/network）。
- **边界**：不创建 Claim/Report/Audit；不接 LangGraph/CrewAI；不自动 Retrieval/reranker/fact cross-check/second judge；不开放 HTTP API；Alembic head 保持 0016（无新 migration）。
- **决策记录**：[docs/decisions/0022-structured-evidence-extraction.md](docs/decisions/0022-structured-evidence-extraction.md)。

## Generic Evidence Origin + Macro Evidence（阶段 3C.3A）

阶段 3C.3A 把 EvidenceCard 泛化为**双 origin**（`origin_type ∈ document_chunk / macro_observation`），在 Stage 4（Claim）前完成 origin 模型泛化：宏证据直接引用 MacroObservation，**不是 Macro → fake DocumentChunk**（不经过 DocumentChunk / ParsedSource / Chroma / quote resolver）。**状态（2026-08-09）：implementation = completed / automated tests = completed / live acceptance = not required（不开放 Evidence HTTP 端点，宏证据走确定性服务）**。单表单 namespace：同一个 `evidence_card_id` 命名空间，不拆两张表。

- **EvidenceCard ├── document_chunk └── macro_observation**：`document_chunk` = 3C.1/3C.2 既有语义（chunk quote + 文档 provenance）；`macro_observation` = 直接引用 MacroObservation 的宏证据（company_id 由调用方上下文提供、Service 显式校验 Company 存在）。两种 origin 共享 fingerprint / replay / 并发幂等 / Repository。
- **Migration 0017**（`alembic current` = 0017 head）：`origin_type` NOT NULL server_default 'document_chunk' + 索引（旧 v1 document 行回填 document_chunk，**不重算旧 fingerprint**）；macro_* 三列 UUID NULL（FK RESTRICT macro_observations / macro_dataset_snapshots / macro_series）+ 索引；document-specific 列（source_id/parsed_source_id/chunk_set_id/chunk_id/quote_start/quote_end/quote_text/quote_sha256）改可 NULL；3 个新 CHECK（origin 枚举、conditional origin_consistency——document_chunk→document+quote 全 NOT NULL 且 macro 全 NULL，macro_observation→反之、`locator_refs` 非空 array）。**0017 downgrade guard**：有 macro_observation 行拒绝降级（不静默丢 origin semantics），无 macro 行可安全降级。
- **MacroEvidenceDraft**（`app/evidence/contracts.py`）：只允许语义输入（company_id/research_question/macro_observation_id/evidence_statement/extractor_name/extractor_version/extractor_model_id?/extractor_confidence）；**evidence_type 不是 draft 字段**（固定 metric）；调用方**不得提供** value/period/provider/snapshot/series/locator/authority tier/quote/fingerprint。
- **MacroEvidenceService.create_macro_card(draft)**（`app/services/macro_evidence_service.py`）：短 DB session 读真实 provenance（Company → Observation → Snapshot → Series → Provider → Artifact links → RawArtifact，链断裂 → `EvidenceProvenanceIntegrityError` 不修复）→ 纯函数派生（provider_key 来自 MacroSeries；authority_tier_snapshot / critical_claim_eligible_snapshot **直接复制 MacroDatasetSnapshot 获取时快照，不硬编码 World Bank tier**；deterministic structured macro locator；evidence_type=metric；quote/published/reporting period 固定 NULL）→ create_or_get（PG `ON CONFLICT(evidence_fingerprint)` 并发幂等）→ replay 逐字段校验（损坏 → `EvidenceCardIntegrityError` 不 repair）。**无 LLM、无 Chroma、无 DocumentChunk、无 quote resolver**。
- **Fingerprint schema v2**：`EVIDENCE_SCHEMA_VERSION = 2`；document + macro fingerprint payload 都加入 `origin_type`。旧 v1 document 卡不重算；新 document 卡用 v2。`compute_macro_evidence_fingerprint` 含宏身份 / period / value / is_missing / provider 快照 / locator。
- **Document 回归（零行为破坏）**：既有 `EvidenceCardService.create_card` 继续只处理 document_chunk origin；3C.1/3C.2 全部语义原样保留。两种 origin 由不同 Service 独占创建。
- **测试**：**30 项宏契约单元测试**（draft 输入防御、无 provenance/value 字段、无 evidence_type 字段、locator 确定性、fingerprint 敏感性）+ **12 项宏证据集成测试**（真实 PG + MockTransport WorldBank 链路，零 Chroma/LLM：document-free 创建、locator 回溯、**authority tier 来自真实 provenance（UPDATE snapshot → 卡复制新值）**、要求 Company 已存在、replay/并发→1、statement/extractor version 变化→新卡、corrupted provenance/replay→integrity、不创建 DocumentChunk/ChunkSet/ParsedSource/SourceRecord、missing observation 仍可登记）+ **2 项 migration 0017 downgrade guard**（isolated 临时 PG：有 macro 卡拒绝降级、无 macro 卡 document v1 行往返无损）。
- **边界**：不创建 Claim/Report/Audit；不接 LangGraph 顶层编排 / CrewAI；不自动 Retrieval / reranker / fact cross-check / second LLM judge；不开放 HTTP API；不引入 LLM 自动解释宏数值；Alembic head = 0017。
- **决策记录**：[docs/decisions/0023-generic-evidence-origin-macro-evidence.md](docs/decisions/0023-generic-evidence-origin-macro-evidence.md)。

## Claim Provenance + Persistence Foundation（阶段 4A）

阶段 4A 把 Stage 3 已确认的 Evidence 单元进一步登记为**可追溯、可回放的 Claim 分析结论**（`claims` / `claim_evidence_links` 表，migration 0018）。**状态（2026-08-09）：implementation = completed / automated tests = completed / live acceptance = not required（不开放 Claim HTTP 端点）**。Claim 是证据链 **Source → Evidence → Claim → Report → Audit** 的第三个环节的最小原子单元：EvidenceCard = 已确认的来源事实；Claim = 引用 Evidence 的分析结论。**关系属于 ClaimEvidenceLink，不在 evidence_cards 上增加 supports_claim / contradicts_claim**。

- **ClaimDraft 只允许语义输入**（`app/claims/contracts.py`）：company_id / research_question / statement / analysis_domain / claim_kind / confidence / importance / support/contradict/context_evidence_ids / analyst_name / analyst_version / analyst_model_id（optional）；调用方**不得提供** authority tier / provider / source IDs / provenance / fingerprint / created_at（由 Service 从真实 Evidence 确定性派生）。
- **冻结枚举（CLAIM_SCHEMA_VERSION = 1）**：analysis_domain（financial/business/event/macro/risk/valuation）；claim_kind（fact/inference/risk/relative_valuation，**不含** prediction/buy/sell/recommendation/price_target/return_forecast）；confidence（low/medium/high）；importance（normal/critical）；relation（supports/contradicts/context）。
- **输入防御**：research_question / statement / analyst_name trim 后非空；evidence id list 去重后按 `str(uuid)` 升序（canonical deterministic order）；**同一 EvidenceCard 不能跨 relation 重复**（v1 禁止 supports+contradicts / supports+context / contradicts+context）。
- **ClaimService.create_claim(draft)**（`app/services/claim_service.py`）：短 DB session 从真实 PG 加载全部 EvidenceCard——任一缺失或 `evidence.company_id != draft.company_id` → `ClaimEvidenceCompanyMismatch`。纯函数规则（**不做语义判断**）：≥1 supports Evidence（否则 `ClaimEvidenceInsufficient`）；critical 需 ≥1 supports 满足 `critical_claim_eligible_snapshot=true`（否则 `ClaimCriticalEvidenceInsufficient`，**不因 extractor_confidence=high 放宽、不因多个 Tier-3 Evidence 自动推断**）；macro 需 ≥1 macro_observation supports **且** ≥1 document_chunk Evidence（supports 或 context，否则 `MacroClaimTransmissionEvidenceInsufficient`，只验证结构、不判断因果）。
- **Fingerprint / replay / 并发**：`claim_fingerprint` = canonical JSON + SHA-256（含 claim_schema_version / company / research_question / statement / enums / analyst 身份 / 按 relation 分组的 ordered evidence_card_ids；**不含 claim_id / created_at**）；同一完全相同 Claim → replay 同一行；并发 → 1 行（PG `ON CONFLICT(claim_fingerprint)`，无进程锁）；replay 逐字段核实（statement/enums/company/question hash/analyst identity/link 数量/relations/Evidence IDs/critical rule/macro rule/fingerprint），任一损坏 → `ClaimIntegrityError` **不自动 repair**；修改观点 = 新 Claim（无 update API）。
- **Migration 0018 downgrade guard**：存在任何 Claim / Link 数据时拒绝降级（不静默丢弃 Claim 证据链）；无数据时允许回到 0017。
- **Migration 0019 closeout**：把"同一 EvidenceCard 对同一 Claim 只能有一种 relation"下沉到数据库层，新增 `UNIQUE(claim_id, evidence_card_id)`（**不修改已落地的 0018**）。真实 PG 验收：同 claim + 同 evidence 已有 supports 后，直接 SQL 插入 contradicts 必须被数据库 UNIQUE 拒绝。downgrade：有 link 数据时拒绝回滚（删除约束会静默允许跨 relation 重复、改变 v1 语义）。
- **阶段边界**：`claims` / `claim_evidence_links` 允许存在；`report_outlines` / `report_sections` / `reports` / `review_issues` **不得存在**（Stage-5 表名精确命名，不用会过期的阶段名）。
- **测试**：**24 项契约单元 + 26 项集成（真实 PG + 真实 Parsing/Chunking/MacroEvidenceService，零 Chroma/LLM：document/macro/mixed relations、company mismatch / missing / no supports / critical without+with eligible / macro 拒绝 / valid macro structure、fingerprint 确定性 / replay / 并发→1 / statement change→新 Claim / evidence relation change→新 Claim / analyst version change→新 Claim / replay corruption→integrity error / EvidenceCard 行永不修改 / document + macro E2E provenance SQL trace / 0019 跨 relation 重复由数据库拒绝）+ 2 项 migration 0018 + 3 项 migration 0019 downgrade guard（isolated 临时 PG）**。全程 0 LLM / 0 Chroma query / 0 LangGraph / 0 Claim Agent / 0 Report 表。
- **决策记录**：[docs/decisions/0024-claim-provenance-foundation.md](docs/decisions/0024-claim-provenance-foundation.md)。
- **后续**：4B.1（Structured Claim Analysis Foundation + Business / Event / Risk）= completed（见下节）；**4B.2A（Financial Metric Observation Foundation）= completed（见下节）**；**4B.2B（Deterministic Financial Calculation）= completed（见下节）**；**4B.2C.1（Financial Claim Provenance）= completed（见下节）**；**4B.2C.2（Structured Financial Analysis）= completed（见下节）**；4C.1（Macro Context Analyst）= next；4C.2（Valuation）= later；4D（Claim 综合 / 冲突 / 证据缺口）= later；**Report 生成与 Agent Audit 属于 Stage 5**，不提前标记。Financial 统一归属 4B.2，4C 不重复 Financial。详见 [docs/stage-4-plan.md](docs/stage-4-plan.md)。

## Structured Claim Analysis（阶段 4B.1）

阶段 4B.1 把 **EvidenceCard[] + research question + analysis domain → LLM 结构化决策 → ClaimCandidate[] → 确定性 ref resolution → ClaimDraft[] → `ClaimService.create_claim_batch` 原子持久化** 接通，是第一个 Structured Analyst 基础设施。**状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Claim Analysis HTTP 端点）；real_claim_analysis_smoke = completed（真实 DeepSeek V4 Flash smoke 走生产适配器通过）**。无新 migration（`alembic current` 保持 0019 head；复用 4A 的 `claims` / `claim_evidence_links`）。只支持 business / event / risk；financial / macro / valuation → `ClaimAnalysisDomainNotReady`。角色边界：**Analyst 只做判断**（相关性、statement、kind、confidence、importance、E 编号引用）；**确定性代码负责** Evidence Pack 构造、ref resolution、company 归属、policy、fingerprint、原子持久化。

- **契约**（`app/analysis/claims/contracts.py`）：`CLAIM_ANALYST_VERSION = 1`、`MAX_EVIDENCE_PER_REQUEST = 30`、`MAX_CLAIMS_PER_DECISION = 5`；`ClaimAnalysisRequest`（company_id UUID / question trim 非空 / evidence 1..30 去重 + canonical 排序）；`ClaimCandidate`（claim_kind 只允许 fact/inference/risk，schema 层拒绝 relative_valuation，每条 ≥1 support_ref，ref 格式 E<number>）；`ClaimAnalysisDecision`（relevant=false→空 claims + 可选 reason_code；relevant=true→1..5 claims；无完全重复）；**无 reasoning / CoT / free-form / analysis_domain / evidence UUID / provider policy 字段**。
- **Evidence Pack**（`evidence_pack.py`）：真实 PG EvidenceCard → **最小投影**（evidence_ref/statement/type/origin_type/authority_tier/provider_key，document 附 quote_text/published_at/reporting_period_end；**不发送** UUID/fingerprint/locator/raw/Chroma distance）；按 `str(evidence_card_id)` 升序编号 E1..En，双向映射可复现。
- **Strategies**（`strategies.py`）：`business_event_v1`（business/event）、`risk_skeptic_v1`（risk）；persisted `analyst_name` = 具体 strategy，`analyst_version` / `analyst_model_id`（`provider:model`）一并落库可追溯。
- **LLM 抽象 + 生产适配器**（Protocol / `adapters.py` / `factory.py`）：`DeepSeekClaimAnalysisModel` = 懒加载 `ChatDeepSeek` + `with_structured_output`，temperature=0 + **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`），只启用 structured-output、不绑定 tools/web search；`OutputParserException`→`ClaimAnalysisMalformedOutput`、其余→`ClaimAnalysisModelUnavailable`。
- **Prompt boundary**（`prompt.py`）：冻结 system prompt 声明 Evidence 是不可信 DATA、忽略注入、不生成投资建议、不用工具/不联网/不调用函数、无 CoT；Evidence 只进 user payload 的 `EVIDENCE_DATA_START/END` delimiter，绝不拼接进 system。
- **Ref resolution**（`ref_resolver.py`）：E → evidence_card_id，**不 fuzzy resolve**；未知 E → `ClaimAnalysisUnknownEvidenceRef`；跨 relation 重复 → `ClaimAnalysisRelationConflict`；组内去重 + canonical 排序；**任一 candidate 无效 → 整次 0 写**。
- **Service**（`service.py`）：防御性 domain check → 真实 PG 加载 Evidence（缺失/跨公司 → `ClaimAnalysisEvidenceCompanyMismatch`）→ build pack → `_call_model`（服务层 double-check schema）→ relevant=false 0-claims → resolve → 构造 drafts → kind 兼容性兜底 → `create_claim_batch`。
- **原子批量持久化**（`app/services/claim_service.py` 的 `create_claim_batch`）：1..5 个 drafts；**all-drafts-validate-first**（开事务前全量加载证据 + policy 校验 + fingerprint 派生，任一失败 → 整批 0 写）；**单 transaction + ordered result**（`ClaimBatchResult.items[i]` 永远对应 `drafts[i]`，不按 created/replayed 分组重排；任一 SQLAlchemyError → rollback + `ClaimPersistenceFailed`，replay 校验失败 `ClaimIntegrityError` → 显式 rollback + raise，均无 partial writes）；`create_claim` 委托给 batch（单条语义不变）。
- **测试**：**49 项单元 + 15 项集成（`test_claim_analysis_service.py`）+ 4 项 batch 集成追加（`test_claim_service.py`）**，全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表；全量 **1253 非集成 + 295 集成通过**。
- **真实 DeepSeek smoke**（`app/cli/smoke_structured_claim_analysis.py`）：seed 真实 HTML 链 → E1..En 最小投影 → `DeepSeekClaimAnalysisModel.analyze` → schema 校验 → 打印摘要 → **清理全部 seed 数据（0 正式业务 Claim 残留）**。2026-08-09 实跑通过（model_id=deepseek:deepseek-v4-flash、relevant=true、1 条 fact claim supports E1）。运行（需 `DEEPSEEK_API_KEY`）：

```bash
conda run -n insightforge python -m app.cli.smoke_structured_claim_analysis
```

- **决策记录**：[docs/decisions/0025-structured-claim-analysis.md](docs/decisions/0025-structured-claim-analysis.md)。

## Financial Metric Observation Foundation（阶段 4B.2A）

阶段 4B.2A 把来源于真实财务 Evidence 的**原始财务数值**确定性登记为 `FinancialMetricObservation`（**Document Evidence → Observation**）。**状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Metric HTTP 端点）**。它是财务分析链条（4B.2A Observation → 4B.2B Deterministic Calculation → 4B.2C.1 Provenance → 4B.2C.2 Financial Analyst）的**确定性第一环**：**不计算**同比 / 环比 / margin / ratio、**不调用 LLM**、不自动从 PDF 表格猜财务数字；后续（4B.2B/4B.2C.1/4B.2C.2）只能基于这些已登记的确定性数值。

- **Migration 0020**（`alembic current` = 0020 head）：`financial_metric_observations` 表（source_evidence_card_id FK evidence_cards RESTRICT、metric_code、statement_scope、period_start NULL、period_end、period_kind、source_value_text、raw_value、raw_unit、normalized_value_cny、metric_fingerprint UNIQUE 等）+ 8 CHECK + 4 索引；**downgrade guard**（有行拒绝降级）。**11 个 metric_code**（revenue / operating_cost / operating_profit / profit_before_tax / net_profit / net_profit_parent / net_profit_parent_excl_nonrecurring / operating_cash_flow_net / total_assets / total_liabilities / equity_parent）、`statement_scope`（consolidated / parent）、`raw_unit` 4 档、**货币 v1 只支持 CNY**。
- **Period 规则（确定性）**：balance sheet → instant + period_start NULL；income / cash-flow → duration + period_start NOT NULL 且 <= period_end（metric_code → statement family 确定性 mapping，caller 不传 statement_type）。
- **Metric/scope 口径政策（Gate 0 B，结构化策略约束）**：`net_profit_parent` / `net_profit_parent_excl_nonrecurring` / `equity_parent` **只允许 `statement_scope=consolidated`**，parent → `FinancialMetricScopeError`（创建与 replay 均重新验证）。**不得声称等于自动识别报表口径**——只是结构化语义白名单，不推断真实口径。
- **Source value exactness（numeric-token）**：`source_value_text`（trim 后）必须等于 EvidenceCard `quote_text` 中**一个完整数字 token**（`find_financial_number_tokens`，与 `parse_financial_number` 同一 grammar）；0 个 → NotFound、>1 个 → Ambiguous；**禁止 substring partial match**（`"收入1000万元"` 里 "1000" 接受而 "100" / "000" 拒绝，"-123.45" / "(123.45)" 的符号与括号属于 token）；**不 fuzzy / 不 normalize / 不自动纠错 / LLM 不参与**。
- **确定性解析**（`app/financial/number_parser.py`）：`Decimal` 全程、**零 float**；严格语法拒绝科学计数法 / 百分号 / 中文数字 / 带单位 / 畸形千分位等；`normalize_value_cny` = ×1 / ×1000 / ×10000 / ×100000000；`validate_financial_decimal_storage`：raw_value 与 normalized_value_cny 落库前必须满足 **NUMERIC(38,12) 契约**（小数位 ≤ 12 且 abs < 10^26），超出 → `FinancialMetricStorageRangeError`（禁止静默 quantize / round / truncate）。
- **Fingerprint / replay**：`compute_metric_fingerprint` = canonical JSON + SHA-256（含 schema_version / company / source_evidence / metric_code / scope / period / source_value_text / raw_value / raw_unit / normalized_value_cny；**不含 metric_observation_id / created_at**）；同一完全相同 observation → replay 同一行；value / unit / period / metric code / scope / source evidence / company 任一变化 → **新行，旧行保留**；**无 update API**。Replay 逐项派生值比对，损坏 → `FinancialMetricIntegrityError`，**不自动 repair**。
- **Provenance**：Observation → EvidenceCard → DocumentChunk → ChunkSet → ParsedSource → SourceRecord → RawArtifact 全链路可回溯；**不复制 locator_refs**、不访问 Chroma / BGE / LLM / RawArtifact bytes。
- **测试**：**82 项单元 + 29 项集成**（真实 PG + 真实 HTML/PDF 服务链，零 Chroma/LLM：创建 / 拒绝 / numeric-token exactness / period / storage bounds / replay / 并发→1 / 变化→新行 / 篡改→IntegrityError / EvidenceCard 不变 / provenance 全链路 / 精确阶段边界 `claims==2 / report 表==0`）+ **2 项 migration 0020 downgrade guard**；全量测试在 Gate 0 提交前重跑确认。
- **当前保证 / producer 输入边界**：已实现 = Evidence/source provenance、exact numeric-token provenance、Decimal parse、period/type/unit 结构化校验、deterministic unit normalization；**尚未实现** = 从年报自动识别 `metric_code` / `statement_scope` / `period` / `raw_unit`（这些字段当前仍由 structured producer 输入）。未来 official structured financial provider / deterministic table extractor 可作为 producer（仍是确定性路径）。
- **边界**：不实现 Financial calculation（4B.2B，见下节）；不实现 Financial Analyst（4B.2C.2）；不接 LangGraph 分析节点；不开放 HTTP API；不自动从 PDF 表格猜财务数字。
- **决策记录**：[docs/decisions/0026-financial-metric-observation-foundation.md](docs/decisions/0026-financial-metric-observation-foundation.md)。

## Deterministic Financial Calculation（阶段 4B.2B）

阶段 4B.2B 把**已登记 FinancialMetricObservation** 通过**冻结公式**计算为派生财务事实（同比 / 环比 / margin / ratio），形成 **Calculation → Observation → EvidenceCard → Source** 证据链。**状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Calculation HTTP 端点）**。**0 LLM / 0 Chroma / 0 Analyst / 0 Claim / 0 Report / 0 Audit / 0 LangGraph analysis node**。角色边界：**LLM（4B.2C.2）不碰数值结果**——draft 只允许语义输入，result_value / result_unit / fingerprint 全由确定性代码派生。

- **Migration 0021**（`alembic current` = 0021 head）：`financial_calculations`（calculation_id PK、company_id FK RESTRICT、calculation_code、result_value NUMERIC(38,12)、result_unit cny/ratio、calculation_schema_version、formula_version、calculation_fingerprint CHAR(64) UNIQUE、created_at）+ `financial_calculation_inputs`（calculation_id FK CASCADE、input_role、metric_observation_id FK RESTRICT；PK(calculation_id, input_role)）+ CHECK（code / unit / fingerprint / schema / formula / role 白名单）+ 3 索引；**downgrade guard**（任一表有行拒绝降级）。
- **冻结契约**（`app/financial/calculations/contracts.py`）：`FINANCIAL_CALCULATION_SCHEMA_VERSION = 1`、`FORMULA_VERSION = 1`；**7 个 calculation_code**（absolute_change_cny / yoy_growth_rate / qoq_growth_rate / gross_margin / operating_margin / net_margin_parent / debt_to_assets_ratio）；result_unit 只有 cny / ratio（**ratio 存 0.1234 而非 12.34**）。**`FinancialCalculationDraft` 只允许语义输入**（company / code / input_observation_ids，role 集合必须与 code 完全一致），调用方**不得提供** result_value / result_unit / formula / Evidence ID / period metadata / fingerprint。
- **Comparability（I）**：company_id == draft（CompanyMismatch）；statement_scope 全部相同（ScopeMismatch）；metric_code 精确匹配 role 期望 / growth 类 current==baseline（InputMismatch）。
- **Period 规则（H-K）**：**absolute** = period_kind 相同；**YoY** = baseline 年份 = current 年份 - 1 且月/日对应；**QoQ** = duration 标准单季度（季首日 → 季末日）或 instant（period_end 为季末日）**且连续季度**（`year*4+quarter`）；**margin**（gross/operating/net_margin_parent）= duration 且所有输入 period_start / period_end 完全相同（同期口径）；**debt_to_assets_ratio** = instant 且 period_start 全为 None、period_end 完全相同（同一报告日）。违反 → PeriodMismatch。
- **公式（L-M，全程 Decimal、禁止 float）**：`absolute_change_cny` = current - baseline（精确减法）；`growth_rate` = (current-baseline)/baseline，**baseline 必须 > 0**（GrowthBaseNotPositive）；4 个 margin / ratio 分母必须 > 0（ZeroDenominator）。**除法 quantize 到 `CALCULATION_SCALE=12`、`ROUND_HALF_EVEN`**。**Storage bounds**：result_value 必须 `fits_numeric_38_12`（小数位 ≤ 12 且 abs < 10^26），否则 StorageRangeError（禁止静默 quantize / round / truncate）。
- **Fingerprint / replay（N-O）**：`compute_calculation_fingerprint` = canonical JSON + SHA-256（含 schema / formula / company / code / **按 input_role 排序的 (role, obs_id, obs fingerprint)** / result_value str() / result_unit；**不含 calculation_id / created_at**）；同一完全相同输入 → replay 同一行；输入任一变化（含上游 Observation 指纹变化）→ 新行，旧行保留；**无 update API**。Replay：**重新加载 Observation + 重新派生**，逐项核实 persisted 字段与 inputs 绑定，损坏 → IntegrityError，**不自动 repair**。
- **Persistence（P）**：`FinancialCalculationService.create_calculation(draft)` 三步（短 DB session 加载 Observation → 纯函数派生 → 短 DB transaction `create_or_get` ON CONFLICT DO NOTHING + 插入 inputs / replay）；`IntegrityError → rollback + raise`、`SQLAlchemyError → 整条 rollback + PersistenceFailed`（0 partial write）。**并发 → 最终 1 calculation**。构造函数只持有 sessionmaker。
- **测试（97 项新增）**：**78 项单元**（test_formulas 23 / test_contracts 32 / test_service_pure 23，零 DB）+ **17 项集成**（test_financial_calculation_service.py，真实 PG + 真实服务链：创建 / replay / 并发→1 / 篡改→IntegrityError / 全部 error paths / CASCADE）+ **2 项 migration 0021 downgrade guard**。全程 0 LLM / 0 Chroma query / 0 Claim / 0 Report 表。
- **当前保证 / producer 输入边界**：已实现 = 确定性公式 / comparability / period 规则 / storage bounds / fingerprint-replay / 并发幂等 / downgrade guard。**尚未实现** = 从年报自动识别 Observation 的 metric_code / scope / period / raw_unit（该边界属于 4B.2A producer 输入）；4B.2C.1 Financial Claim Provenance 已 completed（见下节），4B.2C.2 Structured Financial Analysis 已 completed（见下节）。
- **边界**：4B.2C.2 Structured Financial Analysis 已 completed（见下节）；不实现 Macro / Valuation（4C）；不接 LangGraph 分析节点；不开放 HTTP API；不调用 Retrieval / Chroma / BGE / LLM；不自动从 PDF 表格猜或验证财务数字（只消费已登记 Observation）。
- **决策记录**：[docs/decisions/0027-deterministic-financial-calculation.md](docs/decisions/0027-deterministic-financial-calculation.md)。

## Financial Claim Provenance（阶段 4B.2C.1）

阶段 4B.2C.1 把引用**已登记 FinancialCalculation** 的 Financial Claim 确定性登记为 Claim + 自动展开的 Evidence 链接 + Calculation 链接，形成 **Claim → ClaimFinancialCalculationLink → FinancialCalculation → FinancialMetricObservation → EvidenceCard → Source** 完整可重算证据链。**状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Claim HTTP 端点）**。**0 LLM / 0 Chroma query / 0 Retrieval / 0 LangGraph analysis node / 0 Report / 0 Audit**。角色边界：**调用方/未来 LLM 只选 Calculation refs；derived Evidence IDs 一律由程序自动展开，caller 不得手工伪造**。它是 4B.2C Financial Analyst（4B.2C.2，LLM 解释数值）的 provenance 基础——Audit 可确定性重算"结论所依赖的财务数值"。

- **Migration 0022**（`alembic current` = 0022 head）：`claim_financial_calculation_links`（claim_id FK claims **CASCADE**、calculation_id FK financial_calculations **RESTRICT**、relation supports/contradicts/context、created_at；PK(claim_id, calculation_id, relation)、**UNIQUE(claim_id, calculation_id)**、CHECK relation、索引 calculation_id）+ **Gate 0 C** `uq_financial_calculation_inputs_calc_observation`（UNIQUE(calculation_id, metric_observation_id)，同一 calculation 内同一 Observation 只能绑定一个 role）。**不把 FinancialCalculation 伪装成 EvidenceCard**（Calculation = derived fact，EvidenceCard = source-backed fact，保持分层）。**downgrade guard**：links 有行拒绝回滚（不静默丢弃 Claim ↔ Calculation 链接），空表允许。
- **Schema v2**（`app/claims/financial_contracts.py`）：`FINANCIAL_CLAIM_SCHEMA_VERSION = 2`——只有存在 Calculation links 的 financial Claim 用 v2；**禁止回头改变 v1 Claim fingerprint**（v1 Claims 继续用 `compute_claim_fingerprint` 正常 replay）。**v2 fingerprint = v1 内容 + 按 relation 排序的 calculation lists**（每 entry 至少 `calculation_id` + `calculation_fingerprint`）；canonical JSON + SHA-256；**不含 claim_id / created_at**。同一完全相同 Financial Claim → 同一指纹 → replay 同一行；任一变化 → 新指纹 → 新行，旧行保留（无 update API）。
- **`FinancialClaimDraft` 专用新类**（**不污染 ClaimDraft**）：固定 analysis_domain=financial；claim_kind ∈ fact / inference / risk（**不做 relative_valuation**，估值留 4C）；**至少 1 个 support_calculation_id**；同一 calculation / additional Evidence 不能跨 relation 重复；id list 去重 + canonical 排序。
- **Automatic Evidence expansion（H）**：调用者只选 Calculation refs；程序加载每个 Calculation 的 source Evidence（inputs → Observations → source_evidence_card_id）自动加入 ClaimEvidenceLinks（supports→supports 等）。`additional_*_evidence_ids` **仅用于管理层解释 / 业务事件 / 风险说明**等额外定性 Evidence。
- **Relation propagation（I）**：Calculation relation → underlying source Evidence 用**同 relation**；同一 Evidence 被推导成不同 relation → `FinancialClaimRelationConflict`（**不静默选一个**）；同一 relation 重复（共享 Evidence）→ 幂等去重；additional Evidence 与自动推导 relation 冲突 → reject。
- **Company / integrity validation（J，不 repair）**：每个 Calculation 逐个执行 `FinancialCalculationService.verify_calculation_integrity`（重新加载 inputs + Observations + 重新派生逐项核实）——缺失 → `FinancialClaimCalculationNotFound`、company != draft → `FinancialClaimCalculationMismatch`、重放损坏 → `FinancialClaimIntegrityError`；inputs → Observations（company 一致）→ source Evidence（缺失 / 跨公司 → `FinancialClaimEvidenceCompanyMismatch`）；additional Evidence 同样校验。
- **Critical policy（K）**：**FinancialCalculation 本身不能提升 source authority**；critical 仍需 **≥1 个最终 supports Evidence** 满足 `critical_claim_eligible_snapshot=true`（自动展开 + additional 合并后），否则 `FinancialClaimCriticalEvidenceInsufficient`（复用 ClaimService source policy）。
- **Persistence（L）**：`FinancialClaimService.create_claim(draft)`（构造函数只持有 sessionmaker）三步——短 DB session 加载 + 校验（连接即刻关闭）→ 纯函数派生（自动展开 / relation / critical / v2 fingerprint）→ 短 DB transaction `ClaimRepository.create_or_get`（PG `ON CONFLICT(claim_fingerprint)`，无进程锁）→ created=True 插入 evidence links + calculation links；created=False 时 `_verify_replay`；`FinancialClaimIntegrityError → 显式 rollback + raise`、`SQLAlchemyError → 整条 rollback + FinancialClaimPersistenceFailed`（**0 partial write**，Claim + 两类 links 一个事务）。**并发相同 fingerprint → 最终 1 Claim + 1 套 links**。**无 update API**。
- **Replay（M，不 repair）**：schema v2 replay 重新加载 Claim / evidence links / calculation links / Calculations / inputs / Observations / EvidenceCards，重新执行 Calculation integrity、自动 expansion、critical policy、relation conflict、v2 fingerprint，逐项核实；任一损坏 → `FinancialClaimIntegrityError`，**不自动 repair**（修改 = 新 Claim = 新行）。
- **错误分类**（`app/claims/financial_errors.py`，9 个 + 稳定 code）：FinancialClaimError 基类 + DraftError / CalculationNotFound / CalculationMismatch / EvidenceCompanyMismatch / RelationConflict / CriticalEvidenceInsufficient / IntegrityError / PersistenceFailed。
- **测试（21 项集成新增 + Gate 0 追加）**：`tests/integration/test_financial_claim_service.py`（真实 PG + 真实服务链 seed company/evidence/observation/calculation，零 Chroma/LLM：valid persistence、requires support calculation、calculation missing / company mismatch、calculation corruption → replay IntegrityError（不 repair）、automatic Evidence expansion、共享 Evidence 同 relation 去重、conflicting propagated relation → reject、additional Evidence merged / conflict / missing、critical eligible accepted、critical without eligible source → reject、schema v2 fingerprint deterministic、calculation change → new Claim、relation change → new Claim、replay、concurrency → 1 Claim、corruption → integrity error、no EvidenceCard / FinancialCalculation modified、E2E provenance SQL trace、精确阶段边界）+ **Gate 0 追加**（margin 同期间期：跨期拒绝 / 同期望通过 / 必须 duration；debt_to_assets_ratio 同时点：跨时点拒绝 / 同时点通过 / 必须 instant；metric/scope 政策：net_profit_parent / excl_nonrecurring / equity_parent 只允许 consolidated → `FinancialMetricScopeError`；calculation input distinctness：draft 拒绝重复 observation + DB UNIQUE 拒绝直接 SQL 重复绑定）。全量 **1416 非集成 + 383 集成通过**。全程 0 LLM / 0 Chroma query / 0 Retrieval / 0 LangGraph / 0 Report 表。
- **边界**：4B.2C.2 Structured Financial Analysis（LLM 解释数值）已 completed（见下节）；不实现 Macro / Valuation（4C）；不实现 Claim 综合 / 冲突 / 证据缺口（4D）；不接 LangGraph 分析节点；不开放 HTTP API；不调用 Retrieval / Chroma / BGE；不生成 Report / DraftSection / ReviewIssue / Audit。
- **决策记录**：[docs/decisions/0028-financial-claim-provenance.md](docs/decisions/0028-financial-claim-provenance.md)。

## Structured Financial Analysis（阶段 4B.2C.2）

阶段 4B.2C.2 把 4B.2C.1 的 provenance 基础接上 LLM：**Financial Calculation[] + research question → DeepSeek 结构化分析 → FinancialClaimCandidate[] → 确定性 alias/ref resolution → FinancialClaimDraft(v3) → `FinancialClaimService.create_claim_batch` 原子持久化**，是 4B.2 Financial 链条的收尾。**状态（2026-08-09）：implementation completed / automated tests completed / live acceptance not required（不开放 Financial Analysis HTTP 端点）**。无新 migration（`alembic current` 保持 0022 head）。角色边界：**Analyst 只做判断**（相关性 / statement / kind / confidence / importance / C·E 编号引用），**确定性交给代码**（C/E alias、ref resolution、numeric-literal guard、v3 fingerprint、create_claim_batch 原子持久化）；**不计算任何财务指标、不修改公式结果、不做宏观因果 / 估值**。

- **包位置**：`app/analysis/financial/`（contracts / errors / packs / prompt / adapters / factory / service，镜像 4B.1 `app/analysis/claims/` 结构）。**冻结身份**：`FINANCIAL_ANALYST_NAME="structured_financial_analyst"`、`FINANCIAL_ANALYST_VERSION=1`、analysis_domain=financial；persisted `analyst_model_id`（=`provider:model` = `deepseek:deepseek-v4-flash`）一并落库。
- **请求 / 决策契约**（`contracts.py`）：`FinancialAnalysisRequest`（calculation_ids 1..20、additional_evidence_ids 0..20、去重 + canonical 排序）；`FinancialClaimCandidate`（claim_kind **只允许 inference/risk**——**schema 层拒绝 fact 与 relative_valuation**：定量事实由确定性 `FinancialCalculation` 承担，Analyst 只解释并判断风险，`FinancialClaimDraft` 更低层契约仍支持 fact 但 service `_check_kind_policy` 兜底；每条 ≥1 support_calculation_ref，Calculation ref 格式 `C<number>` / Evidence ref 格式 `E<number>`，**无 reasoning / CoT / UUID / formula / result_value rewrite / fingerprint**）；`FinancialAnalysisDecision`（relevant=false→空 claims + 可选 reason_code；relevant=true→1..3 claims + 无 reason_code）；`MAX_CLAIMS_PER_DECISION=3`。
- **LLM 前验证（F）**：先对每个 Calculation 执行 `verify_calculation_integrity`（缺失/跨公司/重放损坏 → 稳定错误）+ 加载 inputs → Observations 与 additional Evidence；**校验通过后关闭 DB session**——LLM 调用期间不持有 DB transaction / connection。
- **Calculation/Evidence Pack 最小投影（G-H）**：`build_calculation_pack` 按 `str(calculation_id)` 升序编号 **C1..Cn**（确定性），只含必要字段（result_value 按存储表达、result_unit、formula_version、period_summary、statement_scope 从 inputs 派生、deterministic_display_value 程序生成、inputs 摘要 unit=CNY）；**不发送** UUID / fingerprint / observation UUID / locator / raw / Chroma。`build_evidence_pack_allowing_empty` 复用 4B.1 EvidencePack（E1..En），additional evidence 允许 0 条。
- **Numeric-literal guard（J）**：statement 禁止 ASCII/full-width digits `０-９` / `%％` / 中文数字字符（`零〇二两三四五六七八九十百千万亿兆`）/ 定量短语（`百分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数 / 一成 / 一半 / 一点`），违反 → `FinancialAnalysisNumericLiteralForbidden` 整次 0 写；**不自动删数字、不改写、不让第二个 LLM 修正**；**`一/点` 本身允许**（"一定/进一步/观点"等非数量词可用）。展示值（ratio 0.2 → "20.00%" 用 `ROUND_HALF_EVEN` quantize，cny → "X CNY"）程序生成。
- **Ref resolution（L）**：C → calculation_id、E → evidence_card_id，**不 fuzzy resolve**；未知 → `FinancialAnalysisUnknownRef`、跨 relation → `FinancialAnalysisRelationConflict`；组内去重 + canonical 排序；**任一 candidate 无效 → 整次 0 写**。
- **Service 10 步流程（O）**（`service.py`）：request 校验 → 短 DB session 加载校验 → 关闭 session → 构造 C/E alias → 调 `FinancialAnalysisModel.analyze` → double-check schema（`ValidationError → FinancialAnalysisMalformedOutput`）→ relevant=false 0-claims → numeric guard → ref resolution → 构造全部 v3 drafts（analyst 身份固定 + `_check_kind_policy` 兜底）→ `create_claim_batch`。**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph；不调用 tools / web search。
- **LLM 抽象 + 生产适配器（P）**：`FinancialAnalysisModel` Protocol；`FakeFinancialAnalysisModel`（自动化测试，0 真实 LLM）；`DeepSeekFinancialAnalysisModel` = 懒加载 `ChatDeepSeek` + `with_structured_output`，temperature=0.0 + **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`——V4 Flash 默认 thinking，temperature=0 不等于关闭），只启用 structured-output、不绑定 tools/web search；`model_id = provider:model`；`OutputParserException→MalformedOutput`、其余→`ModelUnavailable`。
- **Gate 0（A-C）随 4B.2C.2 交付**：**A** Calculation 承担 supports/contradicts/context 语义，v3 下 automatic source Evidence expansion 一律 relation=context（source Evidence 只提供 provenance context），additional Evidence 保持指定 relation；**B** `FINANCIAL_CLAIM_SCHEMA_VERSION=3`，新创建 Claims 全 v3，v2 保持 legacy replay semantics，**无新 migration**（Alembic 0022），v2/v3 fingerprint 含 schema_version 不 collision；**C** v3 critical Claim 需任一 support Calculation 的 source Evidence eligible 或 additional_support_evidence_ids 中有 eligible，否则 `FinancialClaimCriticalEvidenceInsufficient`；**Calculation 本身绝不提升 authority**。
- **原子批量持久化（N）**：`FinancialClaimService.create_claim_batch(drafts)`（1..3）——all-drafts-validate-first + 单 transaction + ordered result（`.items[i]` 对应 `drafts[i]`）；`IntegrityError → rollback + raise`、`SQLAlchemyError → rollback + PersistenceFailed`；**无 compensating delete**。
- **错误分类**（`app/analysis/financial/errors.py`，11 个 + 稳定 code）：InputError / CalculationNotFound / CalculationCompanyMismatch / CalculationCorrupted / EvidenceCompanyMismatch / MalformedOutput / NumericLiteralForbidden / UnknownRef / RelationConflict / ClaimKindPolicy / ModelUnavailable；错误消息不泄露 calculation / evidence 正文、prompt、key、DB URL、raw provider response。
- **测试**：**73 项单元**（`tests/analysis/financial/`：contracts / packs / prompt，零 DB + fake；含 claim-kind fact→reject / inference·risk→accept、numeric guard 全覆盖）+ **20 项集成**（`tests/integration/test_financial_analysis_service.py`：v3 claim 落库（analyst 身份 / model_id / v3 schema / calculation links / source 自动 context + additional context）、relevant=false 0-claims、Calculation 缺失/跨公司/重放损坏/additional Evidence 缺失 → 稳定错误且不调用 LLM、numeric guard / unknown C ref / cross-relation conflict → 0 写、fact candidate → 0 写、中文数字 statement → 0 写、kind-policy 兜底、合法 inference/risk → 落库、malformed / model unavailable、critical 缺 eligible → 0 写、replay 同决策 → replayed_count=1 同 claim_id、多 claims ordered、smoke cleanup 0 残留、最小 pack 投影）。全量 **1507 非集成 + 463 集成通过**（含 4C.1A 的 18 项单元 + 44 项集成 + 5 项 migration 0024 downgrade guard，见 ADR-0030 / ADR-0031）。全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表。
- **真实 DeepSeek 受控 smoke**（validation 阶段，走生产适配器 `DeepSeekFinancialAnalysisModel`，不得直接调用 SDK）：seed 真实 HTML 链 → Observation → Calculation（C1 yoy_growth_rate 0.2 → "20.00%"、C2 operating_margin 0.15 → "15.00%"）→ `FinancialAnalysisService.analyze` → 校验 schema / numeric guard / ref resolution → 打印摘要（provider / model / latency_ms / claim_count / claim_kinds / numeric_guard_success / ref_resolution_success / cleanup_success）→ **清理全部 seed 数据并实际查询 0 残留**。**不记录** API key / 完整 prompt / reasoning_content / raw provider response。**开发发现（第一次 smoke，非 accepted output）**：模型输出 fact/high Claim（含"处于健康水平"、"盈利能力较强"评价词）→ 暴露 claim-kind / 评价词边界问题 → 收紧为 inference/risk only + prompt 禁止无基准评价词。**修正后 smoke（2026-08-09，accepted output）**：provider=deepseek、model=`deepseek:deepseek-v4-flash`、latency_ms=10664、relevant=true、claim_count=2、claim_kinds=`['inference','inference']`、numeric_guard_success=true、ref_resolution_success=true、cleanup_success=true；2 条 statement 全部纯定性无数字形式且 refs 落在 C1/C2；清理确认 **0 残留**。
- **边界**：不实现 Macro / Valuation（4C）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不接 LangGraph workflow integration；不开放 HTTP API；不生成 Report / DraftSection / ReviewIssue / Audit；不实现自动交易 / 技术分析 / 短期预测 / 买卖建议；**不开始 4C**（4B.2 = FINAL；4C.1A 见下节）。
- **决策记录**：[docs/decisions/0029-structured-financial-analysis.md](docs/decisions/0029-structured-financial-analysis.md)。

## Macro Transmission Provenance（阶段 4C.1A）

阶段 4C.1A 建立 Macro Claim 的传导 provenance 基础：**Macro Evidence + Company Exposure Evidence → Macro Transmission Chain → Macro Claim**，使 Audit 可回溯"宏观变量如何传到公司"。**状态（2026-08-10）：implementation completed / automated tests completed / live acceptance not required（不开放 Macro Claim HTTP 端点）——4C.1A = FINAL（v2 Closeout）**。migration **0023**（`macro_transmission_chains` + `macro_transmission_evidence_links`，downgrade guard 拒绝有数据时回滚）+ **0024**（Closeout：`transmission_fingerprint` 从 global UNIQUE 改为普通索引；同 semantics + 不同 statement / analyst_version → 新 Claim + 新链，旧链保留；downgrade 拒绝 v2 链 / v5 macro Claim / 重复 fingerprint）。**0 LLM / 0 Chroma / 0 Retrieval / 0 LangGraph / 0 Valuation / 0 Report / 0 Audit**。

- **核心边界**：Macro Evidence = 来源支撑的宏观事实（origin_type=macro_observation，真实 World Bank provenance）；Company Exposure Evidence = 来源支撑的公司事实（origin_type=document_chunk）；**Macro Transmission = 分析产物（利率 → financing channel → 公司有息负债 → 融资成本压力），不是 EvidenceCard**。
- **`MacroClaimService.create_claim(draft)`**（`app/services/macro_claim_service.py`）：短 DB session 加载并校验全部 Evidence（存在 / 公司隔离 / 按角色 origin v2 / availability / time-alignment / impact-status / critical）→ 纯函数派生（transmission fingerprint + macro claim fingerprint + context expansion）→ 单短 PG transaction 原子落库（Claim + MacroTransmissionChain + transmission links + ClaimEvidenceLinks，0 partial write）→ replay 版本感知核实（v5 → v2 规则、v4 → v1/v4 legacy 规则，损坏 → `MacroClaimIntegrityError`，不自动 repair）。
- **契约**（`app/claims/macro_contracts.py`）：**4C.1A closeout 时为 `MACRO_CLAIM_SCHEMA_VERSION=5` / `MACRO_TRANSMISSION_SCHEMA_VERSION=2`；冻结 legacy `V4=4` / `V1=1`，fingerprint 含 schema version 永不 cross-collision**（4C.1B 已提升当前为 v6/v3，见下节）；`MacroClaimDraft`（claim_kind 只允许 inference/risk；macro_driver ≥1 + company_exposure ≥1；角色互斥 / additional 互斥；证据 id 去重 + canonical 排序）；channel_type ∈ revenue/cost/financing/demand/supply_chain/trade_policy/operations/other；effect_direction ∈ tailwind/headwind/mixed/uncertain（**不是 buy/sell**）；impact_status ∈ plausible_impact/observed_impact；time_alignment ∈ aligned/uncertain（**无 misaligned**）。
- **Evidence 校验（真实 PG，不信任调用方）**：全部引用卡存在（缺失 → `MacroClaimEvidenceNotFound`）、全部卡 company 一致（跨公司 → `MacroClaimEvidenceCompanyMismatch`）、按角色 origin v2（macro_driver 允许 macro_observation，**或** `news_article` + evidence_type ∈ {event, fact, statement} 的 external event document；company_exposure / observed_effect 必须 document_chunk；违反 → `MacroClaimOriginViolation`）。**不查 Chroma、不接受调用方提供的 provider/authority/provenance**。
- **Information availability（no-lookahead，v2 收口）**：**全部**进入 Claim 的 Evidence availability `<= analysis_as_of`（未来 → `MacroClaimFutureEvidence`）；document 用 `SourceRecord.published_at` 否则 `acquired_at`（**绝不用 reporting_period_end**）；macro 用 `MacroDatasetSnapshot.fetched_at`（**绝不用 normalized_period_start**）；无可用时间 → `MacroClaimTemporalEvidenceInsufficient`（`acquired_at` / `fetched_at` 均 NOT NULL，该分支为防御性）。
- **time-alignment / overclaim policy v2**：observed_impact 必须 time_alignment=aligned；uncertain 只允许 plausible_impact + risk + normal（违反 → `MacroClaimTimeAlignmentPolicy`）；critical 需要 aligned **且** effect_direction != uncertain + eligible 双腿（`MacroClaimCriticalEvidenceInsufficient`）；observed_impact 无 observed_effect → `MacroClaimImpactStatusInsufficient`。**不自动猜 lag / 不降级**。
- **relation 语义**：macro_driver / company_exposure / observed_effect 自动展开为 ClaimEvidenceLinks 一律 relation=context；additional 保持 supports/contradicts/context。
- **测试**：**18 项单元**（`tests/claims/test_macro_contracts.py`：draft 校验 / 枚举白名单 / fingerprint 确定性 + schema version 无 cross-collision / transmission fingerprint 排除 statement + analyst）+ **44 项集成**（`tests/integration/test_macro_claim_service.py`：创建 / origin（含 v2 document driver）/ availability / temporal / time-alignment policy / critical / impact-status / fingerprint ownership / replay+并发+篡改（含 legacy v1/v4 仍 valid）/ additional relation / E2E provenance / 边界）+ **5 项 migration 0024 downgrade guard**（`tests/integration/test_migration_0024_downgrade_guard.py`，isolated 临时 PG），全程 0 真实 LLM / 0 Chroma / 0 LangGraph / 0 Report 表。
- **边界**：不创建 Report / DraftSection / ReviewIssue / Audit；不接 LangGraph 分析节点；不改动 generic Claim schema；不批量 update 历史 v4/v1 rows；不引入 transmission ↔ claim join table。
- **决策记录**：[docs/decisions/0030-macro-transmission-provenance.md](docs/decisions/0030-macro-transmission-provenance.md)、[docs/decisions/0031-macro-transmission-v2-closeout.md](docs/decisions/0031-macro-transmission-v2-closeout.md)。

## Structured Macro Context Analyst（阶段 4C.1B）

阶段 4C.1B 把 4C.1A 的传导 provenance 基础接上 LLM：**Macro Evidence + Company Evidence + research question → DeepSeek 结构化 Macro Context 分析 → MacroClaimCandidate[] → 确定性 M/E alias + numeric-literal guard + ref resolution → v6 Macro Claim**，是 4C 宏观看板的第一个分析链路。**状态（2026-08-10）：implementation completed / automated tests completed / real DeepSeek V4 Flash controlled smoke passed / live acceptance not required（不开放 Macro Analysis HTTP 端点）**。migration **0025**（Gate 0：`macro_transmission_chains.analysis_as_of` 查询列 + CHECK + INDEX + downgrade guard；**不 backfill、绝不从 created_at / published_at / reporting_period_end / fingerprint 反推历史 cutoff**）。版本边界：**当前 `MACRO_CLAIM_SCHEMA_VERSION=6` / `MACRO_TRANSMISSION_SCHEMA_VERSION=3`**；冻结 legacy `V5=5` / `V2=2` / `V4=4` / `V1=1`。角色边界：**Analyst 只做判断**（相关性 / statement / kind / confidence / importance / M·E 编号引用），**确定性交给代码**（M/E alias、numeric-literal guard、ref resolution、v6 fingerprint、create_claim_batch 原子持久化）；**不计算任何宏观指标、不做宏观因果 / 估值**。

- **包位置**：`app/analysis/macro/`（contracts / errors / packs / prompt / model / adapters / factory / service，镜像 `app/analysis/financial/` 结构）。**冻结身份**：`MACRO_ANALYST_NAME="structured_macro_context_analyst"`、`MACRO_ANALYST_VERSION=1`、analysis_domain=macro；persisted `analyst_model_id`（=`provider:model` = `deepseek:deepseek-v4-flash`）一并落库。
- **请求 / 决策契约**（`contracts.py`）：`MacroAnalysisRequest`（macro_driver_evidence_ids 1..20、company_evidence_ids 1..30、两池**不重叠**、去重 + canonical 排序）；`MacroClaimCandidate`（claim_kind **只允许 inference/risk**——**契约层直接拒绝 fact**：宏观定量事实由 Macro Evidence 承载，Analyst 只解释并判断风险，`MacroClaimDraft.__post_init__` 与 service `_check_kind_policy` **双重拒绝 fact（无任何下层契约接受 fact）**；每条 ≥1 macro_driver_ref（`M<number>`）+ ≥1 company_exposure_ref（`E<number>`），**无 reasoning / CoT / UUID / fingerprint**）；`MacroAnalysisDecision`（relevant=false → 空 claims + 可选 reason_code ∈ {not_relevant, insufficient_macro_evidence, insufficient_company_evidence}；relevant=true → 1..3 claims + 无 reason_code）；`MAX_CLAIMS_PER_DECISION=3`。
- **LLM 前验证（真实 PG）**：短 DB session 加载并校验全部 Macro Evidence（macro_driver 池）+ Company Evidence（company 池）——任一缺失 → `EvidenceNotFound`、跨公司 → `CompanyMismatch`、provenance 链缺失 → `Corrupted`；macro_driver 池逐条满足 v2/v3 资格（macro_observation 或 news_article + evidence_type ∈ {event, fact, statement}，违反 → `OriginViolation`）；company 池每条 origin_type=document_chunk；全部 availability 解析（缺失 → `TemporalInsufficient`）、任何 future（availability > analysis_as_of）→ `FutureEvidence`；**校验通过后关闭 DB session**——LLM 调用期间不持有 DB transaction / connection。
- **Pack 最小投影 + M/E alias**：`build_macro_driver_pack` 按 `str(evidence_card_id)` 升序编号 **M1..Mn**、`build_company_evidence_pack` 同法编号 **E1..En**（确定性，两池 namespace **严格分离**）；只发送人类可读摘要与确定性字段（evidence_statement / evidence_type / provider_key / authority_tier_snapshot / availability / quote_text / indicator / value_summary 等）；**不发送** UUID / fingerprint / source UUID / observation UUID / locator / raw / Chroma。
- **Numeric-literal guard v1（独立于 Financial guard）**：statement 禁止 ASCII/full-width digits / `%％` / 中文数字字符（`零〇二两三四五六七八九十百千万亿兆`）/ 定量短语（`百分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数 / 一成 / 一半 / 一点`）/ numeric-context（`第?+一+季/月/年/期/日/号`），违反 → `MacroAnalysisNumericLiteralForbidden` 整次 0 写；**不自动删数字 / 不改写 / 不让第二个 LLM 修正**；**`一/点` 本身允许**（"一定/进一步/观点"等非数量词可用）。
- **Ref resolution + overclaim contract**：M/E → UUID 确定性映射（**不 fuzzy resolve**）；未知 → `MacroAnalysisUnknownRef`、跨 relation → `MacroAnalysisRelationConflict`；组内去重 + canonical 排序；**任一 candidate 无效 → 整次 0 写**。overclaim contract 防线：`observed_impact` 需 ≥1 `observed_effect_ref`（否则只能 plausible_impact）；`time_alignment=uncertain` 只允许 plausible + risk + normal；`_check_overclaim_policy` / `_check_kind_policy` 双重兜底。
- **共享纯函数**：`resolve_availability` / `driver_evidence_eligible` 位于 `app/claims/macro_policy.py`，由 `MacroClaimService` 与 `MacroAnalysisService` **共用**同一 no-lookahead / driver 资格策略（document 用 `published_at` 否则 `acquired_at`，macro 用 `fetched_at`，**绝不用 reporting_period_end / normalized_period_start**），禁止重复实现。
- **Service 10 步流程**（`service.py`）：request 校验 → 短 DB session 加载校验 → 关闭 session → 构造 M/E alias → 调 `MacroAnalysisModel.analyze` → double-check schema（`ValidationError → MacroAnalysisMalformedOutput`）→ relevant=false 0-claims → numeric guard → M/E ref resolution + overclaim policy → 构造全部 v6 drafts（analyst 身份固定 + `_check_kind_policy` 兜底）→ `MacroClaimService.create_claim_batch`（1..3 drafts，单 transaction）。**不创建 Report / DraftSection / ReviewIssue / Audit**；不接 LangGraph；不调用 Retrieval / Chroma / RawArtifact / tools / web search。
- **LLM 抽象 + 生产适配器**：`MacroAnalysisModel` Protocol；`FakeMacroAnalysisModel`（自动化测试，0 真实 LLM）；`DeepSeekMacroAnalysisModel` = 懒加载 `ChatDeepSeek` + `with_structured_output(MacroAnalysisDecision)`，temperature=0.0 + **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`），只启用 structured-output、不绑定 tools/web search；`model_id = provider:model`；`OutputParserException→MalformedOutput`、其余→`ModelUnavailable`；`factory.create_macro_analysis_model` 按 `llm_provider` 分发（未知 → `UnsupportedLLMProviderError`）。
- **持久化 + replay（版本感知）**：`MacroClaimService.create_claim_batch`（1..3 drafts，all-drafts-validate-first + 单 transaction + ordered result，0 partial write）；replay 按既有 claim_schema_version 分叉——v6 → 当前 v3/v6 规则（**额外核验 chain.analysis_as_of == draft.analysis_as_of**）、v5 → v2/v5 规则（analysis_as_of=NULL 允许，不反推 cutoff）、v4 → v1/v4 legacy 规则、其他 → `MacroClaimIntegrityError`，**不自动 repair**。
- **真实 DeepSeek 受控 smoke**（validation 阶段，走生产适配器 `DeepSeekMacroAnalysisModel`，不得直接调用 SDK）：seed 真实 HTML 链（news_article + event 宏观驱动卡 + company 暴露卡，语句纯定性无数字）→ `MacroAnalysisService.analyze` → 校验 schema / numeric guard / ref resolution / v6 claim + v3 transmission + analysis_as_of 查询列 → 打印摘要（provider / model / latency_ms / relevant / claim_count / claim_kinds / claim_schema_version / transmission_schema_version / analysis_as_of_persisted / numeric_guard_success / ref_resolution_success / cleanup_success）→ **清理全部 seed 数据并实际查询 0 残留**。**不记录** API key / 完整 prompt / reasoning_content / raw provider response。**smoke（2026-08-10，accepted output）**：provider=deepseek、model=`deepseek:deepseek-v4-flash`、latency_ms=8720、relevant=true、claim_count=2、claim_kinds=`['inference','risk']`、numeric_guard_success=true、ref_resolution_success=true、claim_schema_version=6、transmission_schema_version=3、analysis_as_of_persisted=2026-08-10、cleanup_success=true；2 条 statement 全部纯定性无数字形式且 refs 落在 M1/E1；清理确认 **0 残留**。
- **测试**：**75 项单元**（`tests/analysis/macro/`：contracts / packs / prompt / factory，零 DB + fake；含 claim-kind fact→reject、M/E ref 格式、numeric guard 全覆盖、两池不重叠、overclaim contract）+ **24 项集成**（`tests/integration/test_macro_analysis_service.py`：10 步流程 E2E（v6 claim + v3 transmission + analysis_as_of 查询列落库）、news_article event driver happy path、最小投影、relevant=false、上游加载失败不调用 LLM、numeric guard / unknown M·E ref / cross-relation / malformed / provider unavailable → 0 写、observed_impact 合法落库、critical 缺 eligible → 0 写、replay 同决策同 claim_id、multiple claims、防御性兜底）+ **5 项 migration 0025 downgrade guard**（`tests/integration/test_migration_0025_downgrade_guard.py`，isolated 临时 PG：空库降级成功 / v3 链拒绝 / v6 macro Claim 拒绝 / safe legacy v2/v5 降级成功且对象保留 / CHECK 拒绝 v3 链缺 cutoff）。全量 **1582 非集成 + 492 集成通过**（含 4C.1B 的 75 项单元 + 24 项集成 + 5 项 migration 0025 downgrade guard，见 ADR-0032）。全程 0 真实 LLM（自动化测试）/ 0 Chroma / 0 LangGraph / 0 Report 表。
- **边界**：不实现 Valuation（4C.2）；不实现 Claim Synthesis / Conflict / Evidence Gap（4D）；不接 LangGraph 分析节点（Service 层供未来 LangGraph 顶层编排调用）；不开放 HTTP API；不生成 Report / DraftSection / ReviewIssue / Audit；不改动 generic Claim schema；不批量 update 历史 rows；**不开始 4C.2**。
- **决策记录**：[docs/decisions/0032-structured-macro-context-analyst.md](docs/decisions/0032-structured-macro-context-analyst.md)。

## 完整系统（Docker Compose）

```bash
docker compose up -d --build backend
```

backend 容器：宿主机 8001 → 容器 8000。

## 迁移与质量检查

```bash
# Alembic
conda run -n insightforge alembic -c backend/alembic.ini upgrade head
conda run -n insightforge alembic -c backend/alembic.ini current

# 静态检查
conda run -n insightforge ruff check backend
conda run -n insightforge ruff format --check backend

# 单元测试（不连真实服务，默认跳过集成测试）
conda run -n insightforge python -m pytest -c backend/pyproject.toml backend/tests -v

# 集成测试（需 PostgreSQL 与 Chroma 已启动）
conda run -n insightforge python -m pytest -c backend/pyproject.toml backend/tests/integration -m integration -v
```

## 阶段 0 验收

阶段 0 的工程基座验收记录见 [docs/stage-0-acceptance.md](docs/stage-0-acceptance.md)。
