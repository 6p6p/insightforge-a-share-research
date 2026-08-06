# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统。

> 当前处于**阶段 0：工程基座**。核心证据链（Source → Evidence → Claim → Report → Audit）、LangGraph 编排、数据库与前端均尚未实现；当前可用的最小 FastAPI 应用仅提供健康检查接口。

## 目录职责

```
backend/            Python 后端（FastAPI）工程，依赖由 backend/pyproject.toml 管理
frontend/           （预留）React + TypeScript 前端入口，当前为空
docs/decisions/     ADR（架构决策记录）
docker/             （预留）容器编排与镜像构建文件
scripts/            （预留）开发/运维脚本
environment.yml     Conda 基础环境定义（仅解释器与 pip）
```

## 本地运行

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
copy .env.example .env
```

4. 启动 FastAPI：

```bash
conda run -n insightforge python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001 --reload
```

5. 健康检查：

- http://127.0.0.1:8001/api/v1/health/live
- http://127.0.0.1:8001/api/v1/health/ready

API 文档：http://127.0.0.1:8001/docs

## 质量检查

```bash
conda run -n insightforge ruff check backend
conda run -n insightforge ruff format --check backend
conda run -n insightforge python -m pytest -c backend/pyproject.toml backend/tests
```
