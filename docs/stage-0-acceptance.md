# 阶段 0 验收：工程基座

> 日期：2026-08-06
> 状态：已通过

## 目标

建立 InsightForge 可测试、可配置、可容器化运行的工程基座：FastAPI 应用、配置加载、结构化日志、请求追踪、健康检查、PostgreSQL 与 Chroma 基础设施、Alembic 迁移、Docker Compose 编排与基础 CI。

## 技术组件

- backend：FastAPI + Pydantic Settings + structlog（JSON 日志）+ SQLAlchemy 2（异步）+ Psycopg 3 + chromadb-client
- 迁移：Alembic（空 baseline）
- 编排：Docker Compose（postgres / chroma / backend）
- 测试：pytest + httpx TestClient（单元）+ 真实服务（集成）
- CI：GitHub Actions（静态检查 / 单元测试 / pip check / 镜像构建）

## 服务与端口

| 服务 | 宿主机 | 容器 | 说明 |
|---|---|---|---|
| backend | 8001 | 8000 | FastAPI |
| postgres | 5433 | 5432 | PostgreSQL |
| chroma | 8002 | 8000 | Chroma Server |

## 健康检查语义

- `GET /api/v1/health/live`：进程可响应即 200，不检查外部依赖。
- `GET /api/v1/health/ready`：检查 configuration / database / chroma 三项；全 ok → 200，任一 error → 503 not_ready。

## Alembic

- baseline 迁移 `0001`；当前库中仅 `alembic_version` 表，无业务表。
- 连续 upgrade head 幂等（第二次不产生新迁移）。

## 测试

- 单元测试：31 passed（含 runtime 配置测试），默认排除集成测试。
- 集成测试：2 passed（真实 PostgreSQL `SELECT 1` 与 Chroma heartbeat）。

## 故障注入（已验证）

- 停止 Chroma：live 仍 200，ready 503（checks.chroma=error）。
- 停止 PostgreSQL：live 仍 200，ready 503（checks.database=error）。
- 恢复后 ready 均回到 200。

## 日志与敏感信息

- 请求日志为单行 JSON（timestamp/level/logger/event + request_id）。
- uvicorn 原生 access log 已关闭（--no-access-log）。
- 日志不包含数据库密码、完整连接串或堆栈；readiness 失败只记录 check 与 error_type。
- chromadb-client 1.5.9 异步客户端没有公开的异步关闭接口；不调用私有方法，资源随进程退出释放（见 ChromaManager 注释与 ADR-0003）。

## 明确未实现

- 业务表（ResearchTask / SourceRecord / EvidenceCard 等）
- LangGraph 编排与 Checkpointer
- Agent、RAG、Embedding、Chroma Collection
- 前端工程
- Redis / Celery / 任务队列

## 验收结论

阶段 0（工程基座）已达到进入阶段 1 的门槛：基础环境、配置、日志、健康检查、持久化与容器化均已验证通过。
