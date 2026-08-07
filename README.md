# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统。

> 当前进度：阶段 2A 基础（公司标准身份 + Source Registry）、阶段 2B.1（原始文件归档 + 来源登记）、阶段 2B.2A（官方披露发现契约 + 可行性探测）与阶段 2C.1（Macro Provider 契约 + World Bank Indicators Provider，实现与自动化测试已完成、真实验收待网络环境）**已实现**，阶段 2C.2A（宏观持久化数据模型 + RawArtifact JSON 泛化）**已实现**，阶段 1B/1C 提供 LangGraph 模拟工作流基础。核心证据链（Evidence → Claim → Report → Audit）、真实 Agent、RAG、业务研报生成与前端**尚未实现**：不自动抓取公告、不同步公司目录、不解析 PDF 正文、不接入 LLM。当前 FastAPI 应用提供健康检查、研究任务、模拟工作流、来源登记与原始文件归档接口，并具备 PostgreSQL 与 Chroma 的持久化基础设施。

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

5. 启动 FastAPI：

```bash
conda run -n insightforge python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001 --reload
```

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
