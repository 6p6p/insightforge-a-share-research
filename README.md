# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统。

> 当前处于**阶段 0：工程基座**。核心证据链（Source → Evidence → Claim → Report → Audit）、LangGraph 编排、Agent、RAG、业务研报生成与前端均**尚未实现**。当前可用的 FastAPI 应用提供健康检查接口，并具备 PostgreSQL 与 Chroma 的持久化基础设施（可启动、可迁移、可被 ready 探测）。

## 目录职责

```
backend/            Python 后端（FastAPI）工程，依赖由 backend/pyproject.toml 管理
backend/alembic/    Alembic 迁移（当前为空 baseline）
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
- `GET /api/v1/health/ready` → 检查 `configuration`、`database`、`chroma` 三项：
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
