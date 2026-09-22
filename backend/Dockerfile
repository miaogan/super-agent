# V1：FastAPI + 前端一体化镜像
FROM python:3.11-slim

# 系统依赖：psycopg 编译需要 libpq（binary wheel 已含；保险起见装）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# 安装 uv（快速 pip 替代）用于依赖管理
RUN pip install --no-cache-dir uv

WORKDIR /app

# 先拷依赖描述，利用 docker 层缓存
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -e .

# 拷源码
COPY . .

# 准备 skills 目录（挂载点）
RUN mkdir -p /app/skills/global

EXPOSE 8000

# 启动 FastAPI 服务；OpenSandbox server 需独立启动
CMD ["python", "server.py"]
