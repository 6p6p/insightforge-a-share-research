FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 应用源码与 Alembic 配置/迁移（不复制 .env）
COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic

# 安装 backend 运行依赖（不含 dev 组）
RUN pip install --no-cache-dir .

# 非 root 用户运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
