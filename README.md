# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统：**Source → Evidence → Claim → Report → Audit**，
一切结论可追溯到证据，一切财务数字可由原始观测确定性重算。

## 技术栈

FastAPI · LangGraph（唯一顶层编排器）· PostgreSQL · ChromaDB · DeepSeek · React + Vite

## 启动步骤

### 1. 前置要求

- Docker（提供 PostgreSQL 与 ChromaDB）
- Python 3.12（建议 conda 环境，如 insightforge）
- Node.js 18+

### 2. 配置

```bash
cp .env.example .env        # 按需修改数据库密码；DeepSeek key 可留空（仍可启动）
```

### 3. 启动依赖服务

```bash
docker compose up -d postgres chroma
```

### 4. 启动后端（首次先安装与迁移）

```bash
cd backend
pip install -e ".[dev]" --extra-index-url https://download.pytorch.org/whl/cpu   # 首次
python -m alembic upgrade head        # 空库时初始化 schema
python -m app.cli.run_backend         # 启动 API（默认 http://127.0.0.1:8001）
```

首次启动自动导入内置数据（来源机构注册表 / 5500+ 家 A 股公司主数据 / 官网域名 registry），无需手动 seed。

### 5. 启动前端

```bash
cd frontend
npm ci
npm run dev                 # http://localhost:5173（API 默认指向 8001）
```

### 6. 验证

打开 http://localhost:5173 → 新建任务 → 输入公司名（如 宁德时代 或 600519）→ 自动研究：
规划 → 获取资料（公告/官网 IR）→ 证据提取 → 分析 → 报告 → 审计。资料不足或等待确认时停在人工环节，
按界面提示 approve / 补资料后继续。

API 文档：http://127.0.0.1:8001/docs

## 一键启动（全 Docker）

```bash
docker compose up -d --build
# 前端 http://localhost:8080 · 后端 http://localhost:8001
```

> **Docker 构建说明（国内镜像默认）**：为缓解国内网络下 Docker Hub / PyPI / npm 的
> 超时问题，构建已默认走国内镜像源：
> - 基础镜像：DaoCloud 镜像站（`m.daocloud.io/docker.io/library/*`，阿里云等公共镜像
>   仓库缺少部分 tag，故选用 DaoCloud）；
> - Python 依赖：阿里云 PyPI（`https://mirrors.aliyun.com/pypi/simple/`）；
>   PyTorch CPU wheel 仍从官方 CPU 源
>   （`https://download.pytorch.org/whl/cpu`）安装，仅普通依赖走阿里云；
> - 前端依赖：npmmirror（`https://registry.npmmirror.com`）；
> - PostgreSQL / ChromaDB 继续使用官方公共镜像（业务约束：不替换数据服务镜像）。

## 常用命令

```bash
cd backend
python -m pytest                      # 单元测试
python -m pytest -m integration       # 集成测试（需 PostgreSQL + Chroma 已启动）
python -m ruff check app tests

cd ../frontend
npm run test
npm run typecheck
npm run build
```

## 备注

- Windows 下若外部网络走代理，启动后端前设置 $env:NO_PROXY='*' 可避免代理干扰；
  run_backend 入口已处理 Windows 事件循环问题。
- HuggingFace 不可达时，可用 EMBEDDING_LOCAL_MODEL_PATH=<本地模型目录> 让 embedding 走本地离线加载。
- 仅做 A 股基本面研究；不提供自动交易、技术分析、短期预测或买卖建议。

详细设计见 docs/（ADR 与验收记录）。
