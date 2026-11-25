FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY admin_service ./admin_service
COPY worker_service ./worker_service

# 管理端配置持久化目录（compose 中挂载命名卷）
RUN mkdir -p /data/admin /data/worker1 /data/worker2

EXPOSE 8000

# 默认启动管理服务；worker 服务在 docker-compose.yml 中覆盖 command
CMD ["uvicorn", "admin_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
