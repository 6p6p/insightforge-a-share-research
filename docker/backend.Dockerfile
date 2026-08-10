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
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.13.0+cpu"

# 安装 backend 运行依赖（不含 dev 组）
RUN pip install --no-cache-dir .

# 清理镜像内的全部 __pycache__ / .pyc：当前 Windows/Docker 构建环境中观察到间歇性
# pyc corruption（ValueError: bad marshal data），源码 .py 均正常、移除 pyc 后运行
# 正常，具体底层存储原因尚未独立证明。运行时已设 PYTHONDONTWRITEBYTECODE=1
# （不写新 pyc）→ 删除后 import 一律从源码编译，容器对 pyc 损坏免疫。
RUN find /usr/local/lib/python3.12 /app -type f -name '*.pyc' -delete \
    && find /usr/local/lib/python3.12 /app -type d -name '__pycache__' -empty -delete

# 非 root 用户运行
RUN useradd --create-home --uid 10001 appuser

# 原始归档挂载点：预创建并授予 appuser。
# named volume 首次挂载时复制目录内容并继承所有权，否则 volume 归 root，应用无写权限。
RUN mkdir -p /app/data/raw && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000
