FROM python:3.11-slim

WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# 安装系统依赖（如需编译 lxml 等可能需要，通常 python:slim 是轻量级镜像，有预编译包可不装）
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 暴露不常用的端口
EXPOSE 28001

# 启动服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "28001"]
