# ADR-0003：持久化与容器化基础

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 背景

阶段 0D 需要为业务数据与向量数据建立可迁移、可检查、可容器化运行的基础。本阶段只搭地基：不创建业务表，不实现检索与 Agent，只让持久化基础设施可启动、可迁移、可被 ready 接口真实探测。

## 决策

1. **PostgreSQL 与 Chroma 职责分离**。
   - PostgreSQL 保存业务数据并承担未来 LangGraph Checkpointer；Chroma 只负责 Chunk 向量索引。两者生命周期、扩容与备份策略独立。
2. **PostgreSQL 使用 Psycopg 3 + 异步 SQLAlchemy**。
   - URL 统一 `postgresql+psycopg://`；不安装 psycopg2/asyncpg；Engine 走 async，避免同步/异步两套架构并存。
3. **Chroma 采用独立 Server + 薄客户端**。
   - 只安装 `chromadb-client`（不含服务端与 Embedding/模型 SDK）；应用通过官方 `AsyncHttpClient` 连接独立 Chroma Server。
4. **当前不创建业务表和 Collection**。
   - ResearchTask、SourceRecord、EvidenceCard 等属业务阶段；现在建表会造成契约未定即固化。Alembic 用空 baseline，Chroma 只做连接探测。
5. **startup 不因依赖不可用而崩溃**。
   - 资源管理器延迟初始化，启动不强制连接；外部服务不可用时应用照常启动，`live` 仍返回 200，真实状态交给 `ready`。
6. **live 与 ready 在容器环境中的作用**。
   - Compose 用 `live` 判断 backend 进程存活；`ready` 反映 PostgreSQL/Chroma 真实可用性（其中一项失败即 503 `not_ready`），供编排与人工排查。
7. **宿主机 PostgreSQL 使用 5433**。
   - 避免与任何本机 5432 既有实例冲突；容器内固定 5432，宿主机经 `${POSTGRES_HOST_PORT:-5433}` 映射。
8. **使用空 baseline migration**。
   - 让 Alembic 版本表（`alembic_version`）在容器与本地一致建立，后续业务迁移在其上线性追加；upgrade 后仅出现 Alembic 系统对象。
9. **当前版本选择**：`postgres:18.4-alpine`、`chromadb/chroma:1.5.9`、SQLAlchemy 2.x、Psycopg 3.x。未来升级大版本需重新跑集成测试与迁移验证。

## 后果

- ready 接口扩展依赖检查无需改契约；业务建表阶段在 baseline 之后追加迁移。
- 若未来需要独立 Chroma 服务或切换向量库，`vectorstore/` 是唯一改造面。
- 密码只存在于本地 `.env` 与运行时环境变量，不写入 compose/镜像/迁移文件。
