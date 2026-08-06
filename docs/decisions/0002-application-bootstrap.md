# ADR-0002：应用引导与请求追踪基础

- 状态：已接受
- 日期：2026-08-06
- 决策人：InsightForge 项目

## 背景

阶段 0C 需要建立可测试、可配置、具备请求追踪能力的最小 FastAPI 应用。应用是后续所有阶段（LangGraph 编排、数据库接入、Agent 运行）的宿主，其引导方式、配置加载、日志与健康检查语义会长期影响整个系统。

## 决策

1. **采用应用工厂 `create_app(settings)`**。
   - 每个调用返回独立的 FastAPI 实例，测试可注入隔离的 Settings，生产用 `get_settings()` 默认值。
   - 避免模块导入时构造全局 app 带来的测试耦合与状态泄漏。
2. **配置通过 Pydantic Settings 集中加载**（`app/core/config.py`）。
   - 字段显式声明，环境变量 + `.env` 统一注入，测试可用 `Settings(_env_file=None)` 隔离本地 `.env`。
   - `.env` 路径按 `config.py` 文件位置向上推导到项目根目录，**不依赖终端当前工作目录**，保证任何启动位置行为一致。
3. **每个请求生成并传递 `request_id`**。
   - 优先沿用调用方 `X-Request-ID`，否则生成 UUID；响应头回写同一值，便于跨服务追踪。
   - 通过 `structlog.contextvars` 绑定到请求日志，请求结束清理，避免并发请求互相污染。
4. **live 与 ready 职责分离**。
   - `live` 只表示进程能响应，不触碰任何外部依赖；`ready` 表示配置加载成功、可接收请求。
   - 当前 `ready` 的 checks 只含 `configuration`，**不检查**尚未接入的 PostgreSQL、ChromaDB 与模型 API，避免伪造不存在的依赖状态。

## 后果

- 后续阶段接入数据库与向量库后，只需在 `ready` 的 checks 中扩展真实依赖探测，接口契约不变。
- 若未来需要支持分布式追踪（如 W3C traceparent），可在中间件中扩展，`request_id` 语义保持不变。
