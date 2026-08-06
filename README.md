# InsightForge

面向 A 股上市公司的证据驱动基本面研究与事实审核系统。

> 当前处于**阶段 0：工程基座**。核心证据链（Source → Evidence → Claim → Report → Audit）、LangGraph 编排、FastAPI 接口、数据库与前端均尚未实现，本仓库目前只包含最小工程结构。

## 目录职责

```
backend/            Python 后端（FastAPI）工程，依赖由 backend/pyproject.toml 管理
frontend/           （预留）React + TypeScript 前端入口，当前为空
docs/decisions/     ADR（架构决策记录）
docker/             （预留）容器编排与镜像构建文件
scripts/            （预留）开发/运维脚本
environment.yml     Conda 基础环境定义（仅解释器与 pip）
```

## 创建并激活 Conda 环境

```bash
conda env create -f environment.yml
conda activate insightforge
```

## 安装 backend 开发依赖

```bash
conda run -n insightforge python -m pip install -e "./backend[dev]"
```

以上命令须在项目根目录执行。Python 依赖一律通过 `pyproject.toml` 管理，不要手动往环境里塞无关包。
