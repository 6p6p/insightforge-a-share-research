# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统。

> 当前进度：阶段 2A 基础（公司标准身份 + Source Registry）、阶段 2B.1（原始文件归档 + 来源登记）与阶段 2B.2A（官方披露发现契约 + 可行性探测）**已实现**，阶段 1B/1C 提供 LangGraph 模拟工作流基础。核心证据链（Evidence → Claim → Report → Audit）、真实 Agent、RAG、业务研报生成与前端**尚未实现**：不自动抓取公告、不同步公司目录、不解析 PDF 正文、不接入 LLM。当前 FastAPI 应用提供健康检查、研究任务、模拟工作流、来源登记与原始文件归档接口，并具备 PostgreSQL 与 Chroma 的持久化基础设施。

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
- 接入形态决策采用严格不变量：`direct_pdf_verified=false` 不得判为 `public_direct_pdf_only`；`matching_candidate_count=0` 不得判为 `public_server_rendered_html`；仅出现"登录/注册"不得判为需要认证。
- 自动发现仅限 `documented_api` 与 `public_server_rendered_html` 两种接入形态；未确认合规通路的形态不实现生产 Adapter。
- 探测边界、决策规则与人工验收门槛见 [docs/decisions/0010-official-disclosure-discovery-policy.md](docs/decisions/0010-official-disclosure-discovery-policy.md)。

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
