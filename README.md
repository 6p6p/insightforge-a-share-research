# InsightForge

面向 A 股上市公司的**证据驱动基本面研究与事实审核系统**（V1.0 release candidate）。

InsightForge 把「公司基本面研究」构建为一条可追溯、可重算、可审计的证据链：

```
Source → Evidence → Claim → Report → Audit
```

- **Source**：公司公告 / 新闻 / 宏观数据 / 估值数据（权威分级 + 原始归档）；
- **Evidence**：与原文精确对齐的证据卡（quote 切片、定位、provenance 指纹）；
- **Claim**：只允许引用已登记证据的结论（跨域分析：business / risk / financial /
  macro / valuation）；
- **Report**：由 bound Claims + Evidence 逐字引用的结构化报告；
- **Audit**：确定性检查 + 语义审计 + 人工确认/修订/补充研究，全部留痕。

一切结论必须可追溯到证据，一切财务数字可由原始观测确定性重算。

> **重要**：真实 LLM（DeepSeek）在严格结构化政策下的合规性问题与人工确认机制
> 详见 [已知限制](#已知限制-known-limitations)。当前不承诺「真实模型全自动无人值守」。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy 2 · Pydantic v2 |
| 编排 | **LangGraph**（唯一顶层编排器）+ PG Checkpointer（断点恢复） |
| 数据库 | PostgreSQL（业务事实 + checkpoint 源） |
| 向量 | ChromaDB（**派生、可重建**索引；BGE-small-zh embedding） |
| LLM | DeepSeek（`deepseek-v4-flash`，frozen policy，thinking disabled） |
| 前端 | React 18 · TypeScript · Vite · Ant Design · TanStack Query |
| 质量 | pytest（2300+ 单元 / 1000+ 集成）· ruff · alembic · GitHub Actions |

## 目录结构

```
backend/            FastAPI 后端（app / alembic / tests / scripts）
frontend/           React + TypeScript 前端
docs/               ADR（架构决策记录）与设计文档
docker/             backend 镜像构建
compose.yaml        PostgreSQL + Chroma + backend + frontend 编排
environment.yml     Conda 基础环境（仅 python 3.12 + pip）
```

## Quick Start

前置要求：Docker、Python 3.12、Node.js（前端）。

### 1. 克隆与配置

```bash
git clone <repo-url> insightforge
cd insightforge
cp .env.example .env        # 按需修改数据库密码 / DeepSeek key
```

`.env` 中 `DEEPSEEK_API_KEY` 可留空：应用仍可启动，LLM 相关路径会以
`pending_credentials` 语义工作（离线 fake 评估 / 确定性管线不受影响）。

### 2. 启动依赖服务（PostgreSQL + Chroma）

```bash
docker compose up -d postgres chroma
```

### 3. 安装 backend 并迁移

```bash
python -m pip install -e "./backend[dev]"
cd backend
python -m alembic upgrade head        # 空库 → 0047 head
```

### 4. 启动 backend

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

- 存活：http://127.0.0.1:8001/api/v1/health/live → `{"status":"ok"}`
- 就绪（database / chroma / checkpoint / raw / export 全依赖）：http://127.0.0.1:8001/api/v1/health/ready
- API 文档：http://127.0.0.1:8001/docs

### 5. 前端

```bash
cd frontend
npm ci
npm run dev                 # 开发服务器（VITE_API_BASE_URL 默认 http://localhost:8001/api/v1）
```

生产构建：`npm run build`（产物由 `frontend/nginx.conf` 同源反代 `/api/v1`）。

### 6. 测试与质量门禁

```bash
cd backend
python -m pytest                        # 单元/非集成（默认跳过 integration）
python -m pytest -m integration         # 集成（需 PostgreSQL + Chroma 已启动）
python -m ruff check app tests
python -m ruff format --check app tests
python -m pip check
python -m alembic current && python -m alembic heads && python -m alembic check

cd ../frontend
npm run test
npm run typecheck
npm run build
```

## 环境变量

见 `.env.example`（完整清单与注释）。关键项：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | PostgreSQL（`postgresql+psycopg://...`） |
| `CHROMA_HOST` / `CHROMA_PORT` | Chroma 地址 |
| `LLM_PROVIDER` / `LLM_MODEL` | 冻结为 `deepseek` / `deepseek-v4-flash` |
| `DEEPSEEK_API_KEY` | 真实 key 只放本地 `.env`；Secret 不进入日志 / API / DB |
| `RAW_STORAGE_ROOT` / `EXPORT_STORAGE_ROOT` | 原始归档 / 导出字节存储 |

## Docker Compose 完整启动

```bash
docker compose up -d --build        # postgres + chroma + backend + frontend
```

- backend：宿主机 `8001 → 容器 8000`；frontend：`8080 → 80`（nginx 同源反代）。
- 健康检查内置（backend `live/ready` 由 compose healthcheck 轮询）。
- 评估 workspace 以只读 volume 挂载（`./benchmark → /app/benchmark:ro`）。

## Demo 路径（人工确认闭环）

1. 打开前端 http://127.0.0.1:8080 → 「新建任务」（公司如 `600519` + 研究问题）。
2. 后台 LangGraph 编排自动推进：plan → 补料/来源就绪 → Stage4 分析 → Stage5 报告。
3. 在任务工作台查看状态 / 事件流 / 证据 / 分析 / 报告 / 审核记录。
4. 编排停在 `waiting_human` 时执行人工 action（approve / 补资料）→ 同一
   thread/checkpoint 恢复至 completed。
5. 报告段落 → claim → evidence → source 的 citation 导航全程可点击追溯。

受控 demo（0 真实 LLM）可通过离线评估与 golden preflight 复现，见下。

## 评估与 Benchmark（Stage 7B）

三路变体（`single_rag` / `multi_stage_no_audit` / `insightforge_full`）在同一
frozen case 上公平对比；每 attempt 独立隔离 PG + per-attempt Chroma collection；
执行与评分全部 immutable 持久化（fingerprint replay）。

```bash
cd backend
python -m app.eval.cli dataset --root ../benchmark/dataset        # 构建 frozen dataset
python -m app.eval.cli run --dataset ../benchmark/dataset \
    --workdir ../benchmark/run_fake                               # 离线确定性实验（默认）
python -m app.eval.cli score --workdir ../benchmark/run_fake      # 评分一致性校验
python -m app.eval.cli report --workdir ../benchmark/run_fake     # summary.md / summary.csv
```

- Web 对比视图：前端「评估对比」页（只读 `GET /api/v1/eval/benchmark/summary`）。
- 真实模型 bounded 探针：`python -m app.eval.cli run --real ...`（需 key，建议
  `--cases` 子集）。
- **Golden E2E preflight**（人工确认机制端到端）：

```bash
cd backend
# 全受控（0 真实 LLM，可离线复现）：waiting_human → 人工 approve → 同线程恢复 → 链路验证
python -m scripts.golden_full_real_preflight --mode controlled-plan \
    --financial-model fake --draft-model fake --claim-model fake \
    --extractor-model fake --macro-model fake --synthesis-model fake \
    --audit-model human-review
```

## 已知限制（Known Limitations）

1. **真实模型 × 严格结构化政策兼容性**（V1 已记录，不隐藏）：
   - financial analyst 输出数字字面量 → `FinancialAnalysisNumericLiteralForbidden`；
   - claim / synthesis 自由文本内联 alias（C1/E1…）→ `DraftSectionInlineAliasLeak`；
   - macro claim 数字在 macro 卡（quote 恒 NULL）上无法 grounding →
     `DraftSectionNumericGroundingError`；
   - 真实 audit 判定不可控：可能判 `research_backflow` +
     `structured_data_refresh_required`（该分支按设计拒绝文档补料恢复）。
   上述均为**确定性政策拒绝（0 写、稳定错误码）**，证据与复现见
   `backend/scripts/golden_full_real_preflight.py` 与 Stage 7 Final Closeout 记录。
   因此**「真实模型全自动无人值守生成报告」不是当前承诺**；受控组件 + 人工确认
   是当前支持的路径。
2. **人工确认机制不可绕过**：`awaiting_stage5` / `waiting_manual` /
   `research_backflow` 均需显式人工 action 或补资料，编排不做自动代理。
3. **外部数据源**：宏观 Provider（World Bank）依赖出网；部分网络环境存在域名级
   阻断（离线回放 / captured fetch 支持无网验证）。
4. **范围边界**：仅 A 股基本面研究；不提供自动交易、技术分析、短期预测或买卖建议。

## 文档

- `docs/decisions/0001-0036`：ADR（架构决策记录，含安全边界、provenance/replay、
  migration/version boundary 说明）。
- `docs/stage-7b1-evaluation-foundation.md`：评估体系（Stage 7B）正式设计文档。
- `docs/stage-0-acceptance.md` / `docs/stage-1-acceptance.md`：早期验收记录。

## License

（待定——发布前补充。）
