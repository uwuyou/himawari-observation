# 数据处理 Worker 镜像
# 包含 satpy / cartopy 需要的系统库
FROM python:3.11-slim

# cartopy 需要 geos/proj；git 用于 clone/push 仓库
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        libgeos-dev \
        libproj-dev \
        proj-bin \
        gcc \
        g++ \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# 先装依赖层，善用缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app.py .

EXPOSE 8080
CMD ["python", "app.py"]