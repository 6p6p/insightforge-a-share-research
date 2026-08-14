FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 应用源码与 Alembic 配置/迁移（不复制 .env）
COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic

# CPU-only PyTorch：预装 PyTorch 官方 CPU wheel（torch==2.13.0+cpu），
# 避免 `pip install .` 从默认 PyPI 拉取 CUDA torch / nvidia-* 运行时包。
# PEP 440 下 torch==2.13.0 匹配 2.13.0+cpu（public version 忽略 local version），
# 因此后续 `pip install .` 会复用此 CPU torch，不会重装 CUDA build。
# `--index-url` 指向 PyTorch CPU 源（+cpu 版本只在此发布）；torch 的普通依赖
# （sympy / filelock 等）需 `--extra-index-url` 从 PyPI 解析（单源会解析失败）。
RUN python -m pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple \
    "torch==2.13.0+cpu"

# 安装 backend 运行依赖（不含 dev 组）；显式超时/重试抵御 CDN 抖动。
RUN pip install --no-cache-dir --timeout 120 --retries 5 .

# 清理镜像内的全部 __pycache__ / .pyc：当前 Windows/Docker 构建环境中观察到间歇性
# pyc corruption（ValueError: bad marshal data），源码 .py 均正常、移除 pyc 后运行
# 正常，具体底层存储原因尚未独立证明。运行时已设 PYTHONDONTWRITEBYTECODE=1
# （不写新 pyc）→ 删除后 import 一律从源码编译，容器对 pyc 损坏免疫。
RUN find /usr/local/lib/python3.12 /app -type f -name '*.pyc' -delete \
    && find /usr/local/lib/python3.12 /app -type d -name '__pycache__' -empty -delete

# 非 root 用户运行
RUN useradd --create-home --uid 10001 appuser

# 原始归档 + 导出归档挂载点：预创建并授予 appuser。
# named volume 首次挂载时复制目录内容并继承所有权，否则 volume 归 root，应用无写权限。
# `/app/data/exports`（stage 6C 内容寻址导出存储）必须同步预创建，否则新 volume
# 以 root 初始化 → appuser 写导出时 PermissionError（ready 的 export_storage 探针失败）。
RUN mkdir -p /app/data/raw /app/data/exports && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000
